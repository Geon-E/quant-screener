"""전 종목 수집 (방침 B: 현재 시점 스크리너 먼저).

대상: KRX 최신 종목기본정보의 코스피·코스닥 보통주(주권) — 스팩 제외. 유니버스 필터(시총·거래대금·관리종목 등)는
      점수 단계에서 적용하고, 수집은 넓게 한다.
범위: 2024년 이후 정기보고서 (TTM·전년 TTM·전년 기말 잔액 계산에 필요한 최소 범위)
      2024 1분기~ → 현재 TTM(4개 분기)과 1년 전 TTM(영업이익 증가율·F-스코어)을 모두 분기 복원으로 계산 가능
절약: 최근 사업보고서의 연결재무제표 유무를 먼저 확인해, 연결이 없는 회사는 다른 보고서도 별도만 조회
      12월 외 결산 회사는 재무를 받지 않는다 (1단계 규칙상 TTM 무효)
재개: 이미 받은 파일은 건너뜀. 호출 예산 소진 시 정상 중단 → 다음 실행(일일 07:40)이 이어받음. 시총 큰 순서로 수집.
저장 형식은 표본 수집(collect/dart.py)과 같아 평가 파이프라인을 그대로 쓴다.
"""
from __future__ import annotations

import json
import time

from .collect.dart import DART, report_versions
from .http import BudgetExceeded
from .storage import L0, L1, dart_path, exists, load_l0, save_l0
from .util import log, today_kst, write_gz_json

SINCE_YEAR = 2024
TIME_LIMIT_S = 80 * 60   # 워크플로 제한(120분) 안에서 저장 단계까지 끝나도록 수집은 80분에서 끊는다


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


def _list_since(d: DART, cc: str) -> list[dict]:
    path = dart_path("list", f"{cc}.json.gz")
    if exists(path):
        return load_l0(path)["payload"]
    items, page = [], 1
    while True:
        js = d._json("list.json", {"corp_code": cc, "bgn_de": f"{SINCE_YEAR}0101", "end_de": f"{today_kst():%Y%m%d}",
                                   "pblntf_ty": "A", "last_reprt_at": "N", "page_no": page, "page_count": 100})
        if js.get("status") not in ("000", "013"):
            raise RuntimeError(f"list.json {cc}: {js.get('status')} {js.get('message')}")
        items += js.get("list") or []
        if page >= int(js.get("total_page") or 1):
            break
        page += 1
    save_l0(path, source={"provider": "DART", "api": "list.json", "corp_code": cc, "count": len(items),
                          "bgn_de": f"{SINCE_YEAR}0101"}, payload=items)
    return items


def _fs_one(d: DART, cc: str, y: int, reprt: str, div: str) -> dict:
    path = dart_path("fs", cc, f"{y}_{reprt}_{div}.json.gz")
    if not exists(path):
        js = d._json("fnlttSinglAcntAll.json", {"corp_code": cc, "bsns_year": str(y), "reprt_code": reprt, "fs_div": div})
        save_l0(path, source={"provider": "DART", "api": "fnlttSinglAcntAll", "corp_code": cc, "bsns_year": y,
                              "reprt_code": reprt, "fs_div": div, "status": js.get("status"),
                              "rows": len(js.get("list") or [])}, payload=js)
    return load_l0(path)["payload"]


def _ok(js: dict) -> bool:
    return js.get("status") == "000" and bool(js.get("list"))


def collect_one(d: DART, cc: str) -> dict:
    comp = d.company(cc)
    acc = (comp.get("acc_mt") or "12").zfill(2)
    if acc != "12":
        return {"status": "non_dec", "acc_mt": acc}
    versions = report_versions(_list_since(d, cc), acc)
    keys = sorted(k for k in versions if k[0] >= SINCE_YEAR)
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
    return {"status": "ok", "reports": len(keys), "fs_ok": got, "cfs": has_cfs}


def run(d: DART) -> dict:
    uni = universe()
    corp = {c["stock_code"]: c["corp_code"] for c in d.corpcode() if c["stock_code"]}
    rows, stat, stopped = [], {}, None
    t0 = time.monotonic()
    try:
        for u in uni:
            if time.monotonic() - t0 > TIME_LIMIT_S:
                stopped = f"시간 제한 {TIME_LIMIT_S // 60}분 — 다음 실행에서 이어받음"
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
    # 수집 완료 목록 (평가 대상) — 중단돼도 지금까지 끝난 회사로 갱신
    write_gz_json(L1 / "universe.json.gz", {"asof": today_kst().isoformat(), "total": len(uni), "rows": rows,
                                            "complete": stopped is None})
    res = {"universe": len(uni), "processed": len(rows), "stopped": stopped, "status": stat, **d.http.stats()}
    log(f"전 종목 수집: {json.dumps(res, ensure_ascii=False)}")
    return res
