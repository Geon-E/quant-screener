"""분기값 복원과 TTM (DESIGN 4.3).

입력은 보고서별 '연초부터 누적(YTD)' 값이다.
  - 손익: 1분기 thstrm_amount, 반기·3분기 thstrm_add_amount(누적), 사업보고서 thstrm_amount(연간)
  - 현금흐름: 분기·반기 보고서도 누적으로 표시되므로 thstrm_amount가 곧 YTD
  → 규칙: thstrm_add_amount가 있으면 그것, 없으면 thstrm_amount를 YTD로 본다 (ytd_from_row).
분기값: Q1 = YTD1, Q2 = YTD2 − YTD1, Q3 = YTD3 − YTD2, Q4 = 연간 − YTD3
TTM(Y, q) = 최근 연속 4개 분기 합. 하나라도 빠지면 None (무효).
"""
from __future__ import annotations

from ..util import to_num

REPRT_Q = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def ytd_from_row(row: dict) -> float | None:
    add = to_num(row.get("thstrm_add_amount"))
    return add if add is not None else to_num(row.get("thstrm_amount"))


def quarterly(ytd: dict[tuple[int, int], float | None]) -> dict[tuple[int, int], float | None]:
    """{(연도, 분기): YTD} → {(연도, 분기): 해당 분기 단독 값}."""
    out = {}
    for (y, q), v in ytd.items():
        if v is None:
            out[(y, q)] = None
        elif q == 1:
            out[(y, q)] = v
        else:
            prev = ytd.get((y, q - 1))
            out[(y, q)] = None if prev is None else v - prev
    return out


def prev_q(y: int, q: int) -> tuple[int, int]:
    return (y, q - 1) if q > 1 else (y - 1, 4)


def ttm(qv: dict[tuple[int, int], float | None], y: int, q: int) -> float | None:
    total, key = 0.0, (y, q)
    for _ in range(4):
        v = qv.get(key)
        if v is None:
            return None
        total += v
        key = prev_q(*key)
    return total


def yoy_ttm_growth(qv: dict, y: int, q: int) -> float | None:
    """TTM 전년동기 대비 증감률. 기준값이 0 이하이면 None (부호가 바뀌면 증감률이 무의미)."""
    cur, prev = ttm(qv, y, q), ttm(qv, y - 1, q)
    if cur is None or prev is None or prev <= 0:
        return None
    return cur / prev - 1
