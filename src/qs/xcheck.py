"""원천값 자동 대조 (D-22 변경: 사람 확인 → 우리 규칙과 무관한 값과의 자동 대조).

목적: 계산 검증(엑셀 307/307)은 '공식이 맞는가'만 증명한다. 여기서는 '맞는 칸(계정)을 골랐는가'를 확인한다.

대조 출처 — 둘 다 우리 계정 선택 규칙(account_rules.yaml)을 거치지 않은 값
  A. DART 주요계정 API (fnlttSinglAcnt): 금감원이 자체 기준으로 뽑아 둔 매출액·영업이익·당기순이익·자산·부채·자본총계
     → 같은 공시 원천이므로 '같은 칸을 골랐는가'를 확인한다 (공시 자체의 오류는 못 잡음)
  B. 사업보고서 '배당에 관한 사항' (alotMatter): 회사가 본문에 직접 적는 (연결/별도)당기순이익
     → 지배주주 순이익을 재무표와 다른 경로로 확인 (서식상 연결은 지배기업 소유주 귀속분)

외부 사이트(FnGuide·네이버)는 자동 수집을 막아 두어 쓰지 않는다 (robots 차단).

판정: 차이 ≤ 단위 반올림 → 정확 일치 / ≤ 1% → 1% 이내 / 그 외 → 불일치 (원인 조사 대상)
      배당 항목은 회사마다 기재 방식이 달라, 단위 표기 오류·전체 순이익 기재로 설명되는 경우를 '설명됨'으로 따로 분류
"""
from __future__ import annotations

import csv
import re
from collections import Counter

from .normalize.ttm import ytd_from_row
from .storage import L1, L2, dart_path, exists, load_l0, save_l0
from .util import REPORTS, log, read_gz_json, to_num

TOL_PCT = 1.0
REPRT_OF_Q = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}

# 주요계정 account_nm → 우리 항목 (공백 제거 후 앞부분 일치)
MAIN_ACCOUNTS = {
    "revenue": re.compile(r"^(매출액|수익\(매출액\)|영업수익|매출)"),
    "op_income": re.compile(r"^영업이익"),
    "ni_total": re.compile(r"^당기순이익"),
    "assets": re.compile(r"^자산총계"),
    "liabilities": re.compile(r"^부채총계"),
    "equity_total": re.compile(r"^자본총계"),
}
FLOW = {"revenue", "op_income", "ni_total"}
UNIT = {"백만원": 1e6, "천원": 1e3, "원": 1.0, "억원": 1e8}


def compare(ours: float | None, ext: float | None, unit_tol: float = 0.5) -> tuple[str, float | None]:
    """(판정, 차이%). unit_tol = 외부값 표기 단위의 반올림 허용 절대오차(원)."""
    if ours is None and ext is None:
        return "둘 다 없음", None
    if ext is None:
        return "외부값 없음", None
    if ours is None:
        return "우리값 없음", None
    diff = abs(ours - ext)
    pct = None if ext == 0 else diff / abs(ext) * 100
    if diff <= unit_tol:
        return "정확 일치", 0.0 if pct is None else round(pct, 4)
    if pct is not None and pct <= TOL_PCT:
        return "1% 이내", round(pct, 4)
    return "불일치", None if pct is None else round(pct, 4)


def parse_main(rows: list[dict], fs_div: str) -> dict[str, float | None]:
    """주요계정 응답 → 우리 항목. 같은 fs_div 행만. 손익은 누적(YTD) 규칙(ytd_from_row) 적용."""
    out: dict[str, float | None] = {}
    for r in rows:
        if r.get("fs_div") != fs_div:
            continue
        nm = re.sub(r"\s", "", r.get("account_nm") or "")
        for item, pat in MAIN_ACCOUNTS.items():
            if item in out or not pat.match(nm):
                continue
            out[item] = ytd_from_row(r) if item in FLOW else to_num(r.get("thstrm_amount"))
    return out


