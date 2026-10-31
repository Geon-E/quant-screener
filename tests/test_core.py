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


# ── 계정 표준화 ──
from qs.normalize.accounts import canon, extract


def _r(sj, aid, nm, amt, add=None, ord_=1):
    return {"sj_div": sj, "account_id": aid, "account_nm": nm, "thstrm_amount": amt,
            "thstrm_add_amount": add, "ord": str(ord_)}


def test_canon_prefix():
    assert canon("ifrs_Revenue") == canon("ifrs-full_Revenue") == "ifrs:Revenue"


def test_extract_ids_and_owners_by_id():
    rows = [_r("CIS", "ifrs-full_Revenue", "매출액", "1000", ord_=1),
            _r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=2),
            _r("CIS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", "80", ord_=3),
            _r("CIS", "ifrs-full_ComprehensiveIncomeAttributableToOwnersOfParent", "지배기업 소유주지분", "120", ord_=9),
            _r("BS", "ifrs-full_Equity", "자본총계", "500", ord_=1),
            _r("BS", "ifrs-full_EquityAttributableToOwnersOfParent", "지배기업 소유주지분", "450", ord_=2)]
    x = extract(rows, "CFS")
    assert x["revenue"].value == 1000 and x["ni_owners"].value == 80 and x["ni_owners"].confidence == "id"
    assert x["equity_owners"].value == 450


def test_extract_owners_ordinal_not_comprehensive():
    ns = "-표준계정코드 미사용-"
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=10),
            _r("CIS", ns, "지배기업소유주지분", "70", ord_=11),
            _r("CIS", ns, "비지배지분", "30", ord_=12),
            _r("CIS", ns, "총포괄손익", "150", ord_=13),
            _r("CIS", ns, "지배기업소유주지분", "110", ord_=14)]
    x = extract(rows, "CFS")
    assert x["ni_owners"].value == 70 and x["ni_owners"].confidence == "ordinal"


def test_extract_cfs_without_owner_line_derives_from_identity():
    # 비지배지분 행은 있는데 지배주주 행이 없음 → 전체·비지배 모두 표준 ID면 지배 = 전체 − 비지배 (신뢰도 derived)
    # (2026-10-25 변경: 이전에는 무효 처리. JW중외제약 사례 — 5차 협의 공유)
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1),
            _r("CIS", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", "20", ord_=3)]
    x = extract(rows, "CFS")["ni_owners"]
    assert x.value == 80 and x.confidence == "derived"


def test_derive_requires_standard_id_total():
    # 전체가 비표준 이름으로만 잡히면 계산하지 않는다 (무효)
    rows = [_r("CIS", "-표준계정코드 미사용-", "당기순이익", "100", ord_=1),
            _r("CIS", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", "20", ord_=3)]
    assert extract(rows, "CFS")["ni_owners"].confidence != "derived"


def test_extract_cfs_no_nci_means_total_is_owners():
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1),
            _r("BS", "ifrs-full_Equity", "자본총계", "500", ord_=1)]
    x = extract(rows, "CFS")
    assert x["ni_owners"].value == 100 and x["ni_owners"].confidence == "no_nci"
    assert x["equity_owners"].value == 500 and x["equity_owners"].confidence == "no_nci"


def test_extract_separate_only_owner_equals_total():
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1), _r("BS", "ifrs-full_Equity", "자본총계", "500", ord_=1)]
    x = extract(rows, "OFS")
    assert x["ni_owners"].value == 100 and x["ni_owners"].confidence == "separate_only"
    assert x["equity_owners"].value == 500


def test_extract_debt_sum_and_exclusions():
    ns = "-표준계정코드 미사용-"
    rows = [_r("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100", ord_=1),
            _r("BS", "dart_LongTermBorrowingsGross", "장기차입금", "200", ord_=2),
            _r("BS", "ifrs-full_CurrentLeaseLiabilities", "리스부채", "10", ord_=3),
            _r("BS", ns, "사채할인발행차금", "-5", ord_=4),
            _r("BS", ns, "교환사채", "50", ord_=5)]
    x = extract(rows, "CFS")
    assert x["debt"].value == 360  # 100+200+10+50, 할인차금 제외
    assert extract([], "CFS")["debt"].value == 0 and extract([], "CFS")["debt"].confidence == "zero_default"


def test_extract_interest_paid_abs_and_hybrid_excluded():
    rows = [_r("CF", "ifrs-full_InterestPaidClassifiedAsOperatingActivities", "이자의 지급", "-30", ord_=1),
            _r("CF", "ifrs-full_InterestPaidClassifiedAsFinancingActivities", "신종자본증권 이자지급", "-7", ord_=2)]
    assert extract(rows, "CFS")["interest_paid"].value == 30


def test_extract_flow_uses_cumulative():
    rows = [_r("CIS", "ifrs-full_Revenue", "매출액", "300", add="900", ord_=1)]
    assert extract(rows, "CFS")["revenue"].value == 900


def test_roe_gpa_require_begin_balance():
    from qs.score.metrics import gp_a
    assert roe(10, None, 100, 1000).reason == "기초 잔액 없음"
    assert gp_a(10, None, 100).reason == "기초 잔액 없음"


# ── 3차 협의 반영 규칙 ──
def test_zero_default_only_when_no_related_names():
    ns = "-표준계정코드 미사용-"
    # 관련 명칭 없음 → 0 (무차입)
    assert extract([_r("BS", "ifrs-full_Equity", "자본총계", "500")], "CFS")["debt"].confidence == "zero_default"
    # 차입금 명칭은 있는데 매핑 규칙에 없는 ID → 무효
    rows = [_r("BS", "ifrs-full_OtherCurrentFinancialLiabilities", "기타차입금", "300")]
    x = extract(rows, "CFS")["debt"]
    assert x.value is None and x.confidence == "unmapped"


def test_no_nci_not_applied_when_owner_named_row_exists():
    ns = "-표준계정코드 미사용-"
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1),
            _r("CIS", ns, "총포괄손익", "150", ord_=2),
            _r("CIS", ns, "지배기업의 소유주", "140", ord_=3)]  # 포괄손익 귀속행만 있음 → 순이익 귀속을 모름
    x = extract(rows, "CFS")["ni_owners"]
    assert x.value is None


def test_separate_only_blocked_if_company_had_cfs():
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1)]
    assert extract(rows, "OFS", had_cfs=False)["ni_owners"].confidence == "separate_only"
    assert extract(rows, "OFS", had_cfs=True)["ni_owners"].value is None


