"""4차 협의 Q1 준비 실험: 관리종목 지정 요건을 재무·시세로 직접 판정하면 얼마나 맞는가.

정답지: KRX 코스닥 일별시세 소속부 '관리종목(소속부없음)' (월말 기준)
판정 규칙 (코스닥시장 상장규정의 재무·시가총액 요건 중 데이터로 확인 가능한 것):
  R1 매출액 30억 미만 (최근 사업연도)
  R2 자본잠식률 50% 이상 (최근 반기말·사업연도말) — 자본잠식률 = (자본금 − 자본총계) ÷ 자본금
  R3 자기자본 10억 미만 (최근 반기말·사업연도말)
  R4 법인세비용차감전손실이 자기자본의 50% 초과인 해가 최근 3개 사업연도 중 2회 이상 (최근 연도 포함)
  R5 최근 4개 사업연도 연속 영업손실
  R6 시가총액 40억 미만 30거래일 연속
판정 못 하는 요건: 감사·검토의견(추가 수집 필요), 공시·지배구조·거래량 요건, 기술성장기업 등 유예 → 누락 원인으로 집계
사전 기준 (claude_position.md Q1): 재현율 ≥ 90%, 정밀도 ≥ 80%
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from collections import Counter, defaultdict

from .normalize.calendar import TradingCalendar
from .pipeline import company_periods
from .storage import L0, load_l0
from .util import REPORTS, log

ADMIN = "관리종목(소속부없음)"


def _d(s):
    return dt.datetime.strptime(s, "%Y%m%d").date()


def month_ends(days: list[str]) -> list[str]:
    last = {}
    for d in days:
        last[d[:6]] = d
    return sorted(last.values())


def judge(periods: dict, asof: dt.date, mcap_hist: list[float], tech: bool = False) -> tuple[dict, list[str]]:
    """반환: ({규칙: True/False/None(판정 불가)}, 근거 문자열).
    tech: 기술성장기업부 소속 이력 — 매출액·법차손·연속 영업손실 요건 유예(근사: 유예 기간 구분 없이 미적용)."""
    avail = {k: p for k, p in periods.items() if dt.date.fromisoformat(p["available_date"]) <= asof}
    fy = sorted((y for (y, q) in avail if q == 4), reverse=True)
    bs_keys = sorted((k for k in avail if k[1] in (2, 4)), reverse=True)
    v = lambda k, it: avail[k]["values"].get(it) if k in avail else None   # noqa: E731
    res, why = {}, []
    if fy:
        rev = v((fy[0], 4), "revenue")
        res["R1"] = None if rev is None else rev < 3.0e9
    else:
        res["R1"] = None
    if bs_keys:
        k = bs_keys[0]
        cap, eq = v(k, "capital_stock"), v(k, "equity_total")
        res["R2"] = None if not cap or eq is None else (cap - eq) / cap >= 0.5
        res["R3"] = None if eq is None else eq < 1.0e9
    else:
        res["R2"] = res["R3"] = None
    if len(fy) >= 3:
        hits = []
        for y in fy[:3]:
            pt, eq = v((y, 4), "pretax"), v((y, 4), "equity_total")
            hits.append(None if pt is None or eq is None else (pt < 0 and -pt > 0.5 * eq if eq > 0 else pt < 0))
        res["R4"] = None if None in hits else (hits[0] and sum(hits) >= 2)
    else:
        res["R4"] = None
    if len(fy) >= 4 and all(fy[i] == fy[0] - i for i in range(4)):
        ops = [v((y, 4), "op_income") for y in fy[:4]]
        res["R5"] = None if None in ops else all(o < 0 for o in ops)
    else:
        res["R5"] = None
    res["R6"] = (len(mcap_hist) >= 30 and all(m < 4.0e9 for m in mcap_hist[-30:])) if mcap_hist else None
    if tech:
        for k in ("R1", "R4", "R5"):
            res[k] = False
    for r, x in res.items():
        if x:
            why.append(r)
    return res, why


def run() -> dict:
    from .collect.dart import DART
    corp = {c["stock_code"]: c["corp_code"] for c in DART(budget=1).corpcode() if c["stock_code"]}
    cal = TradingCalendar.from_l0()
    files = sorted((L0 / "krx" / "sto_ksq_bydd_trd").glob("*.json.gz"))
    daily = {}
    for p in files:
        rec = load_l0(p)
        if rec["meta"].get("rows"):
            daily[p.name[:8]] = rec["payload"]
    days = sorted(daily)
    mcaps = defaultdict(list)
    tech = {r["ISU_CD"] for rows in daily.values() for r in rows if r.get("SECT_TP_NM") == "기술성장기업부"}
    cache = {}
    tp = fp = fn = 0
    rule_hits, miss_reasons, coverage = Counter(), Counter(), Counter()
    rows_out = []
    for d in days:
        for r in daily[d]:
            try:
                mcaps[r["ISU_CD"]].append(float(r["MKTCAP"]))
            except (TypeError, ValueError, KeyError):
                pass
        if d not in month_ends(days):
            continue
        asof = _d(d)
        for r in daily[d]:
            code = r["ISU_CD"]
            cc = corp.get(code)
            if not cc:
                coverage["고유번호 없음"] += 1
                continue
            if cc not in cache:
                try:
                    cache[cc] = company_periods({"corp_code": cc, "stock_code": code, "name": r["ISU_NM"], "category": ""}, cal)[1]
                except FileNotFoundError:
                    cache[cc] = None
            periods = cache[cc]
            if not periods:
                coverage["재무 미수집"] += 1
                continue
            coverage["판정 대상"] += 1
            res, why = judge(periods, asof, mcaps[code], tech=code in tech)
            pred = bool(why)
            truth = r.get("SECT_TP_NM") == ADMIN
            for k in why:
                rule_hits[k] += 1
            if pred and truth:
                tp += 1
            elif pred and not truth:
                fp += 1
            elif truth and not pred:
                fn += 1
                unknown = [k for k, x in res.items() if x is None]
                miss_reasons["판정 불가 규칙 있음(" + ",".join(unknown) + ")" if unknown else "재무 요건 아님(감사의견·공시·유예 등 추정)"] += 1
            if pred != truth:
                rows_out.append([d, code, r["ISU_NM"], "오탐" if pred else "누락", ",".join(why),
                                 ",".join(k for k, x in res.items() if x is None)])
    recall = tp / (tp + fn) if tp + fn else None
    precision = tp / (tp + fp) if tp + fp else None
    out_dir = REPORTS / "phase1" / "exp_admin_rules"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "mismatch.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month_end", "code", "name", "type", "rules_hit", "rules_unknown"])
        w.writerows(rows_out)
    res = {"month_ends": len(month_ends(days)), "tp": tp, "fp": fp, "fn": fn,
           "recall": None if recall is None else round(recall, 3), "precision": None if precision is None else round(precision, 3),
           "pre_registered": {"recall": 0.90, "precision": 0.80},
           "passes": None if recall is None or precision is None else (recall >= 0.9 and precision >= 0.8),
           "rule_hits": dict(rule_hits), "miss_reasons": dict(miss_reasons.most_common()), "coverage": dict(coverage)}
    (out_dir / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"관리종목 요건 판정 실험: {res}")
    return res