def parse_dividend_ni(rows: list[dict], fs_div: str) -> tuple[float | None, float, str | None]:
    """배당 항목 → (당기순이익(원), 단위(원), 원문 구분명). 연결 회사는 '(연결)', 별도 회사는 '(별도)'.
    별도재무제표만 내는 회사가 '(별도)' 칸을 비우고 '(연결)' 칸에 적는 경우가 있다 (더라미·인바이오) → 보조로 읽는다."""
    tags = ["(연결)"] if fs_div == "CFS" else ["(별도)", "(연결)"]
    for tag in tags:
        for r in rows:
            se = re.sub(r"\s", "", r.get("se") or "")
            if se.startswith(tag) and "당기순이익" in se:
                m = re.search(r"\(([^()]*원)\)$", se)
                unit = UNIT.get(m.group(1), 1e6) if m else 1e6
                v = to_num(r.get("thstrm"))
                if v is not None:
                    return v * unit, unit, r.get("se") + ("" if tag == tags[0] else " (별도 칸 비어 연결 칸 사용)")
    return None, 1e6, None


def explain_dividend_mismatch(row: dict, ni_total: float | None, unit: float) -> None:
    """배당 항목 불일치 중 회사 기재 방식으로 설명되는 경우를 분류 (우리 값 오류와 구분).
      - 단위 표기 오류: 외부값 ÷ 단위가 우리 값과 일치 (원 단위 금액을 백만원 칸에 기재)
      - 전체 순이익 기재: 외부값이 지배+비지배 합계(당기순이익 전체)와 일치
    """
    if row["status"] != "불일치" or row["ours"] is None or row["external"] is None:
        return
    if compare(row["ours"], row["external"] / unit, 0.5)[0] == "정확 일치":
        row["status"], row["note"] = "설명됨: 회사 단위 표기 오류", "배당 항목에 원 단위 금액을 백만원 칸에 기재 — 우리 값과 원 단위로 정확히 일치"
    elif ni_total is not None and compare(ni_total, row["external"], unit)[0] == "정확 일치":
        row["status"], row["note"] = "설명됨: 회사가 전체 순이익 기재", \
            f"배당 항목 값이 지배+비지배 합계(당기순이익 {ni_total:,.0f})와 일치 — 지배 귀속분이 아님"


def nci_from_source(cc: str, y: int, q: int, div: str) -> float | None:
    """원천 재무(L0)의 비지배지분 잔액. 없으면 None."""
    p = dart_path("fs", cc, f"{y}_{REPRT_OF_Q[q]}_{div}.json.gz")
    if not exists(p):
        return None
    for r in load_l0(p)["payload"].get("list") or []:
        if r.get("sj_div") == "BS" and re.sub(r"^ifrs(-full)?_", "", r.get("account_id") or "") == "NoncontrollingInterests":
            return to_num(r.get("thstrm_amount"))
    return None


class Fetcher:
    """외부 대조값 수집 — L0에 보관 (재실행 시 재호출 안 함)."""

    def __init__(self, dart):
        self.d = dart

    def _get(self, api: str, cc: str, year: int, reprt: str) -> dict:
        path = dart_path("xcheck", api, cc, f"{year}_{reprt}.json.gz")
        if not exists(path):
            js = self.d._json(f"{api}.json", {"corp_code": cc, "bsns_year": str(year), "reprt_code": reprt})
            save_l0(path, source={"provider": "DART", "api": api, "corp_code": cc, "bsns_year": year,
                                  "reprt_code": reprt, "status": js.get("status"), "rows": len(js.get("list") or [])},
                    payload=js)
        return load_l0(path)["payload"]

    def main(self, cc, year, reprt):
        return self._get("fnlttSinglAcnt", cc, year, reprt)

    def dividend(self, cc, year):
        return self._get("alotMatter", cc, year, "11011")