def test_ordinal_variations():
    ns = "-표준계정코드 미사용-"
    # 비지배가 먼저 나와도 지배기업 행을 고른다
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1),
            _r("CIS", ns, "비지배지분", "30", ord_=2),
            _r("CIS", ns, "지배주주지분", "70", ord_=3)]
    assert extract(rows, "CFS")["ni_owners"].value == 70
    # 순이익 다음에 바로 포괄손익 구간이 시작되면 그 뒤 '지배기업' 행은 채택하지 않음
    rows2 = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1),
             _r("CIS", ns, "기타포괄손익", "50", ord_=2),
             _r("CIS", ns, "지배기업소유주지분", "140", ord_=3),
             _r("CIS", ns, "비지배지분", "10", ord_=4)]
    assert extract(rows2, "CFS")["ni_owners"].value is None


def test_debt_same_id_counted_once():
    rows = [_r("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100", ord_=1),
            _r("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100", ord_=7)]  # 같은 ID 반복(소계 표시 등)
    assert extract(rows, "CFS")["debt"].value == 100


def test_id_matched_blank_amount_is_zero_tagged():
    rows = [_r("BS", "dart_LongTermBorrowingsGross", "장기차입금", "-")]
    x = extract(rows, "CFS")["lt_debt"]
    assert x.value == 0 and x.confidence == "id_blank"


def test_lt_debt_guard_ignores_current_ids():
    rows = [_r("BS", "dart_CurrentPortionOfConvertibleBonds", "전환사채", "50")]
    x = extract(rows, "CFS")
    assert x["lt_debt"].confidence == "zero_default" and x["debt"].value == 50


def test_new_debt_ids():
    rows = [_r("BS", "dart_CurrentLoansReceived", "단기 금융기관 차입금(사채 제외)", "10"),
            _r("BS", "dart_NoncurrentLoansReceived", "장기 금융기관 차입금(사채 제외)", "20"),
            _r("BS", "dart_NoncurrentBondsIssued", "장기 사채", "30")]
    x = extract(rows, "CFS")
    assert x["debt"].value == 60 and x["lt_debt"].value == 50


# ── 수정주가 (D-16) ──
from qs.adjust import adjusted, classify, detect_events, verified_for


def _day(d, close, cmp, shares, fluc=None):
    return {"d": d, "close": close, "cmp": cmp, "fluc": fluc, "shares": shares, "name": "X", "mkt": "KOSPI"}


def test_split_detected_and_adjusted():
    # 5:1 액면분할: 전일 종가 10000 → 기준가 2000, 당일 2100(+100)
    s = {"A": [_day("20250101", 10000, 0, 100), _day("20250102", 2100, 100, 500), _day("20250103", 2200, 100, 500)]}
    ev = detect_events(s)
    assert len(ev) == 1 and ev[0]["class"] == "주식분할" and abs(ev[0]["factor"] - 0.2) < 1e-12
    adj = adjusted(s["A"], ev)
    assert adj[0][1] == pytest.approx(2000) and adj[1][1] == 2100 and adj[2][1] == 2200
    assert adj[1][1] / adj[0][1] - 1 == pytest.approx(0.05)


def test_reverse_split_and_rights():
    assert classify(2.0, 0.5) == "주식병합"
    assert classify(0.93, 1.0) == "권리락(증자·주식배당 등)"
    assert classify(1.0005, 1.0) == "미세 기준가 조정"
    assert classify(1.47, 0.65) == "인적분할·감자 등 복합"


def test_no_event_when_base_equals_prev_close():
    s = {"A": [_day("20250101", 1000, 0, 100), _day("20250102", 1010, 10, 100)]}
    assert detect_events(s) == []


def test_verified_requires_confirmation_for_rights():
    ev = [{"code": "A", "date": "20250601", "class": "권리락(증자·주식배당 등)"},
          {"code": "A", "date": "20250301", "class": "주식분할"}]
    ok, rel = verified_for("A", ev, "20250101", "20251231")
    assert ok is False and len(rel) == 2
    ev[0]["confirmed_by"] = "DART 공시"
    assert verified_for("A", ev, "20250101", "20251231")[0] is True


# ── 원천값 자동 대조 (D-22 변경) ──
from qs.xcheck import compare, parse_dividend_ni, parse_main


def test_compare_statuses():
    assert compare(100.0, 100.0)[0] == "정확 일치"
    assert compare(1_000_400_000.0, 1_000_000_000.0, unit_tol=1e6)[0] == "정확 일치"   # 백만원 반올림
    assert compare(1005.0, 1000.0)[0] == "1% 이내"
    assert compare(1100.0, 1000.0) == ("불일치", 10.0)
    assert compare(None, 5.0)[0] == "우리값 없음"
    assert compare(5.0, None)[0] == "외부값 없음"
    assert compare(0.0, 0.0)[0] == "정확 일치"
    assert compare(-990.0, -1000.0)[0] == "1% 이내"     # 적자도 절대값 기준


def test_parse_main_fs_div_and_ytd():
    rows = [{"fs_div": "OFS", "account_nm": "매출액", "thstrm_amount": "1"},
            {"fs_div": "CFS", "account_nm": "매출액", "thstrm_amount": "30", "thstrm_add_amount": "100"},
            {"fs_div": "CFS", "account_nm": "당기순이익(손실)", "thstrm_amount": "7"},
            {"fs_div": "CFS", "account_nm": "자본 총계", "thstrm_amount": "50", "thstrm_add_amount": "999"}]
    v = parse_main(rows, "CFS")
    assert v["revenue"] == 100      # 반기·3분기는 누적
    assert v["ni_total"] == 7
    assert v["equity_total"] == 50  # 재무상태표는 기말 잔액
    assert "op_income" not in v


def test_parse_dividend_ni_unit_and_basis():
    rows = [{"se": "(별도)당기순이익(백만원)", "thstrm": "200"},
            {"se": "(연결)당기순이익(백만원)", "thstrm": "1,234"}]
    assert parse_dividend_ni(rows, "CFS")[:2] == (1_234_000_000.0, 1e6)
    assert parse_dividend_ni(rows, "OFS")[0] == 200_000_000.0
    assert parse_dividend_ni([{"se": "현금배당금총액(백만원)", "thstrm": "5"}], "CFS")[0] is None


