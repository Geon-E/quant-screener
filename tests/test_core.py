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


def test_extract_cfs_without_owner_line_is_invalid():
    # 비지배지분 행은 있는데 지배주주 행을 못 찾음 → 무효 (대체하지 않음)
    rows = [_r("CIS", "ifrs-full_ProfitLoss", "당기순이익", "100", ord_=1),
            _r("CIS", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", "20", ord_=3)]
    assert extract(rows, "CFS")["ni_owners"].value is None


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
