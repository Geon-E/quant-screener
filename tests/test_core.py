"""1단계 계산 규칙 단위 테스트 (RESULT.md 완료 기준 2·3·7·8 일부)."""
import datetime as dt
import gzip
import math
import os

import pytest

from qs.collect.dart import parse_report_nm, report_versions
from qs.normalize.calendar import TradingCalendar
from qs.normalize.ttm import quarterly, ttm, ytd_from_row, yoy_ttm_growth
from qs.score import fscore
from qs.score.metrics import (INTEREST_COVERAGE_CAP, Winsor, book_yield, earnings_quality, ebitda_ev, ev,
                              interest_coverage, merge_preferred_mcap, momentum_12_1, percentile_ranks, roe)
from qs.util import scrub, to_num, write_gz_json


# ── 숫자 변환 ──
@pytest.mark.parametrize("raw,exp", [("1,234", 1234.0), ("-5", -5.0), ("(300)", -300.0), ("", None), ("-", None),
                                     (None, None), ("abc", None), (7, 7.0)])
def test_to_num(raw, exp):
    assert to_num(raw) == exp


# ── 보고서명 해석 ──
def test_parse_report_nm_dec():
    assert parse_report_nm("사업보고서 (2024.12)") == (2024, "11011", "최초")
    assert parse_report_nm("반기보고서 (2025.06)") == (2025, "11012", "최초")
    assert parse_report_nm("분기보고서 (2025.03)") == (2025, "11013", "최초")
    assert parse_report_nm("[기재정정]분기보고서 (2025.09)") == (2025, "11014", "기재정정")


def test_parse_report_nm_non_dec_is_none():
    assert parse_report_nm("사업보고서 (2025.03)", acc_mt="03") is None
    assert parse_report_nm("주요사항보고서(자기주식취득결정)") is None