def test_explain_dividend_mismatch():
    from qs.xcheck import explain_dividend_mismatch
    r = {"status": "불일치", "ours": -2_741_692_510.0, "external": -2_741_692_510.0 * 1e6, "note": ""}
    explain_dividend_mismatch(r, -2_741_692_510.0, 1e6)
    assert r["status"] == "설명됨: 회사 단위 표기 오류"
    r = {"status": "불일치", "ours": -7_170_811_062.0, "external": 4_191_000_000.0, "note": ""}
    explain_dividend_mismatch(r, 4_191_427_824.0, 1e6)
    assert r["status"] == "설명됨: 회사가 전체 순이익 기재"
    r = {"status": "불일치", "ours": 100.0, "external": 5e6, "note": ""}
    explain_dividend_mismatch(r, 7e6, 1e6)
    assert r["status"] == "불일치"      # 설명 안 되면 그대로


def test_parse_dividend_ni_ofs_fallback_to_consolidated_cell():
    rows = [{"se": "(연결)당기순이익(백만원)", "thstrm": "-4,646"}, {"se": "(별도)당기순이익(백만원)", "thstrm": "-"}]
    v, unit, se = parse_dividend_ni(rows, "OFS")
    assert v == -4_646_000_000.0 and "연결 칸 사용" in se
    assert parse_dividend_ni([{"se": "(연결)당기순이익(백만원)", "thstrm": "-"}], "CFS")[0] is None


# ── 관리종목·상장폐지 실험 (D-15·D-19) ──
from qs.exp_listing import classify_title, state_on, windows


@pytest.mark.parametrize("t,exp", [
    ("관리종목지정(감사의견거절)", "designate"),
    ("[정정]관리종목 지정(자본잠식률 50% 이상)", "designate"),
    ("관리종목지정해제", "release"),
    ("관리종목 지정 해제(사유해소)", "release"),
    ("관리종목 지정우려 예고", None),
    ("상장폐지결정", "delist"),
    ("상장폐지 우려 예고", None),
    ("주권매매거래정지", None),
])
def test_classify_title(t, exp):
    assert classify_title(t) == exp


def test_state_on_sequence():
    ev = [("20250110", "designate", ""), ("20250601", "release", ""), ("20250901", "designate", "")]
    assert state_on(ev, "20250109") is False
    assert state_on(ev, "20250110") is True
    assert state_on(ev, "20250701") is False
    assert state_on(ev, "20251001") is True
    assert state_on(sorted([("20250110", "release", ""), ("20250110", "designate", "")]), "20250110") is False


def test_windows_cover_range_without_gaps():
    w = list(windows(dt.date(2015, 1, 1), dt.date(2015, 8, 15)))
    assert w[0] == (dt.date(2015, 1, 1), dt.date(2015, 3, 31))
    assert w[-1] == (dt.date(2015, 7, 1), dt.date(2015, 8, 15))
    assert all(b[0] == a[1] + dt.timedelta(days=1) for a, b in zip(w, w[1:]))


# ── KRX 빈 응답 보호 (2026-10-25 코스피 10/23 소실 사례) ──
def test_krx_empty_refetch_keeps_good_file(tmp_path, monkeypatch):
    from qs import storage
    from qs.collect import krx as K
    monkeypatch.setattr(storage, "L0", tmp_path)
    monkeypatch.setattr(K, "krx_path", lambda api, bd: tmp_path / api.replace("/", "_") / f"{bd}.json.gz")
    k = K.KRX(budget=10)
    seq = [[{"ISU_CD": "005930"}], []]
    monkeypatch.setattr(k, "fetch", lambda api, bd: seq.pop(0))
    assert k.collect("sto/stk_bydd_trd", "20261023") == 1
    assert k.collect("sto/stk_bydd_trd", "20261023", force=True) == 1     # 빈 재수집 → 유지
    assert k.kept_nonempty == ["sto/stk_bydd_trd 20261023"]
    # 의심 빈 응답 판정: 코스피 0행 + 코스닥 행 있음
    K.save_l0(K.krx_path("sto/stk_bydd_trd", "20261026"), source={"rows": 0}, payload=[])
    K.save_l0(K.krx_path("sto/ksq_bydd_trd", "20261026"), source={"rows": 5}, payload=[1])
    assert K.suspicious_empty("20261026") is True
    K.save_l0(K.krx_path("sto/ksq_bydd_trd", "20261026"), source={"rows": 0}, payload=[])
    assert K.suspicious_empty("20261026") is False                       # 둘 다 비면 휴장일


def test_parse_robots_rejects_firewall_page():
    from qs.exp_listing import parse_robots
    waf = "<br>\r\n<br>\r\n<center>\r\n<h2>\r\nThe request / response that are contrary to the Web firewall security policies have been blocked.</h2>"
    assert parse_robots(200, "text/html", waf, "/investwarn/adminissue.do")[0] is None
    assert parse_robots(200, "text/plain", "User-agent: *\nDisallow: /investwarn/\n", "/investwarn/adminissue.do")[0] is False
    assert parse_robots(200, "text/plain", "User-agent: *\nDisallow:\n", "/investwarn/adminissue.do")[0] is True
    assert parse_robots(404, "", "", "/x")[0] is True
    assert parse_robots(403, "", "", "/x")[0] is None


# ── 스크리너 (방침 B) ──
from qs.screen import group_of, load_groups, pbr_roe_residuals, reliability, score_group


def test_group_longest_prefix_and_holding_company():
    g = load_groups()
    assert group_of("261", g)[0] == "G12"          # 반도체
    assert group_of("26110", g)[0] == "G12"
    assert group_of("262", g)[0] == "G13"          # 전자부품
    assert group_of("64992", g)[0] == "G30"        # 지주회사는 금융업(64)에서 분리
    assert group_of("66121", g)[0] == "EX"         # 증권 → 금융업 제외
    assert group_of("5821", g)[0] == "G27" and group_of("581", g)[0] == "G26"
    assert group_of(None, g) == (None, None)
    assert [reliability(n) for n in (20, 12, 3)] == ["높음", "보통", "낮음"]


def _sr(code, **m):
    base = {k: None for k in ["E/P", "B/P", "EBIT/EV", "ROE", "GP/A", "CFO/NI", "12-1모멘텀", "영업이익증가율", "이자보상배율"]}
    base.update(m)
    return {"stock_code": code, "metrics": base, "exclude": []}


