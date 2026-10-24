"""전 종목 수집 (방침 B: 현재 시점 스크리너 먼저).

대상: KRX 최신 종목기본정보의 코스피·코스닥 보통주(주권) — 스팩 제외. 유니버스 필터(시총·거래대금·관리종목 등)는
      점수 단계에서 적용하고, 수집은 넓게 한다.
범위: 2024년 이후 정기보고서 (TTM·전년 TTM·전년 기말 잔액 계산에 필요한 최소 범위)
      2024 1분기~ → 현재 TTM(4개 분기)과 1년 전 TTM(영업이익 증가율·F-스코어)을 모두 분기 복원으로 계산 가능
절약: 최근 사업보고서의 연결재무제표 유무를 먼저 확인해, 연결이 없는 회사는 다른 보고서도 별도만 조회
      12월 외 결산 회사는 재무를 받지 않는다 (1단계 규칙상 TTM 무효)
새 공시: 매 실행 최근 10일 전체 시장 정기보고서를 조회해 새 보고서·정정본만 추가로 받는다 (update_recent).
재개: 이미 받은 파일은 건너뜀. 호출 예산 소진 시 정상 중단 → 다음 실행(일일 07:40)이 이어받음. 시총 큰 순서로 수집.
저장 형식은 표본 수집(collect/dart.py)과 같아 평가 파이프라인을 그대로 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json

from .collect.dart import DART, report_versions
from .http import BudgetExceeded
from .storage import L0, L1, dart_path, exists, load_l0, save_l0
from .util import elapsed_min, log, today_kst, write_gz_json

SINCE_YEAR = 2024
COLLECT_UNTIL_MIN = 95   # 프로세스 시작 후 95분이 지나면 중단 → 평가·화면·저장까지 워크플로 제한(150분) 안에 끝냄


def _latest(api: str) -> tuple[str, list[dict]]:
    for p in sorted((L0 / "krx" / api).glob("*.json.gz"), reverse=True):
        rec = load_l0(p)
        if rec["meta"].get("rows"):
            return p.name[:8], rec["payload"]
    return "", []


def universe() -> list[dict]:
    """[{stock_code, name, market, sect, list_dd, mcap}] — 보통주·주권·스팩 제외, 시총 큰 순."""
    out = {}
    for mk, base_api, px_api in (("KOSPI", "sto_stk_isu_base_info", "sto_stk_bydd_trd"),
                                 ("KOSDAQ", "sto_ksq_isu_base_info", "sto_ksq_bydd_trd")):
        _, base = _latest(base_api)
        _, px = _latest(px_api)
        mcap = {r["ISU_CD"]: float(r["MKTCAP"]) for r in px if r.get("MKTCAP") not in (None, "", "-")}
        for b in base:
            code = b["ISU_SRT_CD"]
            if b.get("KIND_STKCERT_TP_NM") != "보통주" or b.get("SECUGRP_NM") != "주권":
                continue
            if "SPAC" in (b.get("SECT_TP_NM") or "") or "스팩" in (b.get("ISU_ABBRV") or ""):
                continue
            out[code] = {"stock_code": code, "name": b.get("ISU_ABBRV"), "market": mk, "sect": b.get("SECT_TP_NM") or "",
                         "list_dd": b.get("LIST_DD"), "mcap": mcap.get(code)}
    return sorted(out.values(), key=lambda x: -(x["mcap"] or 0))


def _list_since(d: DART, cc: str, since: int = SINCE_YEAR) -> list[dict]:
    """공시 목록. 이미 있는 목록이 since보다 늦게 시작하면 since부터 다시 받아 합친다 (과거 이력 단계)."""
    path = dart_path("list", f"{cc}.json.gz")
    old = []
    if exists(path):
        rec = load_l0(path)
        have_from = str(rec["meta"].get("bgn_de") or "20150101")   # 표본 수집 목록은 2015년부터
        if have_from <= f"{since}0101":
            return rec["payload"]
        old = rec["payload"]
    items, page = [], 1
    while True:
        js = d._json("list.json", {"corp_code": cc, "bgn_de": f"{since}0101", "end_de": f"{today_kst():%Y%m%d}",
                                   "pblntf_ty": "A", "last_reprt_at": "N", "page_no": page, "page_count": 100})
        if js.get("status") not in ("000", "013"):
            raise RuntimeError(f"list.json {cc}: {js.get('status')} {js.get('message')}")
        items += js.get("list") or []
        if page >= int(js.get("total_page") or 1):
            break
        page += 1
    seen = {x["rcept_no"] for x in items}
    items += [x for x in old if x["rcept_no"] not in seen]
    save_l0(path, source={"provider": "DART", "api": "list.json", "corp_code": cc, "count": len(items),
                          "bgn_de": f"{since}0101"}, payload=items)
    return items


def _fs_one(d: DART, cc: str, y: int, reprt: str, div: str, force: bool = False) -> dict:
    path = dart_path("fs", cc, f"{y}_{reprt}_{div}.json.gz")
    if force or not exists(path):
        js = d._json("fnlttSinglAcntAll.json", {"corp_code": cc, "bsns_year": str(y), "reprt_code": reprt, "fs_div": div})
        save_l0(path, source={"provider": "DART", "api": "fnlttSinglAcntAll", "corp_code": cc, "bsns_year": y,
                              "reprt_code": reprt, "fs_div": div, "status": js.get("status"),
                              "rows": len(js.get("list") or [])}, payload=js)
    return load_l0(path)["payload"]


def update_recent(d: DART, corp_codes: set[str], days: int = 10) -> dict:
    """새 공시 반영: 회사 지정 없이 최근 N일 전체 시장 정기보고서를 조회 → 우리 유니버스 회사의 공시 목록에 추가.
    - 새 보고서: 목록에 추가 → collect_one이 없는 재무 파일을 받아옴
    - 정정 보고서(이미 받은 보고서의 새 버전): 해당 재무 파일을 다시 받음 (DART 응답은 최신 정정본)
    평소 하루 몇 회 호출, 실적 시즌에도 수십 회 + 새 보고서 재무."""
    end = today_kst()
    bgn = end - dt.timedelta(days=days)
    items, page = [], 1
    while True:
        js = d._json("list.json", {"bgn_de": f"{bgn:%Y%m%d}", "end_de": f"{end:%Y%m%d}", "pblntf_ty": "A",
                                   "last_reprt_at": "N", "page_no": page, "page_count": 100})
        if js.get("status") not in ("000", "013"):
            raise RuntimeError(f"list.json 최근 공시: {js.get('status')} {js.get('message')}")
        items += js.get("list") or []
        if page >= int(js.get("total_page") or 1):
            break
        page += 1
    save_l0(dart_path("recent", f"{end:%Y%m%d}.json.gz"),
            source={"provider": "DART", "api": "list.json", "bgn_de": f"{bgn:%Y%m%d}", "count": len(items)}, payload=items)
    new_reports, amended, refetched = 0, 0, 0
    by_cc: dict[str, list[dict]] = {}
    for it in items:
        if it.get("corp_code") in corp_codes:
            by_cc.setdefault(it["corp_code"], []).append(it)
    for cc, its in by_cc.items():
        path = dart_path("list", f"{cc}.json.gz")
        if not exists(path):
            continue                       # 아직 첫 수집 전 → collect_one이 전체 목록을 받음
        cur = load_l0(path)["payload"]
        have = {x["rcept_no"] for x in cur}
        add = [x for x in its if x["rcept_no"] not in have]
        if not add:
            continue
        before = report_versions(cur)
        cur = cur + add
        save_l0(path, source={"provider": "DART", "api": "list.json", "corp_code": cc, "count": len(cur),
                              "updated_from": "recent"}, payload=cur)
        after = report_versions(cur)
        for key, vs in after.items():
            if key not in before:
                new_reports += 1
            elif len(vs) > len(before[key]) and key[0] >= SINCE_YEAR:
                amended += 1
                for div in ("CFS", "OFS"):
                    if exists(dart_path("fs", cc, f"{key[0]}_{key[1]}_{div}.json.gz")):
                        _fs_one(d, cc, key[0], key[1], div, force=True)
                        refetched += 1
    return {"recent_filings": len(items), "companies": len(by_cc), "new_reports": new_reports,
            "amended": amended, "fs_refetched": refetched}


def _ok(js: dict) -> bool:
    return js.get("status") == "000" and bool(js.get("list"))


def collect_one(d: DART, cc: str, since: int = SINCE_YEAR) -> dict:
    comp = d.company(cc)
    acc = (comp.get("acc_mt") or "12").zfill(2)
    if acc != "12":
        return {"status": "non_dec", "acc_mt": acc}
    versions = report_versions(_list_since(d, cc, since), acc)
    keys = sorted(k for k in versions if k[0] >= since)
    if not keys:
        return {"status": "no_reports"}
    annual = [k for k in keys if k[1] == "11011"]
    probe = annual[-1] if annual else keys[-1]
    has_cfs = _ok(_fs_one(d, cc, *probe, "CFS"))
    got = 0
    for y, r in keys:
        if has_cfs and _ok(_fs_one(d, cc, y, r, "CFS")):
            got += 1
        elif _ok(_fs_one(d, cc, y, r, "OFS")):
            got += 1
    # 감사의견(사업보고서)·검토의견(반기) — 위험 필터·관리종목 판정용
    for y, r in keys:
        if r in ("11011", "11012"):
            d.audit(cc, y, r)
    # D-17: 정정 이력이 있는 보고서는 최초본 XBRL을 받아 둔다 (백테스트 시점 정합성)
    xb = 0
    for key in keys:
        vs = versions.get(key, [])
        if len(vs) > 1:
            info = d.xbrl(vs[0]["rcept_no"], key[1])
            xb += int(bool(info.get("ok")))
    return {"status": "ok", "reports": len(keys), "fs_ok": got, "cfs": has_cfs, "xbrl_first": xb}


HISTORY_SINCE = 2015


def history_targets(uni: list[dict]) -> list[str]:
    """과거 이력 대상 종목코드: 현재 유니버스(시총 순) → KRX 월말 종목기본정보에 나타난 과거 보통주(상장폐지 포함)."""
    out = [u["stock_code"] for u in uni]
    seen = set(out)
    for api in ("sto_stk_isu_base_info", "sto_ksq_isu_base_info"):
        for p in sorted((L0 / "krx" / api).glob("*.json.gz"), reverse=True):
            rec = load_l0(p)
            for b in rec["payload"] if rec["meta"].get("rows") else []:
                c = b["ISU_SRT_CD"]
                if c in seen or b.get("KIND_STKCERT_TP_NM") != "보통주" or b.get("SECUGRP_NM") != "주권":
                    continue
                if "SPAC" in (b.get("SECT_TP_NM") or "") or "스팩" in (b.get("ISU_ABBRV") or ""):
                    continue
                seen.add(c)
                out.append(c)
    return out


def collect_history(d: DART, uni: list[dict], corp: dict[str, str]) -> dict:
    targets = history_targets(uni)
    stat, done, stopped = {}, 0, None
    try:
        for code in targets:
            if elapsed_min() > COLLECT_UNTIL_MIN:
                stopped = f"시간 예산 {COLLECT_UNTIL_MIN}분 — 과거 이력은 다음 실행에서 이어받음"
                break
            cc = corp.get(code)
            r = collect_one(d, cc, since=HISTORY_SINCE) if cc else {"status": "no_corp_code"}
            stat[r["status"]] = stat.get(r["status"], 0) + 1
            done += 1
    except BudgetExceeded as e:
        stopped = str(e)
    res = {"targets": len(targets), "processed": done, "status": stat, "stopped": stopped,
           "complete": stopped is None and done == len(targets)}
    write_gz_json(L1 / "history_progress.json.gz", {"asof": today_kst().isoformat(), **res})
    log(f"과거 이력 수집: {json.dumps(res, ensure_ascii=False)}")
    return res


def run(d: DART) -> dict:
    uni = universe()
    corp = {c["stock_code"]: c["corp_code"] for c in d.corpcode() if c["stock_code"]}
    rows, stat, stopped, recent = [], {}, None, None
    try:
        recent = update_recent(d, {corp[u["stock_code"]] for u in uni if u["stock_code"] in corp})
        for u in uni:
            if elapsed_min() > COLLECT_UNTIL_MIN:
                stopped = f"시간 예산 {COLLECT_UNTIL_MIN}분 — 다음 실행에서 이어받음"
                break
            cc = corp.get(u["stock_code"])
            if not cc:
                r = {"status": "no_corp_code"}
            else:
                r = collect_one(d, cc)
            stat[r["status"]] = stat.get(r["status"], 0) + 1
            rows.append({**u, "corp_code": cc, **r})
    except BudgetExceeded as e:
        stopped = str(e)
        log(f"전 종목 수집 중단(다음 실행에서 이어받음): {e}")
    # 과거 이력 단계 (백테스트용): 현재 종목이 모두 끝난 뒤, 2015년~ 재무를 현재 종목 → 과거 상장(폐지 포함) 순으로
    hist = None
    if stopped is None:
        hist = collect_history(d, uni, corp)
        stopped = hist.get("stopped")
    # 수집 완료 목록 (평가 대상) — 중단돼도 지금까지 끝난 회사로 갱신
    write_gz_json(L1 / "universe.json.gz", {"asof": today_kst().isoformat(), "total": len(uni), "rows": rows,
                                            "complete": len(rows) == len(uni)})
    res = {"universe": len(uni), "processed": len(rows), "stopped": stopped, "status": stat, "recent": recent, "history": hist,
           **d.http.stats()}
    log(f"전 종목 수집: {json.dumps(res, ensure_ascii=False)}")
    return res