def check_company(r: dict, fetch: Fetcher) -> list[dict]:
    cc = r["corp_code"]
    l1 = read_gz_json(L1 / "dart" / f"{cc}.json.gz")
    py, pq = int(r["period"][:4]), int(r["period"][-1])
    targets = [(py, pq)]
    annual = (py, 4) if pq == 4 else (py - 1, 4)
    if annual not in targets:
        targets.insert(0, annual)
    rows = []
    for (y, q) in targets:
        per = l1.get(f"{y}Q{q}")
        if not per:
            continue
        div, vals, conf, src = per["fs_div"], per["values"], per["confidence"], per["sources"]
        base = {"stock_code": r["stock_code"], "name": r["name"], "period": f"{y}Q{q}", "fs_div": div}

        def add(item, ext, source, unit_tol, ours=None, note=""):
            o = vals.get(item) if ours is None else ours
            st, pct = compare(o, ext, unit_tol)
            rows.append({**base, "item": item, "ours": o, "external": ext, "source": source, "status": st,
                         "diff_pct": pct, "our_confidence": conf.get(item),
                         "our_accounts": " | ".join(f"{a}:{n}" for a, n in (src.get(item) or [])[:3]), "note": note})

        js = fetch.main(cc, y, REPRT_OF_Q[q])
        ext = parse_main(js.get("list") or [], div) if js.get("status") == "000" else {}
        note_main = "" if js.get("status") == "000" else f"주요계정 응답 status {js.get('status')}"
        for item in MAIN_ACCOUNTS:
            add(item, ext.get(item), "DART 주요계정", 0.5, note=note_main)

        # 지배주주 자본: 외부 출처에 지배 귀속 자본이 없으므로
        #  - 우리가 '지배=전체' 규칙(no_nci·separate_only)을 쓴 경우 → 외부 자본총계와 직접 비교
        #  - 그 외 → 지배(우리) + 비지배(원천 행) = 자본총계(외부) 합계 대조
        #    (비지배지분은 음수일 수 있다 — SGC E&C 사례. '지배 ≤ 총계' 범위 확인은 틀린 규칙이라 폐기)
        if conf.get("equity_owners") in ("no_nci", "separate_only"):
            add("equity_owners", ext.get("equity_total"), "DART 주요계정 자본총계 (지배=전체 규칙 검증)", 0.5)
        else:
            nci = nci_from_source(cc, y, q, div)
            eo = vals.get("equity_owners")
            ours_sum = None if eo is None or nci is None else eo + nci
            add("equity_owners", ext.get("equity_total"), "DART 주요계정 자본총계 vs 지배(우리)+비지배(원천)", 0.5,
                ours=ours_sum, note=f"비지배지분 {nci:,.0f}" if nci is not None else "원천에 비지배지분 행 없음")
            rows[-1]["ours_raw"] = eo

        if q == 4:
            dj = fetch.dividend(cc, y)
            ni, unit, se = parse_dividend_ni(dj.get("list") or [], div) if dj.get("status") == "000" else (None, 1e6, None)
            add("ni_owners", ni, f"사업보고서 배당 항목 {se or ''}".strip(), unit,
                note="" if dj.get("status") == "000" else f"배당 항목 응답 status {dj.get('status')}")
            explain_dividend_mismatch(rows[-1], vals.get("ni_total"), unit)
    return rows


def run(dart) -> dict:
    from .util import elapsed_min
    if elapsed_min() > 110:   # 실행 시간 상한 보호 (2026-10-30) — 원천값 대조는 다음 실행에서
        return {"skipped": "시간 예산 — 다음 실행에서 대조"}
    res = read_gz_json(L2 / "sample_eval.json.gz")
    fetch = Fetcher(dart)
    rows = []
    for r in res:
        if r.get("status") != "ok":
            continue
        try:
            rows += check_company(r, fetch)
        except Exception as e:  # noqa: BLE001
            log(f"!! 대조 실패 {r['name']}: {e}")
            rows.append({"stock_code": r["stock_code"], "name": r["name"], "status": "오류", "note": str(e)[:200]})
    out = REPORTS / "phase1" / "external_crosscheck.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["stock_code", "name", "period", "fs_div", "item", "ours", "external", "diff_pct", "status", "source",
            "our_confidence", "our_accounts", "note"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for x in rows:
            w.writerow(x)
    by_item = {}
    for x in rows:
        by_item.setdefault(x.get("item", "-"), Counter())[x["status"]] += 1
    flagged = [{k: x.get(k) for k in ("name", "period", "item", "ours", "external", "diff_pct", "status",
                                      "our_confidence", "our_accounts", "source", "note")}
               for x in rows if x["status"] in ("불일치", "우리값 없음", "오류") or x["status"].startswith("설명됨")]
    summary = {"rows": len(rows), "status": dict(Counter(x["status"] for x in rows)),
               "by_item": {k: dict(v) for k, v in by_item.items()}, "flagged": flagged, **dart.http.stats()}
    log(f"원천값 대조: {summary['status']}")
    return summary