def test_score_group_minimums_and_no_fill():
    rows = [_sr("A", **{"E/P": .1, "B/P": .5, "ROE": .1, "GP/A": .2, "12-1모멘텀": .1, "이자보상배율": 5}),
            _sr("B", **{"E/P": .05, "B/P": .3, "ROE": .05, "GP/A": .1, "12-1모멘텀": -.1, "이자보상배율": 2}),
            _sr("C", **{"EBIT/EV": .1, "ROE": .2, "GP/A": .3, "12-1모멘텀": .2, "이자보상배율": 9})]   # E/P·B/P 모두 없음
    score_group(rows)
    a, b, c = rows
    assert a["composite"] is not None and a["composite"] > b["composite"]
    assert c["composite"] is None and any("가치" in x for x in c["exclude"])   # 가치 최소조건 미충족 → 순위 제외
    assert a["pct"]["CFO/NI"] is None                                        # 결측은 결측 (50점 채움 없음)


def test_pbr_roe_residual_needs_15():
    rows = [_sr(str(i), **{"B/P": 1 / (1 + i * .1), "ROE": .02 * i}) for i in range(10)]
    assert all(v is None for v in pbr_roe_residuals(rows).values())
    # log(PBR) = 0.2 + 3·ROE ± 0.1 (홀수 = 예상보다 비쌈)
    rows = [_sr(str(i), **{"B/P": 1 / math.exp(0.2 + 3 * .01 * i + (0.1 if i % 2 else -0.1)), "ROE": .01 * i}) for i in range(20)]
    res = pbr_roe_residuals(rows)
    assert sum(v is not None for v in res.values()) == 20
    assert res["1"] < 0 < res["2"]        # 예상보다 PBR 높음(비쌈) → 음수, 낮음(쌈) → 양수


def test_update_recent_adds_new_and_refetches_amended(tmp_path, monkeypatch):
    from qs import storage, universe as U
    monkeypatch.setattr(storage, "L0", tmp_path)
    monkeypatch.setattr(U, "dart_path", lambda kind, *p: tmp_path / kind / os.path.join(*p))
    monkeypatch.setattr(U, "exists", lambda p: p.exists())
    cc = "00126380"
    U.save_l0(U.dart_path("list", f"{cc}.json.gz"), source={}, payload=[
        {"rcept_no": "20260814000001", "rcept_dt": "20260814", "report_nm": "반기보고서 (2026.06)"}])
    U.save_l0(U.dart_path("fs", cc, "2026_11012_CFS.json.gz"), source={}, payload={"status": "000", "list": [{"a": 1}]})
    recent = [{"corp_code": cc, "rcept_no": "20261020000002", "rcept_dt": "20261020", "report_nm": "[기재정정]반기보고서 (2026.06)"},
              {"corp_code": cc, "rcept_no": "20261114000003", "rcept_dt": "20261114", "report_nm": "분기보고서 (2026.09)"},
              {"corp_code": "99999999", "rcept_no": "20261114000009", "rcept_dt": "20261114", "report_nm": "분기보고서 (2026.09)"}]
    calls = []

    class D:
        def _json(self, path, params):
            calls.append((path, params.get("fs_div")))
            if path == "list.json":
                return {"status": "000", "list": recent, "total_page": 1}
            return {"status": "000", "list": [{"b": 2}]}
    r = U.update_recent(D(), {cc})
    assert r["new_reports"] == 1 and r["amended"] == 1 and r["fs_refetched"] == 1
    assert ("fnlttSinglAcntAll.json", "CFS") in calls                      # 정정본 재무 다시 받음
    assert len(U.load_l0(U.dart_path("list", f"{cc}.json.gz"))["payload"]) == 3


# ── 백테스트 (2단계) ──
from qs import backtest as BT


def test_rebalance_days_rule_and_calendar_edge():
    days = [f"2025{m:02d}{d:02d}" for m in range(3, 12) for d in range(1, 29) if dt.date(2025, m, d).weekday() < 5]
    r = BT.rebalance_days(days, "2025-01-01", "2025-12-31")
    assert r[0] == "20250407"            # 3/31 이후 5번째 거래일 (4/1,2,3,4,7)
    assert all(x > "20250315" for x in r)
    late = [d for d in days if d >= "20250901"]
    assert "20250905" not in BT.rebalance_days(late, "2025-01-01", "2025-12-31")   # 기한(8/14)이 달력 밖 → 제외


def test_sell_tax_by_date_and_costs():
    c = BT.load_costs()
    assert BT.sell_tax(c, "20180102", "KOSPI") == 0.0030
    assert BT.sell_tax(c, "20250102", "KOSDAQ") == 0.0015
    assert BT.sell_tax(c, "20260102", "KOSPI") == 0.0020
    buy = BT.trade_cost(c, "base", "buy", "20250102", "KOSPI", 2e12, 1e6, None)
    sell = BT.trade_cost(c, "base", "sell", "20250102", "KOSPI", 2e12, 1e6, None)
    assert abs(buy - 1e6 * (0.00015 + 0.0005)) < 1e-6
    assert abs(sell - buy - 1e6 * 0.0015) < 1e-6
    assert BT.trade_cost(c, "conservative", "buy", "20250102", "KOSPI", 1e10, 1e6, 1e8) > BT.trade_cost(c, "base", "buy", "20250102", "KOSPI", 1e10, 1e6, 1e8)


def test_select_buffer_cap_and_untradable():
    ranked = [{"stock_code": f"{i:06d}", "rank": i, "group_id": "G1" if i <= 5 else f"G{i}"} for i in range(1, 41)]
    sel = BT.select(ranked, prev=[], n=5, cap=3)
    assert sel == ["000001", "000002", "000003", "000006", "000007"]          # G1은 3종목까지
    sel2 = BT.select(ranked, prev=["000025", "000035"], n=5, cap=3)
    assert "000025" in sel2 and "000035" not in sel2                         # 30위 안 보유 유지, 밖은 교체
    sel3 = BT.select(ranked, prev=["999999"], n=5, cap=3, untradable={"999999"})
    assert sel3[0] == "999999"                                               # 매매 불가는 계속 보유


def test_perf_and_spearman():
    p = BT.perf([1.0, 1.1, 0.88, 1.21], ["20200101", "20200102", "20200103", "20210101"])
    assert abs(p["mdd"] - (0.88 / 1.1 - 1)) < 1e-12 and abs(p["total_return"] - 0.21) < 1e-12
    assert abs(BT.spearman(list(range(12)), list(range(12))) - 1) < 1e-12
    assert abs(BT.spearman(list(range(12)), list(range(12))[::-1]) + 1) < 1e-12
    assert BT.spearman([1, 2], [1, 2]) is None


def test_dev_mode_refuses_validation_window():
    with pytest.raises(ValueError):
        BT.run(start="2022-04-01", end="2023-01-01", mode="dev")
    with pytest.raises(ValueError):
        BT.run(start="2022-04-01", end="2023-01-01", mode="validation")      # 전략 frozen 전


