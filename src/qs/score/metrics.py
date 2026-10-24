"""지표 계산과 유효성 규칙 (DESIGN 4.4~4.6, 5.3). 무효는 None + 사유 — 0점으로 치환하지 않는다."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Metric:
    value: float | None
    reason: str | None = None  # 무효 사유

    @property
    def valid(self) -> bool:
        return self.value is not None and math.isfinite(self.value)


def _div(a, b) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


# ── 시가총액: 보통주 + 같은 회사 우선주 (DESIGN 4.4) ──
def merge_preferred_mcap(price_rows: list[dict], base_rows: list[dict]) -> dict[str, dict]:
    """KRX 일별시세 + 종목기본정보 → 보통주 코드별 {mcap_common, mcap_pref, mcap_total, pref_codes}.
    우선주 판정은 종목기본정보의 주식종류(KIND_STKCERT_TP_NM)로 하고, 연결은 종목코드 앞 5자리 + '0'."""
    kind = {b["ISU_SRT_CD"]: b.get("KIND_STKCERT_TP_NM", "") for b in base_rows}
    mcap = {r["ISU_CD"]: float(r["MKTCAP"]) for r in price_rows if r.get("MKTCAP") not in (None, "", "-")}
    out: dict[str, dict] = {}
    for code, v in mcap.items():
        if kind.get(code) == "보통주":
            out.setdefault(code, {"mcap_common": 0.0, "mcap_pref": 0.0, "pref_codes": []})["mcap_common"] = v
    for code, v in mcap.items():
        k = kind.get(code, "")
        if "우선주" in k:
            common = code[:5] + "0"
            if common in out:
                out[common]["mcap_pref"] += v
                out[common]["pref_codes"].append(code)
    for v in out.values():
        v["mcap_total"] = v["mcap_common"] + v["mcap_pref"]
    return out


# ── 개별 지표 ──
def earnings_yield(ni_owners_ttm, mcap) -> Metric:
    v = _div(ni_owners_ttm, mcap)
    return Metric(v, None if v is not None else "분모·분자 없음")


def book_yield(equity_owners, mcap) -> Metric:
    if equity_owners is None or mcap is None:
        return Metric(None, "데이터 없음")
    if equity_owners <= 0:
        return Metric(None, "자본잠식")
    return Metric(equity_owners / mcap)


def ev(mcap, interest_bearing_debt, cash, st_fin) -> float | None:
    if mcap is None or interest_bearing_debt is None or cash is None:
        return None
    return mcap + interest_bearing_debt - cash - (st_fin or 0.0)


def ebitda_ev(op_income_ttm, dep_ttm, ev_value, mcap) -> Metric:
    if op_income_ttm is None or dep_ttm is None:
        return Metric(None, "EBITDA 구성 계정 없음")
    if ev_value is None or mcap is None:
        return Metric(None, "EV 계산 불가")
    if ev_value <= 0 or ev_value < 0.1 * mcap:
        return Metric(None, "EV 비정상")
    return Metric((op_income_ttm + dep_ttm) / ev_value)


def roe(ni_owners_ttm, eq_begin, eq_end, mcap) -> Metric:
    if ni_owners_ttm is None or eq_end is None:
        return Metric(None, "데이터 없음")
    if eq_begin is None:
        return Metric(None, "기초 잔액 없음")  # 기말만으로 대체하지 않음 (조용한 대체 금지)
    if eq_end <= 0 or eq_begin <= 0:
        return Metric(None, "자본잠식")
    avg = (eq_begin + eq_end) / 2
    if mcap and avg < 0.01 * mcap:
        return Metric(None, "분모 불안정")
    return Metric(ni_owners_ttm / avg)


def gp_a(gross_profit_ttm, assets_begin, assets_end) -> Metric:
    if gross_profit_ttm is None or assets_end is None:
        return Metric(None, "데이터 없음")
    if assets_begin is None:
        return Metric(None, "기초 잔액 없음")
    avg = (assets_begin + assets_end) / 2
    if avg <= 0:
        return Metric(None, "자산 비정상")
    return Metric(gross_profit_ttm / avg)


def earnings_quality(cfo_ttm, ni_ttm) -> Metric:
    if cfo_ttm is None or ni_ttm is None:
        return Metric(None, "데이터 없음")
    if ni_ttm <= 0:
        return Metric(None, "순이익 ≤ 0")
    return Metric(cfo_ttm / ni_ttm)


INTEREST_COVERAGE_CAP = 100.0


def interest_coverage(op_income_ttm, interest_ttm) -> Metric:
    if op_income_ttm is None or interest_ttm is None:
        return Metric(None, "데이터 없음")
    if interest_ttm == 0:
        return Metric(INTEREST_COVERAGE_CAP, None)  # 무차입 = 우량 → 상한값 (DESIGN 4.6)
    return Metric(min(op_income_ttm / abs(interest_ttm), INTEREST_COVERAGE_CAP))


def momentum_12_1(adj_closes: list[float | None], adjustment_verified: bool) -> Metric:
    """adj_closes: 기준일까지 일별 수정종가 (오래된 → 최신), 최소 253 거래일.
    수정주가 보정이 검증되지 않았으면 무효 (D-16)."""
    if not adjustment_verified:
        return Metric(None, "수정주가 미검증")
    if len(adj_closes) < 253:
        return Metric(None, "가격 이력 부족(13개월 미만)")
    p_12 = adj_closes[-253]
    p_1 = adj_closes[-22]
    if not p_12 or not p_1 or p_12 <= 0:
        return Metric(None, "가격 결측")
    return Metric(p_1 / p_12 - 1)


@dataclass
class Winsor:
    lo: float
    hi: float

    @classmethod
    def fit(cls, values: list[float], p: float = 0.01) -> "Winsor":
        xs = sorted(v for v in values if v is not None and math.isfinite(v))
        if not xs:
            return cls(-math.inf, math.inf)
        def q(a):
            i = (len(xs) - 1) * a
            lo, hi = math.floor(i), math.ceil(i)
            return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)
        return cls(q(p), q(1 - p))

    def apply(self, v):
        return None if v is None else min(max(v, self.lo), self.hi)


def percentile_ranks(values: dict[str, float | None]) -> dict[str, float | None]:
    """유효값끼리 백분위(0~100), 동점은 평균 순위. 무효는 None 유지."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    out = {k: None for k in values}
    if n == 0:
        return out
    if n == 1:
        out[items[0][1]] = 50.0
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        rank = (i + j) / 2
        for t in range(i, j + 1):
            out[items[t][1]] = rank / (n - 1) * 100
        i = j + 1
    return out
