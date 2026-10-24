"""피오트로스키 F-스코어 v1 (DESIGN 5.6). 비교는 TTM 당기 vs 1년 전 TTM.

입력 dict 키 (None 허용):
  ni, cfo, assets_begin, assets_end, lt_debt, avg_assets, cur_assets, cur_liab,
  shares, gross_profit, revenue
항목이 계산 불가하면 무효(0점 아님). 유효 항목 7개 미만이면 F-스코어 자체가 무효.
"""
from __future__ import annotations


def _r(a, b):
    return None if a is None or b is None or b == 0 else a / b


def items(cur: dict, prev: dict) -> dict[str, int | None]:
    roa_c = _r(cur.get("ni"), cur.get("assets_begin"))
    roa_p = _r(prev.get("ni"), prev.get("assets_begin"))
    out: dict[str, int | None] = {}
    out["1_roa_pos"] = None if roa_c is None else int(roa_c > 0)
    out["2_cfo_pos"] = None if cur.get("cfo") is None else int(cur["cfo"] > 0)
    out["3_roa_up"] = None if roa_c is None or roa_p is None else int(roa_c > roa_p)
    out["4_accrual"] = None if cur.get("cfo") is None or cur.get("ni") is None else int(cur["cfo"] > cur["ni"])
    lc, lp = cur.get("lt_debt"), prev.get("lt_debt")
    if lc is None or lp is None:
        out["5_leverage_down"] = None
    elif lc == 0 and lp == 0:
        out["5_leverage_down"] = 1
    else:
        a, b = _r(lc, cur.get("avg_assets")), _r(lp, prev.get("avg_assets"))
        out["5_leverage_down"] = None if a is None or b is None else int(a < b)
    cr_c, cr_p = _r(cur.get("cur_assets"), cur.get("cur_liab")), _r(prev.get("cur_assets"), prev.get("cur_liab"))
    out["6_liquidity_up"] = None if cr_c is None or cr_p is None else int(cr_c > cr_p)
    sc, sp = cur.get("shares"), prev.get("shares")
    out["7_no_dilution"] = None if sc is None or sp is None else int(sc <= sp)
    gm_c, gm_p = _r(cur.get("gross_profit"), cur.get("revenue")), _r(prev.get("gross_profit"), prev.get("revenue"))
    out["8_margin_up"] = None if gm_c is None or gm_p is None else int(gm_c > gm_p)
    at_c, at_p = _r(cur.get("revenue"), cur.get("avg_assets")), _r(prev.get("revenue"), prev.get("avg_assets"))
    out["9_turnover_up"] = None if at_c is None or at_p is None else int(at_c > at_p)
    return out


def score(cur: dict, prev: dict) -> tuple[float | None, dict]:
    it = items(cur, prev)
    valid = [v for v in it.values() if v is not None]
    if len(valid) < 7:
        return None, it
    return sum(valid) / len(valid) * 9, it