def test_list_since_extends_range_and_merges(tmp_path, monkeypatch):
    from qs import universe as U
    monkeypatch.setattr(U, "dart_path", lambda kind, *p: tmp_path / kind / os.path.join(*p))
    monkeypatch.setattr(U, "exists", lambda p: p.exists())
    cc = "00000001"
    U.save_l0(U.dart_path("list", f"{cc}.json.gz"), source={"bgn_de": "20240101"},
              payload=[{"rcept_no": "20260814000001", "report_nm": "반기보고서 (2026.06)"}])
    calls = []

    class D:
        def _json(self, path, params):
            calls.append(params["bgn_de"])
            return {"status": "000", "total_page": 1, "list": [{"rcept_no": "20160330000001", "report_nm": "사업보고서 (2015.12)"},
                                                               {"rcept_no": "20260814000001", "report_nm": "반기보고서 (2026.06)"}]}
    assert len(U._list_since(D(), cc, since=2024)) == 1 and calls == []        # 이미 2024~ → 재사용
    items = U._list_since(D(), cc, since=2015)
    assert calls == ["20150101"] and len(items) == 2                           # 2015~로 다시 받아 중복 없이 합침
    assert U._list_since(D(), cc, since=2015) and calls == ["20150101"]        # 이후엔 재사용


def test_blank_id_rows_fall_back_to_nonstandard_named_rows():
    """ID 행이 비어 있어도 비표준 '사채'·'전환사채' 금액은 부채로 잡는다 (NAVER 2016 3Q 사례)."""
    from qs.normalize.accounts import extract
    rows = [{"sj_div": "BS", "ord": "1", "account_id": "dart_ShortTermBorrowings", "account_nm": "단기차입금", "thstrm_amount": ""},
            {"sj_div": "BS", "ord": "2", "account_id": "dart_LongTermBorrowingsGross", "account_nm": "장기차입금", "thstrm_amount": ""},
            {"sj_div": "BS", "ord": "3", "account_id": "-표준계정코드 미사용-", "account_nm": "전환사채", "thstrm_amount": "13179961230"}]
    v = extract(rows, "CFS")
    assert v["debt"].value == 13179961230 and v["debt"].confidence == "pattern"


def test_as_of_values_uses_original_before_first_amendment():
    from qs.pipeline import as_of_values
    p = {(2016, 2): {"values": {"ni_owners": 100.0, "equity_owners": 500.0}, "values_first": {"ni_owners": 80.0, "equity_owners": None},
                     "rcept_dt_second": "20161115", "amended": True}}
    before, used = as_of_values(p, dt.date(2016, 9, 1))
    assert before[(2016, 2)]["values"] == {"ni_owners": 80.0, "equity_owners": 500.0} and used == ["2016Q2"]
    after, used2 = as_of_values(p, dt.date(2016, 11, 20))
    assert after[(2016, 2)]["values"]["ni_owners"] == 100.0 and used2 == []


def test_admin_rule_judge():
    from qs.exp_admin_rules import judge
    def per(y, q, avail, **v):
        return {"available_date": avail, "values": v}
    P = {(2025, 4): per(2025, 4, "2026-03-31", revenue=2.0e9, op_income=-1, pretax=-6e8, equity_total=1e9, capital_stock=3e9),
         (2024, 4): per(2024, 4, "2025-03-31", revenue=5e9, op_income=-1, pretax=-6e8, equity_total=1e9, capital_stock=3e9),
         (2023, 4): per(2023, 4, "2024-03-31", revenue=5e9, op_income=-1, pretax=1, equity_total=1e9, capital_stock=3e9),
         (2022, 4): per(2022, 4, "2023-03-31", revenue=5e9, op_income=-1, pretax=1, equity_total=1e9, capital_stock=3e9)}
    res, why = judge(P, dt.date(2026, 4, 30), [1e10] * 40)
    assert res["R1"] and res["R2"] and res["R4"] and res["R5"] and res["R6"] is False and res["R3"] is False
    res2, _ = judge(P, dt.date(2025, 4, 30), [])          # 2025 사업보고서 공시 전 → 2024가 최신
    assert res2["R1"] is False and res2["R5"] is None     # 4년 연속 판단에 연도 부족


def test_summarize_benchmarks_beta_and_yearly():
    days = [f"2019{m:02d}{d:02d}" for m in range(1, 13) for d in (3, 10, 17, 24)] + ["20200103"]
    k = {d: 100 * (1.001 ** i) for i, d in enumerate(days)}
    nav = [1.002 ** i for i in range(len(days))]
    sim = {"nav": nav, "nav_days": days, "ic": [{"ic": .1}, {"ic": .2}], "hit": [True, False, True], "holdings": [{"turnover": .5, "cost": .001}],
           "bench_ew": [("a", "b", .05), ("b", "c", .02)], "structure": []}
    r = BT.summarize(sim, {"index": {"KOSPI": k, "KOSDAQ": {}}})
    assert abs(r["benchmark_universe_ew"]["total_return"] - (1.05 * 1.02 - 1)) < 1e-12
    assert abs(r["hit_rate"] - 2 / 3) < 1e-12 and r["ic"]["n"] == 2
    assert "benchmark_KOSPI" in r and set(r["yearly"]) == {"2019", "2020"}


def test_audit_opinion_parse():
    from qs.collect.dart import audit_opinion
    ok = {"status": "000", "list": [{"bsns_year": "제57기(당기)", "adt_opinion": "적정"}, {"bsns_year": "제56기(전기)", "adt_opinion": "한정"}]}
    assert audit_opinion(ok) == ("적정", True)
    assert audit_opinion({"status": "000", "list": [{"bsns_year": "제10기(당기)", "adt_opinion": "의견거절"}]})[1] is False
    assert audit_opinion({"status": "000", "list": [{"bsns_year": "당기", "adt_opinion": "한정의견"}]})[1] is False
    assert audit_opinion({"status": "013"}) == (None, None)


# ── 4차 협의 백테스트 통과 조건 ──
def _flat_cost(rate):
    return lambda c, side, notional: notional * rate


