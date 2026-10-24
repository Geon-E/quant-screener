"""표본 평가 파이프라인: L0 → L1(표준 계정·시점 정보) → L2(지표) + 커버리지 보고 + 검증 워크북.

기준일 D = 최신 거래일 (상장폐지 종목은 마지막 거래일).
기준 분기 P = available_date ≤ D 인 가장 최근 정기보고서 분기.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

from .collect.dart import report_versions
from .normalize.accounts import extract, rules
from .normalize.calendar import TradingCalendar
from .normalize.ttm import REPRT_Q, prev_q, quarterly, ttm, yoy_ttm_growth
from .sample import load_sample
from .score import fscore
from .score.metrics import (book_yield, earnings_quality, earnings_yield, ebitda_ev, ev, gp_a, interest_coverage,
                            merge_preferred_mcap, momentum_12_1, roe, Metric)
from .storage import L0, L1, L2, dart_path, exists, load_l0
from .util import REPORTS, canonical_json, log, sha256_bytes, write_gz_json, write_text

FLOW_ITEMS = [k for k, v in rules().items() if v["kind"] == "flow"]
STOCK_ITEMS = [k for k, v in rules().items() if v["kind"] == "stock"]


def _d(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y%m%d").date()


# ── KRX ──
class Prices:
    def __init__(self):
        self.dir_k = L0 / "krx" / "sto_stk_bydd_trd"
        self.dir_q = L0 / "krx" / "sto_ksq_bydd_trd"
        self.days = sorted(p.name[:8] for p in self.dir_k.glob("*.json.gz") if load_l0(p)["meta"].get("rows"))
        self._cache = {}

    def day(self, bd: str) -> list[dict]:
        if bd not in self._cache:
            rows = []
            for d in (self.dir_k, self.dir_q):
                p = d / f"{bd}.json.gz"
                if p.exists():
                    rows += load_l0(p)["payload"]
            self._cache[bd] = rows
        return self._cache[bd]

    def base_info(self, bd: str) -> list[dict]:
        rows = []
        for api in ("sto_stk_isu_base_info", "sto_ksq_isu_base_info"):
            files = sorted(p.name[:8] for p in (L0 / "krx" / api).glob("*.json.gz") if p.name[:8] <= bd)
            for f in reversed(files):
                rec = load_l0(L0 / "krx" / api / f"{f}.json.gz")
                if rec["meta"].get("rows"):
                    rows += rec["payload"]
                    break
        return rows

    def last_trading_day_of(self, code: str) -> str | None:
        for bd in reversed(self.days):
            if any(r["ISU_CD"] == code for r in self.day(bd)):
                return bd
        return None


# ── 수정주가·모멘텀 (D-16) ──
_ADJ = {}


def _adj_state():
    if not _ADJ:
        from .adjust import load_series
        p = L1 / "krx" / "adj_events.json.gz"
        _ADJ["events"] = load_l0(p) if p.exists() else None   # write_gz_json 형식 그대로 읽음
        _ADJ["series"] = load_series()
    return _ADJ


def _momentum(code: str, last_bd: str, px) -> Metric:
    from .adjust import adjusted, verified_for
    st = _adj_state()
    if st["events"] is None:
        return momentum_12_1([], adjustment_verified=False)
    s = [x for x in st["series"].get(code, []) if x["d"] <= last_bd]
    ev = [e for e in st["events"] if e["code"] == code]
    closes = [a for _, a in adjusted(s, ev)]
    if len(s) < 253:
        return momentum_12_1(closes, adjustment_verified=True)  # → 가격 이력 부족
    ok, rel = verified_for(code, ev, s[-253]["d"], last_bd)
    m = momentum_12_1(closes, adjustment_verified=ok)
    if not ok:
        m.reason = "수정주가 미검증(미확인 기업행위: " + ", ".join(f"{e['date']} {e['class']}" for e in rel
                                                    if not e.get("confirmed_by")) + ")"
    return m


# ── 회사 1개 ──
def build_company(row: dict, cal: TradingCalendar, px: Prices) -> dict:
    cc, code = row["corp_code"], row["stock_code"]
    comp = load_l0(dart_path("company", f"{cc}.json.gz"))["payload"]
    acc_mt = (comp.get("acc_mt") or "12").zfill(2)
    filings = load_l0(dart_path("list", f"{cc}.json.gz"))["payload"]
    versions = report_versions(filings, acc_mt)
    out = {"stock_code": code, "corp_code": cc, "name": row["name"], "category": row["category"],
           "induty_code": comp.get("induty_code"), "acc_mt": acc_mt, "flags": []}
    if acc_mt != "12":
        out["flags"].append("12월 외 결산 → 1단계 TTM 무효 (DESIGN 4.3)")
        out["status"] = "excluded_non_dec"
        return out

    # L1: 보고서별 표준 항목
    periods = {}
    # 연결재무제표 제출 분기 (separate_only 오적용 방지: 인접 ±4분기에 연결이 있으면 별도 기간의 대체 금지)
    cfs_q = set()
    for (y, c) in versions:
        p = dart_path("fs", cc, f"{y}_{c}_CFS.json.gz")
        if exists(p) and load_l0(p)["payload"].get("status") == "000":
            cfs_q.add(y * 4 + REPRT_Q[c])
    out["cfs_quarters"] = len(cfs_q)
    for (y, code_r), vs in sorted(versions.items()):
        qi = y * 4 + REPRT_Q[code_r]
        had_cfs = any(abs(qi - x) <= 4 for x in cfs_q)
        q = REPRT_Q[code_r]
        fs_div, payload = None, None
        for div in ("CFS", "OFS"):
            p = dart_path("fs", cc, f"{y}_{code_r}_{div}.json.gz")
            if exists(p):
                js = load_l0(p)["payload"]
                if js.get("status") == "000" and js.get("list"):
                    fs_div, payload = div, js
                    break
        if not payload:
            continue
        items = extract(payload["list"], fs_div, had_cfs=had_cfs)
        api_rcepts = sorted({r.get("rcept_no") for r in payload["list"] if r.get("rcept_no")})
        first, last = vs[0], vs[-1]
        avail, exact = cal.next_trading_day(_d(first["rcept_dt"]))
        periods[(y, q)] = {
            "year": y, "q": q, "reprt_code": code_r, "fs_div": fs_div,
            "rcept_first": first["rcept_no"], "rcept_dt_first": first["rcept_dt"],
            "rcept_last": last["rcept_no"], "rcept_dt_last": last["rcept_dt"],
            "api_rcept_no": api_rcepts[-1] if api_rcepts else None,
            "amended": any("기재정정" in v["tag"] for v in vs[1:]),
            "available_date": avail.isoformat(), "available_exact": exact,
            "values": {k: v.value for k, v in items.items()},
            "confidence": {k: v.confidence for k, v in items.items()},
            "sources": {k: v.sources for k, v in items.items()},
        }
    out["l1_periods"] = len(periods)

    # 기준일 D
    last_bd = px.last_trading_day_of(code)
    if not last_bd:
        out["status"] = "no_price"
        out["flags"].append("KRX 백필 기간에 시세 없음")
        return out
    D = _d(last_bd)
    out["asof"] = D.isoformat()
    if last_bd != px.days[-1]:
        out["flags"].append(f"상장폐지/거래정지 — 마지막 거래일 {last_bd} 기준 평가")

    usable = [k for k, p in periods.items() if dt.date.fromisoformat(p["available_date"]) <= D]
    if not usable:
        out["status"] = "no_financials"
        return out
    P = max(usable)
    out["period"] = f"{P[0]}Q{P[1]}"
    target_div = periods[P]["fs_div"]
    out["fs_div"] = target_div

    # 분기 복원 (같은 fs_div만 — 연결/별도 혼용 금지)
    def series(item):
        ytd = {k: p["values"][item] for k, p in periods.items() if p["fs_div"] == target_div}
        return quarterly(ytd)

    qs = {it: series(it) for it in FLOW_ITEMS}
    T = {it: ttm(qs[it], *P) for it in FLOW_ITEMS}
    P4 = (P[0] - 1, P[1])
    T_prev = {it: ttm(qs[it], *P4) for it in FLOW_ITEMS}
    S = {it: (periods[P]["values"].get(it) if P in periods else None) for it in STOCK_ITEMS}
    S_prev = {it: (periods[P4]["values"].get(it) if P4 in periods and periods[P4]["fs_div"] == target_div else None)
              for it in STOCK_ITEMS}

    # 시점 정합성: TTM에 쓰인 분기 중 D 이후 정정된 것이 있으면 미충족
    used = [P]
    k = P
    for _ in range(3):
        k = prev_q(*k)
        used.append(k)
    pit_issue = [f"{y}Q{q}" for (y, q) in used if (y, q) in periods and periods[(y, q)]["amended"]
                 and _d(periods[(y, q)]["rcept_dt_last"]) > D]
    if pit_issue:
        out["flags"].append(f"시점 정합성 미충족(기준일 이후 정정): {pit_issue}")

    # 시가총액 (보통주 + 우선주)
    day_rows = px.day(last_bd)
    mc = merge_preferred_mcap(day_rows, px.base_info(last_bd)).get(code)
    pr = next((r for r in day_rows if r["ISU_CD"] == code), {})
    mcap = mc["mcap_total"] if mc else None
    out.update({"close": pr.get("TDD_CLSPRC"), "mcap_common": mc and mc["mcap_common"], "mcap_pref": mc and mc["mcap_pref"],
                "mcap": mcap, "pref_codes": mc and mc["pref_codes"]})

    # 지표
    ev_v = ev(mcap, S["debt"], S["cash"], S["st_fin"])
    # 이자: 이자지급(CF)이 기준. 없을 때만 금융원가 대용 — 신뢰도를 구분해 보고 (3차 협의 #5)
    interest, int_basis = None, "없음"
    if T["interest_paid"] not in (None, 0):
        interest, int_basis = T["interest_paid"], "이자지급(CF)"
    elif T["finance_costs"] not in (None, 0):
        interest, int_basis = abs(T["finance_costs"]), "금융원가 대용(신뢰도 낮음)"
    elif T["interest_paid"] == 0 or T["finance_costs"] == 0:
        interest, int_basis = 0.0, "이자 0 (무차입)"
    m: dict[str, Metric] = {
        "E/P": earnings_yield(T["ni_owners"], mcap),
        "B/P": book_yield(S["equity_owners"], mcap),
        "EBIT/EV": Metric(None, "EV 비정상") if ev_v is None or mcap is None or ev_v <= 0 or ev_v < 0.1 * mcap
        else Metric(None if T["op_income"] is None else T["op_income"] / ev_v, None if T["op_income"] is not None else "영업이익 없음"),
        "EBITDA/EV(실험)": ebitda_ev(T["op_income"], T["depreciation"], ev_v, mcap),  # D-20: 전략에서 제외, 커버리지 추적용
        "ROE": roe(T["ni_owners"], S_prev["equity_owners"], S["equity_owners"], mcap),
        "GP/A": gp_a(T["gross_profit"], S_prev["assets"], S["assets"]),
        "CFO/NI": earnings_quality(T["cfo"], T["ni_total"]),
        "이자보상배율": interest_coverage(T["op_income"], interest),
        "영업이익증가율": Metric(yoy_ttm_growth(qs["op_income"], *P)),
        "12-1모멘텀": _momentum(code, last_bd, px),
    }
    fs_cur = {"ni": T["ni_total"], "cfo": T["cfo"], "assets_begin": S_prev["assets"], "assets_end": S["assets"],
              "lt_debt": S["lt_debt"], "avg_assets": None if S_prev["assets"] is None or S["assets"] is None else (S_prev["assets"] + S["assets"]) / 2,
              "cur_assets": S["cur_assets"], "cur_liab": S["cur_liab"], "shares": None,
              "gross_profit": T["gross_profit"], "revenue": T["revenue"]}
    P8 = (P[0] - 2, P[1])
    S_prev2 = {it: (periods[P8]["values"].get(it) if P8 in periods and periods[P8]["fs_div"] == target_div else None)
               for it in STOCK_ITEMS}
    fs_prev = {"ni": T_prev["ni_total"], "cfo": T_prev["cfo"], "assets_begin": S_prev2["assets"], "assets_end": S_prev["assets"],
               "lt_debt": S_prev["lt_debt"], "avg_assets": None if S_prev2["assets"] is None or S_prev["assets"] is None else (S_prev2["assets"] + S_prev["assets"]) / 2,
               "cur_assets": S_prev["cur_assets"], "cur_liab": S_prev["cur_liab"], "shares": None,
               "gross_profit": T_prev["gross_profit"], "revenue": T_prev["revenue"]}
    fsc, fitems = fscore.score(fs_cur, fs_prev)

    out.update({
        "status": "ok",
        "ttm": T, "ttm_prev": T_prev, "stock": S, "stock_prev": S_prev,
        "confidence_P": periods[P]["confidence"], "sources_P": periods[P]["sources"], "ev": ev_v, "interest_basis": int_basis,
        "metrics": {k: v.value for k, v in m.items()}, "metric_reasons": {k: v.reason for k, v in m.items() if not v.valid},
        "fscore": fsc, "fscore_items": fitems,
        "periods_meta": {f"{y}Q{q}": {kk: p[kk] for kk in ("fs_div", "rcept_first", "rcept_dt_first", "rcept_last",
                                                           "rcept_dt_last", "api_rcept_no", "amended", "available_date")}
                         for (y, q), p in periods.items() if (y, q) >= (P[0] - 2, P[1])},
        "verify_inputs": verify_inputs(periods, P, target_div),
    })
    # L1 저장 (회사별)
    write_gz_json(L1 / "dart" / f"{cc}.json.gz",
                  {f"{y}Q{q}": p for (y, q), p in sorted(periods.items())})
    return out


def verify_inputs(periods: dict, P: tuple, div: str) -> dict:
    """검증 워크북용 원천값: TTM = YTD(P) + 연간(전년) − YTD(전년 동기) — 파이썬(분기 합산)과 다른 경로."""
    y, q = P
    def g(key, item):
        p = periods.get(key)
        return None if not p or p["fs_div"] != div else p["values"].get(item)
    def rc(key):
        p = periods.get(key)
        return p and p["api_rcept_no"]
    res = {"rcept": {"P": rc(P), "annual_prev": rc((y - 1, 4)), "P_prevyear": rc((y - 1, q)), "P_minus4_stock": rc((y - 1, q))}}
    for it in FLOW_ITEMS:
        res[it] = {"ytd_P": g(P, it), "annual_prev": g((y - 1, 4), it), "ytd_P_prevyear": g((y - 1, q), it)}
    for it in STOCK_ITEMS:
        res[it] = {"P": g(P, it), "P_prevyear": g((y - 1, q), it)}
    return res


def run() -> dict:
    cal = TradingCalendar.from_l0()
    px = Prices()
    sample = load_sample()
    results = []
    for row in sample:
        try:
            r = build_company(row, cal, px)
        except Exception as e:  # noqa: BLE001
            r = {"stock_code": row["stock_code"], "name": row["name"], "status": "error", "error": f"{type(e).__name__}: {e}"}
            log(f"!! {row['name']} 평가 실패: {e}")
        results.append(r)
        log(f"평가 {row['name']}: {r.get('status')} {r.get('period', '')} {r.get('flags', '')}")

    h = write_gz_json(L2 / "sample_eval.json.gz", results)
    cov = coverage(results)
    write_text(REPORTS / "phase1" / "coverage.json", json.dumps(cov, ensure_ascii=False, indent=1))
    write_text(REPORTS / "phase1" / "sample_eval.json", json.dumps(results, ensure_ascii=False, indent=1, default=str))
    # 유니버스 제외 이력 (3차 협의 #8: 제외 사실과 사유를 남긴다)
    import csv
    ex = REPORTS / "phase1" / "universe_exclusions.csv"
    ex.parent.mkdir(parents=True, exist_ok=True)
    with ex.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stock_code", "name", "status", "reason", "asof"])
        for r in results:
            if r.get("status") != "ok":
                w.writerow([r.get("stock_code"), r.get("name"), r.get("status"), "; ".join(r.get("flags", [])) or r.get("error", ""), r.get("asof", "")])
    from .workbook import build as build_wb
    build_wb(results, REPORTS / "phase1" / "verification_workbook.xlsx")
    return {"companies": len(results), "l2_sha256": h, "coverage": cov["metrics"]}


def coverage(results: list[dict]) -> dict:
    ok = [r for r in results if r.get("status") == "ok"]
    met = defaultdict(lambda: {"valid": 0, "invalid_reasons": Counter()})
    conf = defaultdict(Counter)
    for r in ok:
        for k, v in r["metrics"].items():
            if v is None:
                met[k]["invalid_reasons"][r["metric_reasons"].get(k) or "값 없음"] += 1
            else:
                met[k]["valid"] += 1
        for it, c in r["confidence_P"].items():
            conf[it][c] += 1
    fallback = Counter()
    for r in ok:
        for it, c in r["confidence_P"].items():
            if c not in ("id",):
                fallback[f"{it}:{c}"] += 1
    return {"evaluated": len(ok), "status": dict(Counter(r.get("status") for r in results)),
            "fallback_rules_applied": dict(sorted(fallback.items())),
            "interest_basis": dict(Counter(r.get("interest_basis") for r in ok)),
            "metrics": {k: {"valid": v["valid"], "rate": round(v["valid"] / max(len(ok), 1), 3),
                            "invalid_reasons": dict(v["invalid_reasons"])} for k, v in met.items()},
            "item_confidence": {k: dict(v) for k, v in conf.items()},
            "fscore_valid": sum(1 for r in ok if r.get("fscore") is not None)}


def determinism_check() -> dict:
    """같은 L0로 두 번 계산해 L2 해시 비교 (완료 기준 7)."""
    import importlib
    from . import pipeline as me
    h1 = sha256_bytes(canonical_json(me._eval_only()))
    importlib.reload(me)
    h2 = sha256_bytes(canonical_json(me._eval_only()))
    return {"run1": h1, "run2": h2, "identical": h1 == h2}


def _eval_only() -> list[dict]:
    cal = TradingCalendar.from_l0()
    px = Prices()
    return [build_company(row, cal, px) for row in load_sample()]