def test_report_versions_sorted_and_grouped():
    fl = [{"report_nm": "[기재정정]사업보고서 (2024.12)", "rcept_no": "20250620000111", "rcept_dt": "20250620"},
          {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20250318001273", "rcept_dt": "20250318"}]
    v = report_versions(fl)[(2024, "11011")]
    assert [x["rcept_no"] for x in v] == ["20250318001273", "20250620000111"]
    assert v[0]["tag"] == "최초"


# ── TTM ──
def test_ytd_prefers_cumulative():
    assert ytd_from_row({"thstrm_amount": "100", "thstrm_add_amount": "250"}) == 250
    assert ytd_from_row({"thstrm_amount": "100", "thstrm_add_amount": ""}) == 100
    assert ytd_from_row({"thstrm_amount": "100"}) == 100


def test_quarterly_and_q4_derivation():
    ytd = {(2024, 1): 10, (2024, 2): 25, (2024, 3): 45, (2024, 4): 70}
    q = quarterly(ytd)
    assert q == {(2024, 1): 10, (2024, 2): 15, (2024, 3): 20, (2024, 4): 25}


def test_ttm_across_year_boundary():
    ytd = {(2024, 1): 10, (2024, 2): 25, (2024, 3): 45, (2024, 4): 70, (2025, 1): 12, (2025, 2): 30}
    q = quarterly(ytd)
    # 2025Q2 TTM = 2024Q3(20) + 2024Q4(25) + 2025Q1(12) + 2025Q2(18)
    assert ttm(q, 2025, 2) == 75


def test_ttm_missing_quarter_is_none():
    ytd = {(2024, 1): 10, (2024, 3): 45, (2024, 4): 70}  # 반기 누락 → 2024Q2, Q3 단독값 계산 불가
    q = quarterly(ytd)
    assert ttm(q, 2024, 4) is None


def test_yoy_growth_negative_base_is_none():
    q = {(2023, i): -5 for i in range(1, 5)} | {(2024, i): 10 for i in range(1, 5)}
    assert yoy_ttm_growth(q, 2024, 4) is None
    q2 = {(2023, i): 5 for i in range(1, 5)} | {(2024, i): 10 for i in range(1, 5)}
    assert yoy_ttm_growth(q2, 2024, 4) == pytest.approx(1.0)


# ── 시점 정합성: 공시 다음 거래일 ──
def test_next_trading_day_skips_holiday_and_weekend():
    days = [dt.date(2025, 10, 10), dt.date(2025, 10, 13), dt.date(2025, 10, 14)]  # 금, 월, 화
    cal = TradingCalendar(days)
    assert cal.next_trading_day(dt.date(2025, 10, 10)) == (dt.date(2025, 10, 13), True)
    assert cal.next_trading_day(dt.date(2025, 10, 11)) == (dt.date(2025, 10, 13), True)  # 토요일 공시


def test_next_trading_day_outside_calendar_flags_approx():
    cal = TradingCalendar([dt.date(2025, 10, 13)])
    d, exact = cal.next_trading_day(dt.date(2020, 1, 3))  # 금요일
    assert d == dt.date(2020, 1, 6) and exact is False


# ── 시가총액 우선주 합산 ──
def test_merge_preferred_mcap():
    price = [{"ISU_CD": "005930", "MKTCAP": "400"}, {"ISU_CD": "005935", "MKTCAP": "50"},
             {"ISU_CD": "005380", "MKTCAP": "40"}, {"ISU_CD": "005385", "MKTCAP": "3"}, {"ISU_CD": "005387", "MKTCAP": "4"},
             {"ISU_CD": "000001", "MKTCAP": "9"}]
    base = [{"ISU_SRT_CD": "005930", "KIND_STKCERT_TP_NM": "보통주"}, {"ISU_SRT_CD": "005935", "KIND_STKCERT_TP_NM": "구형우선주"},
            {"ISU_SRT_CD": "005380", "KIND_STKCERT_TP_NM": "보통주"}, {"ISU_SRT_CD": "005385", "KIND_STKCERT_TP_NM": "구형우선주"},
            {"ISU_SRT_CD": "005387", "KIND_STKCERT_TP_NM": "신형우선주"}, {"ISU_SRT_CD": "000001", "KIND_STKCERT_TP_NM": "신형우선주"}]
    m = merge_preferred_mcap(price, base)
    assert m["005930"]["mcap_total"] == 450
    assert m["005380"]["mcap_total"] == 47 and sorted(m["005380"]["pref_codes"]) == ["005385", "005387"]
    assert "000001" not in m  # 짝 없는 우선주는 보통주로 취급하지 않음


# ── 지표 유효성 (무효 ≠ 0) ──
def test_book_yield_impaired_is_invalid():
    m = book_yield(-10, 100)
    assert m.value is None and m.reason == "자본잠식"


def test_roe_small_denominator_invalid():
    assert roe(5, 0.5, 0.5, 1000).reason == "분모 불안정"
    assert roe(10, 90, 110, 1000).value == pytest.approx(0.1)


def test_ebitda_ev_rules():
    assert ebitda_ev(10, 2, -5, 100).reason == "EV 비정상"
    assert ebitda_ev(10, 2, 5, 100).reason == "EV 비정상"  # EV < 시총 10%
    assert ebitda_ev(10, None, 100, 100).reason == "EBITDA 구성 계정 없음"
    assert ebitda_ev(10, 2, 120, 100).value == pytest.approx(0.1)
    assert ev(100, 30, 20, 5) == 105


def test_earnings_quality_and_coverage():
    assert earnings_quality(10, -1).value is None
    assert interest_coverage(50, 0).value == INTEREST_COVERAGE_CAP
    assert interest_coverage(50, -10).value == 5


def test_momentum_requires_verified_adjustment():
    prices = [100.0] * 300
    assert momentum_12_1(prices, adjustment_verified=False).reason == "수정주가 미검증"
    prices2 = [100.0] * 47 + [110.0] * 253
    prices2[-253] = 100.0
    m = momentum_12_1(prices2, adjustment_verified=True)
    assert m.value == pytest.approx(0.1)


def test_percentile_ties_and_invalid():
    r = percentile_ranks({"a": 1.0, "b": 2.0, "c": 2.0, "d": None, "e": 3.0})
    assert r["a"] == 0 and r["e"] == 100 and r["b"] == r["c"] == 50 and r["d"] is None


def test_winsor():
    w = Winsor.fit(list(range(101)), p=0.01)
    assert w.apply(-50) == 1 and w.apply(500) == 99 and w.apply(None) is None


# ── F-스코어 ──
def test_fscore_invalid_when_too_few_items():
    s, it = fscore.score({"ni": 1}, {})
    assert s is None


def test_fscore_full_marks():
    prev = dict(ni=5, cfo=4, assets_begin=100, lt_debt=20, avg_assets=100, cur_assets=50, cur_liab=50,
                shares=100, gross_profit=20, revenue=100)
    cur = dict(ni=10, cfo=15, assets_begin=100, lt_debt=10, avg_assets=100, cur_assets=60, cur_liab=50,
               shares=100, gross_profit=30, revenue=120)
    s, it = fscore.score(cur, prev)
    assert s == 9 and all(v == 1 for v in it.values())


def test_fscore_missing_account_not_zero():
    prev = dict(ni=5, cfo=4, assets_begin=100, avg_assets=100, cur_assets=50, cur_liab=50, shares=100,
                gross_profit=20, revenue=100)
    cur = dict(ni=10, cfo=15, assets_begin=100, avg_assets=100, cur_assets=60, cur_liab=50, shares=100,
               gross_profit=30, revenue=120)
    s, it = fscore.score(cur, prev)
    assert it["5_leverage_down"] is None and s == 9  # 8개 유효 전부 1점 → 9점 환산


# ── 보안·결정성 ──
def test_scrub(monkeypatch):
    monkeypatch.setenv("KRX_API_KEY", "SECRET123")
    assert scrub({"u": "x?k=SECRET123"}) == {"u": "x?k=***"}


def test_gz_json_deterministic(tmp_path):
    a, b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    h1 = write_gz_json(a, {"b": 1, "a": [1, 2]})
    h2 = write_gz_json(b, {"a": [1, 2], "b": 1})
    assert h1 == h2 and a.read_bytes() == b.read_bytes()