def test_rebalance_step_entry_conserves_value():
    """현금 1.0, 2종목 진입, 편도 1% → 투자금 + 비용 = 1.0, 동일 비중."""
    st = BT.rebalance_step({}, 1.0, {"A": 10.0, "B": 20.0}, {}, ["A", "B"], set(), _flat_cost(0.01))
    inv = st["units"]["A"] * 10 + st["units"]["B"] * 20
    assert abs(inv + st["cost"] + st["cash"] - 1.0) < 1e-12
    assert abs(st["units"]["A"] * 10 - st["units"]["B"] * 20) < 1e-12
    assert abs(st["cost"] - 0.01 * inv) < 1e-9 and abs(st["cash"]) < 1e-9


def test_rebalance_step_swap_hold_and_halt():
    """A 유지, B 매도 → C 매수, H는 거래정지(매매 불가)로 계속 보유. 비용 0이면 총가치 보존."""
    units = {"A": 0.025, "B": 0.0125, "H": 0.01}       # A=0.25, B=0.25, H=0.1 (평가가)
    st = BT.rebalance_step(units, 0.0, {"A": 10.0, "B": 20.0, "C": 5.0}, {"H": 10.0}, ["A", "C"], {"H"}, _flat_cost(0.0))
    assert "B" not in st["units"] and st["units"]["H"] == 0.01
    total = st["units"]["A"] * 10 + st["units"]["C"] * 5 + st["units"]["H"] * 10 + st["cash"]
    assert abs(total - 0.6) < 1e-12 and abs(st["units"]["A"] * 10 - 0.25) < 1e-12   # (0.6-0.1)/2


def test_rebalance_step_liquidity_cap_leaves_cash():
    st = BT.rebalance_step({}, 1.0, {"A": 1.0, "B": 1.0}, {}, ["A", "B"], set(), _flat_cost(0.0), {"A": 0.1})
    assert abs(st["units"]["A"] - 0.1) < 1e-12 and abs(st["units"]["B"] - 0.5) < 1e-12
    assert abs(st["cash"] - 0.4) < 1e-12 and st["capped"] == ["A"]


def test_rebalance_step_full_exit():
    st = BT.rebalance_step({"A": 0.1}, 0.0, {"A": 10.0}, {}, [], set(), _flat_cost(0.002))
    assert st["units"] == {} and abs(st["cash"] - (1.0 - 0.002)) < 1e-12


def test_delist_proceeds_rules():
    assert BT.delist_proceeds({"Reason": "피흡수합병(스팩소멸합병)"}, "20250102", 5000.0)[0] == 5000.0
    assert BT.delist_proceeds({"Reason": "감사의견 거절", "ArrantEnforceDate": "2025-01-02"}, "20250110", 50.0)[0] == 50.0  # 정리매매 관측
    assert BT.delist_proceeds({"Reason": "감사의견 거절", "ArrantEnforceDate": "2025-02-01"}, "20250110", 50.0)[0] == 0.0
    assert BT.delist_proceeds(None, "20250110", 50.0)[0] == 0.0
    assert BT.delist_proceeds(None, "20250110", 50.0, policy="last")[0] == 50.0


def test_snapshot_never_uses_future():
    st = BT.PriceStore.__new__(BT.PriceStore)
    st._snap = [("20250930", {"A": {}}), ("20251031", {"B": {}})]
    assert st.snapshot("20250901") == {}
    assert "A" in st.snapshot("20251015") and "B" in st.snapshot("20251101")


def test_parallel_stops_on_budget_and_isolates_errors():
    from qs import universe as U
    from qs.http import BudgetExceeded
    calls = []
    def fn(x):
        calls.append(x)
        if x == 3:
            raise ValueError("bad company")
        if x == 50:
            raise BudgetExceeded("DART 일일 호출 한도 초과(020)")
        return {"status": "ok", "x": x}
    res, reason = U._parallel(None, list(range(200)), fn)
    assert reason and "한도" in reason
    assert res[3]["status"] == "error" and res[0]["status"] == "ok"
    assert len(calls) < 200                     # 한도 이후 새 작업은 시작하지 않음


def test_kind_source_policy_recorded():
    from qs.collect import kind
    assert kind.allowed() is True     # config/sources.yaml: robots.txt 없음(사용자 확인), 관리종목 경로만, 하루 1회


def test_financial_holding_companies_excluded():
    """KSIC 64992로 등록된 금융지주는 금융업(D-06)으로 제외 — 보정표 또는 이름 규칙."""
    from qs.screen import filter_and_score, load_groups
    def ev(code, name):
        return {"stock_code": code, "name": name, "status": "ok", "induty_code": "64992", "mcap": 1e13,
                "metrics": {}, "stock": {}, "ttm": {}}
    out, _, _ = filter_and_score([ev("105560", "KB금융"), ev("999990", "어떤금융지주"), ev("003550", "LG")],
                                 {}, dt.date(2026, 10, 22), {}, set(), load_groups())
    g = {r["stock_code"]: r["group_id"] for r in out}
    assert g == {"105560": "EX", "999990": "EX", "003550": "G30"}


def test_parse_dividend_common_stock():
    from qs.collect.dart import parse_dividend
    js = {"status": "000", "list": [
        {"se": "주당액면가액(원)", "stock_knd": "", "thstrm": "100"},
        {"se": "(연결)현금배당성향(%)", "stock_knd": "", "thstrm": "25.1"},
        {"se": "현금배당금총액(백만원)", "stock_knd": "", "thstrm": "9,809,438"},
        {"se": "주당 현금배당금(원)", "stock_knd": "보통주", "thstrm": "1,446"},
        {"se": "주당 현금배당금(원)", "stock_knd": "우선주", "thstrm": "1,447"},
        {"se": "현금배당수익률(%)", "stock_knd": "보통주", "thstrm": "2.7"}]}
    d = parse_dividend(js)
    assert d == {"dps": 1446.0, "yield_pct": 2.7, "payout_pct": 25.1, "total_mn": 9809438.0}
    assert parse_dividend({"status": "013"})["dps"] is None


def test_business_holding_registered_as_649_not_excluded():
    from qs.screen import filter_and_score, load_groups
    ev = lambda c, n, k: {"stock_code": c, "name": n, "status": "ok", "induty_code": k, "mcap": 1e13, "metrics": {}, "stock": {}, "ttm": {}}
    out, _, _ = filter_and_score([ev("999991", "어떤홀딩스", "649"), ev("999992", "어떤벤처투자", "649"), ev("004800", "효성", "649")],
                                 {}, dt.date(2026, 10, 22), {}, set(), load_groups())
    g = {r["stock_code"]: r["group_id"] for r in out}
    assert g == {"999991": "G30", "999992": "EX", "004800": "G30"}


def test_review_opinion_no_exception_is_clean():
    from qs.collect.dart import audit_opinion
    for t in ("예외사항 없음", "해당사항없음", "적정의견"):
        assert audit_opinion({"status": "000", "list": [{"bsns_year": "당기", "adt_opinion": t}]})[1] is True
    assert audit_opinion({"status": "000", "list": [{"bsns_year": "당기", "adt_opinion": "한정(범위제한)"}]})[1] is False


def test_owners_derived_from_total_minus_nci():
    """지배 행 없음 + 자본총계·비지배지분(ID) 있음 → 지배 = 총계 − 비지배 (JW중외제약 2026 2Q)."""
    from qs.normalize.accounts import extract
    rows = [{"sj_div": "BS", "ord": "1", "account_id": "ifrs-full_Equity", "account_nm": "자본총계", "thstrm_amount": "409935587608"},
            {"sj_div": "BS", "ord": "2", "account_id": "ifrs-full_NoncontrollingInterests", "account_nm": "비지배지분", "thstrm_amount": "2000"}]
    v = extract(rows, "CFS")
    assert v["equity_owners"].value == 409935585608 and v["equity_owners"].confidence == "derived"


def test_small_group_not_ranked():
    """D-26: 비교 대상 10개 미만 사업군은 백분위가 무의미 → 순위에서 빼고 사유 표시. 10개 이상은 순위."""
    from qs.screen import filter_and_score, load_groups, MIN_GROUP_N
    def ev(code, k, i):
        m = {"E/P": 0.05 + i * 0.01, "B/P": 0.5 + i * 0.1, "EBIT/EV": 0.05 + i * 0.01, "ROE": 0.05 + i * 0.01, "GP/A": 0.1 + i * 0.01,
             "CFO/NI": 1.0, "12-1모멘텀": 0.1 * i, "영업이익증가율": 0.1, "이자보상배율": 5.0}
        return {"stock_code": code, "name": code, "status": "ok", "induty_code": k, "mcap": 1e12, "metrics": m,
                "stock": {"equity_owners": 1e11}, "ttm": {"ni_owners": 1e10}}
    big = [ev(f"9{i:05d}", "264", i) for i in range(MIN_GROUP_N)]        # 반도체 10개
    small = [ev(f"8{i:05d}", "681", i) for i in range(2)]                # 부동산 2개
    tv = {r["stock_code"]: [1e10] * 20 for r in big + small}
    out, ranked, _ = filter_and_score(big + small, {}, dt.date(2026, 10, 22), tv, set(), load_groups())
    rk = {r["stock_code"] for r in ranked}
    assert all(r["stock_code"] in rk for r in big)
    sm = [r for r in out if r["stock_code"].startswith("8")]
    assert all(r["stock_code"] not in rk and any("비교 대상 부족" in x for x in r["exclude"]) for r in sm)


def _synthetic_xs(signal: float, periods: int = 16, n: int = 100, seed: int = 0):
    """점수와 다음 기간 수익의 관계가 signal인 가짜 단면 (수익률 판정 로직 점검용 — 실제 데이터 아님)."""
    import random
    rnd = random.Random(seed)
    xs, nav, days = [], [1.0], []
    d0 = dt.date(2018, 5, 1)
    for k in range(periods):
        s_day = (d0 + dt.timedelta(days=91 * k)).strftime("%Y%m%d")
        e_day = (d0 + dt.timedelta(days=91 * (k + 1))).strftime("%Y%m%d")
        rows = []
        for i in range(n):
            sc = rnd.uniform(0, 100)
            rows.append({"c": f"{i:06d}", "s": sc, "f": signal * (sc - 50) / 1000 + rnd.gauss(0, 0.05), "g": 0.0, "u": 0.0,
                         "h": False, "f12": None})
        top = sorted(rows, key=lambda x: -x["s"])[:20]
        for x in top:
            x["h"] = True
        um = sum(x["f"] for x in rows) / n
        for x in rows:
            x["g"] = um
            x["u"] = um
        pr = sum(x["f"] for x in top) / 20
        if not days:
            days.append(s_day)
        days.append(e_day)
        nav.append(nav[-1] * (1 + pr))
        xs.append({"rebalance": s_day, "start": s_day, "end": e_day, "rows": rows})
    return {"xs": xs, "nav": nav, "nav_days": days}


def test_judge_passes_strong_signal_and_fails_noise():
    from qs.backtest import cross_section, judge
    strong = _synthetic_xs(1.0)
    cs = cross_section(strong["xs"])
    assert cs["spread_top_minus_bottom"] > 0 and cs["adjacent_inversions"] <= 1 and cs["ic_sector"]["ir"] > 0.3
    j = judge(strong, {"cross_section": cs}, conservative_excess=0.05)
    assert j["status"] == "통과", j
    noise = _synthetic_xs(0.0, seed=3)
    jn = judge(noise, {"cross_section": cross_section(noise["xs"])}, conservative_excess=0.0)
    assert jn["status"] in ("실패", "미판정")
    assert any("비용 민감" in f for f in jn["flags"])


def test_cross_section_quintile_one_is_top_score():
    from qs.backtest import cross_section
    cs = cross_section(_synthetic_xs(1.0)["xs"])
    q = cs["quintiles"]
    assert q[1]["sector_excess"]["mean"] > q[5]["sector_excess"]["mean"]
    assert q[1]["win_vs_sector"]["lo"] is not None and 0 <= q[1]["win_vs_sector"]["mean"] <= 1
    assert "90~100" in cs["bands_10pt"]


def test_primary_exclusion_order():
    from qs.screen import primary_exclusion
    assert primary_exclusion([]) is None
    assert primary_exclusion(["적자(TTM 지배주주 순이익 < 0)", "20일 평균 거래대금 5억 미만"])[0] == "거래대금 미달"
    assert primary_exclusion(["F-스코어 2.0 ≤ 3", "금융업(D-06)"])[0] == "금융업"
    assert primary_exclusion(["비교 대상 부족(사업군 3개 < 10, D-26)", "적자(TTM)"])[0] == "비교 대상 부족"


def test_store_shards_pack_roundtrip(tmp_path):
    """보관소 조각: 파일이 빠짐없이 조각에 들어가고, 풀면 같은 경로로 돌아온다 (2026-10-30 사고 후)."""
    import tarfile
    from qs.store_shards import pack, shard_key
    src = tmp_path / "l0"
    files = ["dart/fs/00126380/2024_11011.json.gz", "dart/xbrl/00164779/a.zip", "krx/sto_stk_bydd_trd/20261026.json.gz", "MANIFEST.json"]
    for f in files:
        (src / f).parent.mkdir(parents=True, exist_ok=True)
        (src / f).write_bytes(f.encode())
    assert shard_key("krx/sto_stk_bydd_trd/x", "l0") == "krx-sto_stk_bydd_trd" and shard_key("MANIFEST.json", "l0") == "misc"
    sh = pack(src, tmp_path / "out", "l0", "7")
    assert sum(s["files"] for s in sh) == len(files) and all(s["name"].endswith("-g7.tar.gz") for s in sh)
    dst = tmp_path / "back"
    for s in sh:
        with tarfile.open(tmp_path / "out" / s["name"]) as tf:
            tf.extractall(dst, filter="data")
    assert sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()) == sorted(files)


def test_dividend_history_and_fin_subgroup():
    from qs.collect.dart import parse_dividend_hist
    from qs.screen import fin_subgroup
    js = {"status": "000", "list": [
        {"se": "주당 현금배당금(원)", "stock_knd": "보통주", "thstrm": "1,446", "frmtrm": "1,444", "lwfr": "1,444"},
        {"se": "주당 현금배당금(원)", "stock_knd": "우선주", "thstrm": "1,447", "frmtrm": "1,445", "lwfr": "1,445"},
        {"se": "(연결)현금배당성향(%)", "stock_knd": "", "thstrm": "25.1"}]}
    h = parse_dividend_hist(js)
    assert h["dps"] == [1446.0, 1444.0, 1444.0] and h["payout_pct"] == 25.1
    assert fin_subgroup("64110", "기업은행") == "은행" and fin_subgroup("65110", "한화생명") == "보험"
    assert fin_subgroup("64992", "KB금융") == "금융지주" and fin_subgroup("66121", "키움증권") == "증권"


def _facts(qvals: dict, tag="NetIncomeLoss", annual=None, inst=None, shares=1000):
    us = {tag: {"units": {"USD": [{"frame": f"CY{y}Q{q}", "val": v, "end": f"{y}-{q*3:02d}-28"} for (y, q), v in qvals.items()]
                          + [{"frame": f"CY{y}", "val": v, "end": f"{y}-12-31"} for y, v in (annual or {}).items()]}}}
    for t, vals in (inst or {}).items():
        us[t] = {"units": {"USD": [{"frame": f"CY{y}Q{q}I", "val": v, "end": f"{y}-{q*3:02d}-28"} for (y, q), v in vals.items()]}}
    return {"facts": {"us-gaap": us, "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2026-07-20", "val": shares, "accn": "a1"}]}}}}}


def test_us_ttm_with_derived_q4_and_metrics():
    """SEC frame: 분기 값 + 연간에서 4분기 역산 → TTM, 주식수, 지표."""
    import datetime as dt
    from qs.us import summarize, metrics, group_of, load_groups
    q = {(2025, 1): 10, (2025, 2): 10, (2025, 3): 10, (2026, 1): 20, (2026, 2): 20}
    f = _facts(q, annual={2025: 40}, inst={"StockholdersEquity": {(2026, 2): 400, (2025, 2): 300}})
    s = summarize(f, today=dt.date(2026, 10, 1))
    assert s["status"] == "ok" and s["last_q"] == "2026Q2"
    assert s["ttm"]["ni_owners"] == 10 + 10 + 20 + 20      # 2025Q3, 2025Q4(=40−30), 2026Q1, 2026Q2
    assert s["shares"] == 1000
    m = metrics(s, mcap=600.0, mom=None)
    assert abs(m["E/P"] - 60 / 600) < 1e-9 and abs(m["ROE"] - 60 / 350) < 1e-9
    assert summarize(f, today=dt.date(2027, 6, 1))["status"] == "stale"
    g = load_groups()
    assert group_of("3674", g)[0] == "U06" and group_of("3672", g)[0] == "U07" and group_of("6798", g)[0] == "EX"
    assert group_of("7372", g)[0] == "U10" and group_of("8731", g)[0] == "U03" and group_of("5812", g)[0] == "U26"


def test_market_and_hybrid_scores():
    """시장 전체 점수는 업종 구분 없이 계산되고, 혼합은 업종 70% + 시장 30% (사전 등록 EXPERIMENTS #4)."""
    from qs.screen import filter_and_score, load_groups, HYBRID_W
    def ev(code, k, i):
        m = {"E/P": 0.02 + i * 0.01, "B/P": 0.5 + i * 0.05, "EBIT/EV": 0.03 + i * 0.01, "ROE": 0.05 + (i % 5) * 0.02,
             "GP/A": 0.1 + i * 0.01, "CFO/NI": 1.0, "12-1모멘텀": 0.05 * i, "영업이익증가율": 0.1, "이자보상배율": 5.0 + i}
        return {"stock_code": code, "name": code, "status": "ok", "induty_code": k, "mcap": 1e12, "metrics": m,
                "stock": {"equity_owners": 1e11}, "ttm": {"ni_owners": 1e10}}
    evs = [ev(f"9{i:05d}", "264", i) for i in range(12)] + [ev(f"7{i:05d}", "21", i + 5) for i in range(12)]
    tv = {r["stock_code"]: [1e10] * 20 for r in evs}
    import copy
    out_t, _, _ = filter_and_score(copy.deepcopy(evs), {}, dt.date(2026, 10, 22), tv, set(), load_groups())
    out_h, _, _ = filter_and_score(copy.deepcopy(evs), {}, dt.date(2026, 10, 22), tv, set(), load_groups(), {"grouping": "hybrid"})
    t = {r["stock_code"]: r for r in out_t}
    for r in out_h:
        a, m = t[r["stock_code"]].get("composite"), r.get("mkt_composite")
        if a is not None and m is not None:
            assert abs(r["composite"] - round(HYBRID_W * a + (1 - HYBRID_W) * m, 1)) < 0.11
    assert any(r.get("mkt_composite") is not None for r in out_t)


def test_ksic_name_and_company_info(tmp_path, monkeypatch):
    from qs import screen
    assert screen.ksic_name("29210") == ("농업 및 임업용 기계 제조업", False)
    name, approx = screen.ksic_name("21212")          # 표에 없는 세세분류 → 상위 분류로 대신
    assert name and approx is True
    assert screen.ksic_name(None) == (None, False)
    # 원천 파일이 없으면 업종명만
    info = screen.company_info("00000000", "29210")
    assert info == {"ind": "농업 및 임업용 기계 제조업", "ind_approx": None, "est": None, "hm": None}
    assert screen.company_info(None, None) is None
