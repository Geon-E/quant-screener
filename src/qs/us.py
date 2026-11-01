"""미국 저평가 후보 — SEC EDGAR 재무(무료·공식) + Massive(옛 Polygon) 무료 시세.

원칙은 국내와 같다: 사업군(SIC 고정 표) 안에서 지표별 백분위 → 가치·퀄리티·추세·안정성 → 종합점수 (가설, 성과 미검증).
원천
  - SEC companyfacts.zip (전 종목 XBRL 재무, 매일 갱신) · submissions.zip (SIC 업종) · company_tickers_exchange.json
    SEC 규칙: User-Agent에 연락처 필수 → 환경변수 SEC_CONTACT (GitHub 비밀값, 저장소·화면에 쓰지 않음)
  - Massive grouped daily (하루 1회 호출로 전 종목 종가·거래대금), 무료 분당 5회·과거 2년 → 환경변수 MASSIVE_API_KEY
보관: 원본 zip은 다시 받을 수 있으므로 저장하지 않고, 회사별 요약(L1 us/fundamentals)과 일별 시세(L0 us/grouped)만 둔다.
한계: 다중 클래스 주식은 대표 티커 가격 × 전체 주식수, 회계연도는 SEC 'frame'(달력 분기 정렬)에 의존.

2026-11-01: SEC가 GitHub 실행 서버 접속을 차단(403 'Undeclared Automated Tool'·'Request Rate Threshold', 4회 시험 모두).
Massive 무료 등급은 재무 API가 없음(403 not entitled). 사용자 결정: 'ETF·배당만 먼저' → 기본은 run_lite.
SEC 경로는 코드만 남겨 두고 환경변수 US_SEC=1일 때만 쓴다 (차단된 상태에서 재시도로 SEC를 두드리지 않도록).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import re
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from .util import CONFIG, REPORTS, log, read_gz_json, write_gz_json
from .storage import L0, L1

SEC_FACTS = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SEC_SUBM = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
MASSIVE = os.environ.get("MASSIVE_BASE", "https://api.massive.com")
EXCHANGES = {"NYSE", "Nasdaq"}
MIN_MCAP = 300e6          # 시가총액 3억 달러 이상
MIN_DV20 = 2e6            # 20일 평균 거래대금 200만 달러 이상

# 계정 태그: 앞에서부터 찾음
TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "ni_owners": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "op_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "cost_rev": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "interest": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestPaidNet"],
    "dps": ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"],
    "equity_owners": ["StockholdersEquity"],
    "equity_total": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "debt_cur": ["LongTermDebtCurrent", "DebtCurrent"],
    "debt_st": ["ShortTermBorrowings", "CommercialPaper"],
}
DURATION = {"revenue", "ni_owners", "op_income", "gross_profit", "cost_rev", "cfo", "interest", "dps"}
Q_FRAME = re.compile(r"^CY(\d{4})Q([1-4])$")
A_FRAME = re.compile(r"^CY(\d{4})$")
I_FRAME = re.compile(r"^CY(\d{4})Q([1-4])I$")


# ── 수신 ──
def _ua() -> dict:
    c = os.environ.get("SEC_CONTACT", "").strip()
    if not c:
        raise SystemExit("SEC_CONTACT 비밀값 없음 — SEC는 요청자 연락처(이름 이메일)를 요구함")
    return {"User-Agent": f"quant-screener {c}", "Accept-Encoding": "gzip, deflate"}


def _get(url: str, **kw):
    import requests
    for i in range(4):
        r = requests.get(url, headers=_ua(), timeout=600, stream=kw.get("stream", False))
        if r.status_code == 200:
            return r
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"SEC {url} HTTP {r.status_code}")


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _get(url, stream=True) as r, dest.open("wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    return dest


# ── 업종 ──
def load_groups() -> list[tuple[str, str, str]]:
    out = []
    with (CONFIG / "us_peer_groups_v1.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for p in r["sic_prefixes"].split(";"):
                if p.strip():
                    out.append((p.strip(), r["group_id"], r["group_name"]))
    return sorted(out, key=lambda x: -len(x[0]))


def group_of(sic: str | None, groups) -> tuple[str | None, str | None]:
    s = (sic or "").strip().zfill(4) if sic else ""
    for p, gid, name in groups:
        if s.startswith(p):
            return gid, name
    return None, None


# ── 재무 요약 (회사 하나의 companyfacts JSON → TTM·잔액) ──
def _units(fact: dict, unit: str = "USD") -> list[dict]:
    u = fact.get("units") or {}
    return u.get(unit) or u.get("USD/shares") or []


def series(facts: dict, item: str) -> dict:
    """item의 첫 번째로 존재하는 태그 → {'q': {(y,q): v}, 'a': {y: v}, 'i': {(y,q): v}, 'tag': 태그}."""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for tag in TAGS[item]:
        f = gaap.get(tag)
        if not f:
            continue
        unit = "USD/shares" if item == "dps" else "USD"
        q, a, i = {}, {}, {}
        for x in _units(f, unit):
            fr = x.get("frame") or ""
            if m := Q_FRAME.match(fr):
                q[(int(m[1]), int(m[2]))] = x["val"]
            elif m := A_FRAME.match(fr):
                a[int(m[1])] = x["val"]
            elif m := I_FRAME.match(fr):
                i[(int(m[1]), int(m[2]))] = x["val"]
        if q or a or i:
            return {"q": q, "a": a, "i": i, "tag": tag}
    return {"q": {}, "a": {}, "i": {}, "tag": None}


def _prev(k: tuple[int, int], n: int = 1) -> tuple[int, int]:
    y, q = k
    for _ in range(n):
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return y, q


def quarters(s: dict) -> dict:
    """분기 값 + 빠진 4분기 = 연간 − (1~3분기)."""
    q = dict(s["q"])
    for y, v in s["a"].items():
        if (y, 4) not in q and all((y, k) in q for k in (1, 2, 3)):
            q[(y, 4)] = v - sum(q[(y, k)] for k in (1, 2, 3))
    return q


def ttm(q: dict, end: tuple[int, int]) -> float | None:
    ks = [_prev(end, i) for i in range(4)]
    return sum(q[k] for k in ks) if all(k in q for k in ks) else None


def summarize(facts: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    S = {k: series(facts, k) for k in TAGS}
    qs = {k: quarters(S[k]) for k in DURATION}
    ends = sorted(set(qs["ni_owners"]) | set(qs["revenue"]))
    if not ends:
        return {"status": "no_financials"}
    last = ends[-1]
    # 최근 분기가 9개월보다 오래되면 무효 (상장폐지·공시 중단)
    last_end = dt.date(last[0], last[1] * 3, 28)
    if (today - last_end).days > 280:
        return {"status": "stale", "last_q": f"{last[0]}Q{last[1]}"}
    T = {k: ttm(qs[k], last) for k in DURATION}
    if T["gross_profit"] is None and T["revenue"] is not None and ttm(qs["cost_rev"], last) is not None:
        T["gross_profit"] = T["revenue"] - ttm(qs["cost_rev"], last)
    Tp = {k: ttm(qs[k], _prev(last, 4)) for k in ("op_income", "ni_owners", "revenue")}

    def inst(item, key):
        v = S[item]["i"]
        return v.get(key)
    bal = {k: inst(k, last) for k in ("equity_owners", "equity_total", "assets", "liabilities", "cash", "debt_lt", "debt_cur", "debt_st")}
    bal_prev = {k: inst(k, _prev(last, 4)) for k in ("equity_owners", "assets")}
    if bal["liabilities"] is None and bal["assets"] is not None and bal["equity_total"] is not None:
        bal["liabilities"] = bal["assets"] - bal["equity_total"]
    debt = sum(v for v in (bal["debt_lt"], bal["debt_cur"], bal["debt_st"]) if v is not None) if any(
        bal[k] is not None for k in ("debt_lt", "debt_cur", "debt_st")) else None
    # 주식 수: dei (클래스별이면 같은 기준일 합계)
    dei = (facts.get("facts") or {}).get("dei") or {}
    sh = (dei.get("EntityCommonStockSharesOutstanding") or {}).get("units", {}).get("shares") or []
    shares, sh_date = None, None
    if sh:
        latest = max(x["end"] for x in sh)
        same: dict = {}
        for x in sh:
            if x["end"] == latest:
                same.setdefault(x.get("accn"), []).append(x["val"])
        shares = max(sum(v) for v in same.values())   # 한 공시 안의 클래스별 값 합
        sh_date = latest
    return {"status": "ok", "last_q": f"{last[0]}Q{last[1]}", "ttm": T, "ttm_prev": Tp, "bal": bal, "bal_prev": bal_prev,
            "debt": debt, "shares": shares, "shares_date": sh_date,
            "dps_ttm": T["dps"], "tags": {k: S[k]["tag"] for k in ("revenue", "ni_owners", "op_income", "equity_owners")}}


def metrics(s: dict, mcap: float | None, mom: float | None) -> dict:
    T, Tp, B, Bp = s["ttm"], s["ttm_prev"], s["bal"], s["bal_prev"]
    def div(a, b):
        return a / b if a is not None and b not in (None, 0) else None
    def avg(a, b):
        return (a + b) / 2 if a is not None and b is not None else None
    ev = (mcap + (s["debt"] or 0) - (B["cash"] or 0)) if mcap else None
    eq_avg = avg(B["equity_owners"], Bp["equity_owners"])
    m = {
        "E/P": div(T["ni_owners"], mcap),
        "B/P": div(B["equity_owners"], mcap) if (B["equity_owners"] or 0) > 0 else None,
        "EBIT/EV": div(T["op_income"], ev) if ev and ev > 0 else None,
        "ROE": div(T["ni_owners"], eq_avg) if eq_avg and eq_avg > 0 else None,
        "GP/A": div(T["gross_profit"], avg(B["assets"], Bp["assets"])),
        "CFO/NI": div(T["cfo"], T["ni_owners"]) if (T["ni_owners"] or 0) > 0 else None,
        "12-1모멘텀": mom,
        "영업이익증가율": (T["op_income"] / Tp["op_income"] - 1) if T["op_income"] is not None and (Tp["op_income"] or 0) > 0 else None,
        "이자보상배율": (min(T["op_income"] / T["interest"], 100.0) if (T["interest"] or 0) > 0 and T["op_income"] is not None
                     else (100.0 if T["op_income"] and T["op_income"] > 0 and T["interest"] in (0, None) and s["debt"] in (0, None) else None)),
    }
    return m


def fundamentals(facts_zip: Path, ciks: set[int]) -> dict[int, dict]:
    out = {}
    with zipfile.ZipFile(facts_zip) as z:
        for cik in ciks:
            name = f"CIK{cik:010d}.json"
            try:
                facts = json.loads(z.read(name))
            except KeyError:
                out[cik] = {"status": "no_facts"}
                continue
            try:
                out[cik] = summarize(facts)
            except Exception as e:  # noqa: BLE001 — 한 회사 오류로 전체를 멈추지 않음
                out[cik] = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:100]}"}
    return out


def sic_map(subm_zip: Path, ciks: set[int]) -> dict[int, dict]:
    out = {}
    with zipfile.ZipFile(subm_zip) as z:
        names = set(z.namelist())
        for cik in ciks:
            n = f"CIK{cik:010d}.json"
            if n in names:
                js = json.loads(z.read(n))
                out[cik] = {"sic": js.get("sic"), "sic_desc": js.get("sicDescription"), "fye": js.get("fiscalYearEnd"),
                            "category": js.get("category")}
    return out


# ── 시세 (Massive grouped daily) ──
def grouped(date: str) -> list[dict] | None:
    """date(YYYY-MM-DD) 전 종목 일봉. 휴장일은 빈 목록. L0 us/grouped/<date>.json.gz에 보관."""
    p = L0 / "us" / "grouped" / f"{date}.json.gz"
    if p.exists():
        return read_gz_json(p)
    if not os.environ.get("MASSIVE_API_KEY", "").strip():
        return None
    js = _massive(f"/v2/aggs/grouped/locale/us/market/stocks/{date}", {"adjusted": "true"})   # 분당 5회 간격은 _massive가 지킴
    rows = [{"T": x["T"], "c": x.get("c"), "v": x.get("v"), "vw": x.get("vw")} for x in (js.get("results") or [])]
    write_gz_json(p, rows)
    return rows


def price_history(days_back: int = 380, budget_calls: int = 60) -> dict[str, list[tuple[str, float, float]]]:
    """최근 days_back일 평일 일봉 → {ticker: [(date, close, dollar_vol)]} (날짜 오름차순).
    아직 없는 날짜는 최근 날짜부터 새로 받는다(새 호출은 budget_calls까지) — 첫 실행에도 최신 종가가 먼저 생기도록."""
    today = dt.date.today()
    days = [(today - dt.timedelta(days=k)).isoformat() for k in range(days_back, 0, -1)
            if (today - dt.timedelta(days=k)).weekday() < 5]
    missing = [d for d in reversed(days) if not (L0 / "us" / "grouped" / f"{d}.json.gz").exists()]
    for ds in missing[:budget_calls]:
        try:
            grouped(ds)
        except RuntimeError as e:
            log(f"!! 미국 시세 {ds}: {e}")
    hist = defaultdict(list)
    for ds in days:
        p = L0 / "us" / "grouped" / f"{ds}.json.gz"
        if not p.exists():
            continue
        for x in read_gz_json(p) or []:
            if x.get("c"):
                hist[x["T"]].append((ds, x["c"], (x.get("vw") or x["c"]) * (x.get("v") or 0)))
    return hist


# ── Massive 공통 호출 (무료 분당 5회) ──
_LAST_CALL = [0.0]


def _massive(url: str, params: dict | None = None) -> dict:
    import requests
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("MASSIVE_API_KEY 없음")
    params = dict(params or {}, apiKey=key)
    for i in range(4):
        wait = 12.5 - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        r = requests.get(url if url.startswith("http") else f"{MASSIVE}{url}", params=params, timeout=120)
        _LAST_CALL[0] = time.time()
        if r.status_code == 429:
            time.sleep(65)
            continue
        if r.status_code == 200:
            return r.json()
        break
    raise RuntimeError(f"Massive {url.split('?')[0][-60:]} HTTP {r.status_code}: {r.text[:120].replace(key, '***')}")


def _pages(path: str, params: dict, max_pages: int = 60) -> list[dict]:
    out, url, prm = [], path, params
    for _ in range(max_pages):
        js = _massive(url, prm)
        out += js.get("results") or []
        nxt = js.get("next_url")
        if not nxt:
            return out
        url, prm = nxt, {}          # next_url에 조건이 들어 있음 (키는 _massive가 붙임)
    log(f"!! {path}: {max_pages}쪽에서 멈춤")
    return out


# ── ETF·배당 (재무 없이) ──
FREQ = {12: "월", 4: "분기", 2: "반기", 1: "연"}
EXCH = {"XNYS": "NYSE", "XNAS": "Nasdaq"}


def us_tickers(max_age_days: int = 7) -> list[dict]:
    """보통주(CS) 목록: 티커·이름·거래소. 1주일에 한 번만 다시 받음 (L0 us/tickers/<날짜>)."""
    d = L0 / "us" / "tickers"
    have = sorted(d.glob("*.json.gz")) if d.exists() else []
    if have and (dt.date.today() - dt.date.fromisoformat(have[-1].name[:10])).days < max_age_days:
        return read_gz_json(have[-1])
    rows = _pages("/v3/reference/tickers", {"market": "stocks", "type": "CS", "active": "true", "limit": 1000})
    keep = [{"t": x["ticker"], "n": x.get("name"), "ex": EXCH.get(x.get("primary_exchange"))} for x in rows
            if EXCH.get(x.get("primary_exchange")) and x.get("ticker")]
    write_gz_json(d / f"{dt.date.today().isoformat()}.json.gz", keep)
    return keep


def dividends(months: int = 25, deadline: float | None = None) -> tuple[list[dict], int]:
    """최근 months개월 배당 기록 (월별 파일 L0 us/dividends/<YYYY-MM>). 지난달·이번 달은 매번 다시, 그 전 달은 한 번만.
    → (기록 목록, 확보한 개월 수)"""
    today = dt.date.today()
    d = L0 / "us" / "dividends"
    out, got = [], 0
    y, m = today.year, today.month
    for k in range(months):
        mm = (m - 1 - k) % 12 + 1
        yy = y + (m - 1 - k) // 12
        tag = f"{yy:04d}-{mm:02d}"
        p = d / f"{tag}.json.gz"
        if p.exists() and k >= 2:
            out += read_gz_json(p); got += 1
            continue
        if deadline and time.time() > deadline:
            if p.exists():
                out += read_gz_json(p); got += 1
            continue
        nxt = f"{yy + (mm == 12):04d}-{mm % 12 + 1:02d}-01"
        try:
            rows = _pages("/v3/reference/dividends", {"ex_dividend_date.gte": f"{tag}-01", "ex_dividend_date.lt": nxt,
                                                     "limit": 1000, "order": "asc", "sort": "ex_dividend_date"})
        except RuntimeError as e:
            log(f"!! 배당 {tag}: {e}")
            if p.exists():
                out += read_gz_json(p); got += 1
            continue
        keep = [{"t": x.get("ticker"), "a": x.get("cash_amount"), "cur": x.get("currency"), "ex": x.get("ex_dividend_date"),
                 "f": x.get("frequency"), "ty": x.get("dividend_type")} for x in rows if x.get("ticker")]
        write_gz_json(p, keep)
        out += keep; got += 1
    return out, got


def div_stats(recs: list[dict], today: dt.date, full_two_years: bool) -> dict[str, dict]:
    """티커별 최근 12개월 정기배당 합(특별배당 제외), 그 전 12개월 합, 배당 주기, 경고."""
    y1, y2 = today - dt.timedelta(days=365), today - dt.timedelta(days=730)
    by = defaultdict(list)
    for r in recs:
        if (r.get("cur") or "USD") == "USD" and r.get("a") and r.get("ex"):
            by[r["t"]].append(r)
    out = {}
    for t, rs in by.items():
        last, prev, spec, fq = 0.0, 0.0, 0, []
        for r in rs:
            ex = dt.date.fromisoformat(r["ex"][:10])
            if ex > today:
                continue
            special = r.get("ty") not in (None, "CD")
            if ex > y1:
                if special:
                    spec += 1
                else:
                    last += r["a"]; fq.append(r.get("f"))
            elif ex > y2 and not special:
                prev += r["a"]
        if last <= 0:
            continue
        f = max(set(fq), key=fq.count) if fq else None
        trend = None
        if full_two_years:
            trend = "신규" if prev <= 0 else ("증가" if last > prev * 1.001 else "감소" if last < prev * 0.999 else "유지")
        warn = [f"특별배당 {spec}회는 수익률에서 제외"] if spec else []
        out[t] = {"dps": round(last, 4), "dps1": round(prev, 4) if full_two_years else None, "trend": trend,
                  "freq": FREQ.get(f) if isinstance(f, int) else None, "warn": warn}
    return out


def run_lite(price_calls: int = 40, time_budget_min: float = 40) -> dict:
    deadline = time.time() + time_budget_min * 60
    today = dt.date.today()
    tick = us_tickers()
    log(f"미국 보통주(NYSE·Nasdaq): {len(tick)}개")
    recs, months = dividends(deadline=deadline - 10 * 60)
    stats = div_stats(recs, today, full_two_years=months >= 25)
    left = max(0, int((deadline - time.time()) / 13))
    hist = price_history(budget_calls=min(price_calls, left))
    out = []
    for x in tick:
        st = stats.get(x["t"])
        px = hist.get(x["t"]) or []
        if not st or not px:
            continue
        close = px[-1][1]
        dv20 = sum(v[2] for v in px[-20:]) / len(px[-20:])
        if close < 1 or dv20 < MIN_DV20 or len(px) < 5:
            continue
        y = st["dps"] / close * 100
        warn = list(st["warn"])
        if y > 12:
            warn.append("수익률 12% 초과: 주가 급락·일시 배당 확인")
        out.append({"ticker": x["t"], "name": x["n"], "exchange": x["ex"], "close": close, "dv20": dv20, "asof_px": px[-1][0],
                    "dividend": {**st, "warn": warn, "yield_now": round(y, 3)}})
    etfs = etf_view(hist)
    for e in etfs:
        st = stats.get(e["ticker"])
        e["dy"] = round(st["dps"] / e["close"] * 100, 2) if st and e.get("close") else None
        e["dfreq"] = st["freq"] if st else None
    days = len({d for v in hist.values() for d, _, _ in v})
    res = {"mode": "lite", "tickers": len(tick), "div_payers": len(out), "dividend_months": months, "price_days": days, "etfs": etfs}
    publish_lite(out, res)
    (REPORTS / "us").mkdir(parents=True, exist_ok=True)
    (REPORTS / "us" / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"미국(ETF·배당): {json.dumps({k: v for k, v in res.items() if k != 'etfs'}, ensure_ascii=False)}")
    return res


def compact_lite(r: dict) -> dict:
    from .screen_view import SHOW
    d = r["dividend"]
    return {"c": r["ticker"], "n": r.get("name"), "mk": r.get("exchange"), "g": None, "gid": None, "gn": None, "rel": None,
            "rk": None, "grk": None, "sc": None, "b": [None] * 4, "x": [], "w": [], "mc": None, "px": r["close"],
            "tv": round(r["dv20"]), "per": None, "pbr": None, "m": {k: None for k in SHOW}, "p": {k: None for k in SHOW},
            "pd": None, "fs": "", "fsc": None, "ksic": None, "fb": [], "ib": None, "xp": None, "xpf": None, "fd": None, "av": None,
            "mr": {}, "prk": None, "pk": None,
            "dv": {"year": None, "dps": d["dps"], "dps1": d["dps1"], "dps2": None, "payout": None, "yield_now": d["yield_now"],
                   "streak3": None, "trend": d["trend"], "warn": d["warn"], "freq": d["freq"]},
            "fg": None, "fs2": None, "frk": None, "fp": None, "fx": None, "co": None}


def publish_lite(out: list[dict], res: dict) -> None:
    from .screen_view import build_view
    from .util import now_kst
    asof = max((r["asof_px"] for r in out if r.get("asof_px")), default=None) or \
        max((e.get("asof") for e in res["etfs"] if e.get("asof")), default=None)
    warns = []
    if res["price_days"] < 252:
        warns.append(f"1년 수익률은 시세가 1년치 쌓이면 표시됩니다 (현재 {res['price_days']}거래일)")
    if res["dividend_months"] < 25:
        warns.append("배당 증감 비교는 2년치 배당 기록이 모이면 표시됩니다")
    summary = {"market": "US", "lite": True, "asof_price": asof.replace("-", "") if asof else "", "ranked": 0,
               "evaluated": len(out), "universe_collected": res["tickers"], "universe_total": res["tickers"],
               "collection_complete": True, "prev_asof": None, "etfs": res["etfs"], "price_days": res["price_days"],
               "status": {"built_at": now_kst().isoformat(timespec="minutes"), "warnings": warns, "history": None}}
    build_view(out, summary, REPORTS / "us", compact_fn=compact_lite, site_name="us.html", keep_reports=False)


# ── 실행 ──
def run(work: Path | None = None, price_calls: int = 60) -> dict:
    if os.environ.get("US_SEC") != "1":
        return run_lite(price_calls=price_calls)
    from .screen import score_group, reliability, MIN_GROUP_N
    work = work or Path("/tmp/us_sec")
    tick = _get(SEC_TICKERS).json()
    cols = tick["fields"]
    rows = [dict(zip(cols, r)) for r in tick["data"]]
    rows = [r for r in rows if r.get("exchange") in EXCHANGES and r.get("ticker")]
    by_cik = defaultdict(list)
    for r in rows:
        by_cik[int(r["cik"])].append(r)
    ciks = set(by_cik)
    log(f"미국 종목: {len(rows)}개 티커 / {len(ciks)}개 회사 (NYSE·Nasdaq)")
    fz = download(SEC_FACTS, work / "companyfacts.zip")
    fund = fundamentals(fz, ciks)
    fz.unlink(missing_ok=True)
    sz = download(SEC_SUBM, work / "submissions.zip")
    sic = sic_map(sz, ciks)
    sz.unlink(missing_ok=True)
    hist = price_history(budget_calls=price_calls)
    have_px = bool(hist)
    groups = load_groups()
    out = []
    for cik, tks in by_cik.items():
        t0 = tks[0]
        f = fund.get(cik) or {"status": "no_facts"}
        si = sic.get(cik) or {}
        gid, gname = group_of(si.get("sic"), groups)
        r = {"cik": cik, "stock_code": t0["ticker"], "ticker": t0["ticker"], "tickers": [t["ticker"] for t in tks], "name": t0["name"], "exchange": t0["exchange"],
             "sic": si.get("sic"), "sic_desc": si.get("sic_desc"), "group_id": gid, "group_name": gname,
             "status": f.get("status"), "last_q": f.get("last_q"), "exclude": [], "warn": [], "metrics": {}}
        px = hist.get(t0["ticker"]) or []
        close = px[-1][1] if px else None
        dv20 = sum(x[2] for x in px[-20:]) / len(px[-20:]) if px else None
        mom = (px[-22][1] / px[-252][1] - 1) if len(px) >= 252 and px[-252][1] else None
        mcap = close * f["shares"] if close and f.get("shares") else None
        r.update({"close": close, "mcap": mcap, "dv20": dv20, "asof_px": px[-1][0] if px else None})
        if f.get("status") == "ok":
            r["metrics"] = metrics(f, mcap, mom)
            r["dps_ttm"] = f.get("dps_ttm")
            r["div_yield"] = (f["dps_ttm"] / close * 100) if f.get("dps_ttm") and close else None
            if (f["bal"].get("equity_owners") or 0) <= 0:
                r["exclude"].append("자본잠식(자본 ≤ 0)")
            if (f["ttm"].get("ni_owners") or 0) < 0:
                r["exclude"].append("적자(TTM 순이익 < 0)")
        else:
            r["exclude"].append({"no_facts": "SEC 재무 없음", "stale": "재무 공시 9개월 이상 없음", "no_financials": "재무 없음"}.get(
                f.get("status"), f.get("status") or "재무 없음"))
        if gid is None:
            r["exclude"].append(f"업종 미배정(SIC {si.get('sic')})")
        elif gid == "EX":
            r["exclude"].append("금융·리츠·스팩")
        if have_px:
            if not close:
                r["exclude"].append("시세 없음")
            elif (mcap or 0) < MIN_MCAP:
                r["exclude"].append("시가총액 3억 달러 미만")
            if (dv20 or 0) < MIN_DV20:
                r["exclude"].append("20일 평균 거래대금 200만 달러 미만")
        else:
            r["warn"].append("시세 없음: 가치 지표(PER·PBR·EV) 계산 불가")
        out.append(r)
    by_g = defaultdict(list)
    for r in out:
        if not r["exclude"]:
            by_g[r["group_id"]].append(r)
    for gid, rs in by_g.items():
        score_group(rs)
        for r in rs:
            r["group_n"], r["reliability"] = len(rs), reliability(len(rs))
            if len(rs) < MIN_GROUP_N:
                r["exclude"].append(f"비교 대상 부족(사업군 {len(rs)}개 < {MIN_GROUP_N})")
    ranked = sorted([r for r in out if not r["exclude"] and r.get("composite") is not None], key=lambda r: (-r["composite"], r["ticker"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    res = {"companies": len(out), "with_financials": sum(1 for r in out if r["status"] == "ok"), "ranked": len(ranked),
           "prices": have_px, "price_days": len({d for v in hist.values() for d, _, _ in v}) if have_px else 0,
           "groups": {g: len(v) for g, v in sorted(by_g.items(), key=lambda x: str(x[0]))},
           "status": dict(Counter_(r["status"] for r in out))}
    write_gz_json(L1 / "us" / "screen.json.gz", {"asof": dt.date.today().isoformat(), "rows": out, "summary": res})
    res["etfs"] = etf_view(hist)
    publish(out, res)
    (REPORTS / "us").mkdir(parents=True, exist_ok=True)
    (REPORTS / "us" / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"미국: {json.dumps({k: v for k, v in res.items() if k != 'groups'}, ensure_ascii=False)}")
    return res


def Counter_(it):
    from collections import Counter
    return Counter(it)


def etf_view(hist: dict) -> list[dict]:
    """보유 ETF 요약: 현재가, 1개월·1년 수익률(가격 기준, 분배금 제외), 52주 고점 대비. 분배금 수익률은 후속(배당 API 호출 필요)."""
    out = []
    with (CONFIG / "us_etfs.csv").open(encoding="utf-8") as f:
        for e in csv.DictReader(f):
            px = hist.get(e["ticker"]) or []
            if not px:
                out.append({**e, "close": None})
                continue
            c = px[-1][1]
            def ret(n):
                return (c / px[-1 - n][1] - 1) if len(px) > n and px[-1 - n][1] else None
            hi = max(x[1] for x in px[-252:])
            out.append({**e, "close": c, "asof": px[-1][0], "r1m": ret(21), "r1y": ret(251), "from_high": c / hi - 1 if hi else None,
                        "days": len(px)})
    return out


def compact_us(r: dict) -> dict:
    """미국 행 → 국내 화면과 같은 키 (같은 템플릿 재사용)."""
    from .screen_view import SHOW, _r
    m, ep, bp = r.get("metrics") or {}, (r.get("metrics") or {}).get("E/P"), (r.get("metrics") or {}).get("B/P")
    dv = None
    if r.get("dps_ttm"):
        dv = {"year": r.get("last_q"), "dps": _r(r["dps_ttm"], 4), "dps1": None, "dps2": None, "payout": None,
              "yield_now": _r(r.get("div_yield"), 3), "streak3": None, "trend": None, "warn": [], "n": 1}
    return {"c": r["ticker"], "n": r.get("name"), "mk": r.get("exchange"), "g": r.get("group_name"), "gid": r.get("group_id"),
            "gn": r.get("group_n"), "rel": r.get("reliability"), "rk": r.get("rank"), "grk": r.get("group_rank"),
            "sc": r.get("composite"), "b": [(r.get("buckets") or {}).get(k) for k in ("value", "quality", "trend", "safety")],
            "x": r.get("exclude") or [], "w": r.get("warn") or [], "mc": _r(r.get("mcap"), 0), "px": r.get("close"),
            "tv": _r(r.get("dv20"), 0), "per": _r(1 / ep, 2) if ep and ep > 0 else None, "pbr": _r(1 / bp, 2) if bp and bp > 0 else None,
            "m": {k: _r(m.get(k)) for k in SHOW}, "p": {k: (r.get("pct") or {}).get(k) for k in SHOW},
            "pd": r.get("last_q"), "fs": "", "fsc": None, "ksic": r.get("sic"), "fb": [], "ib": None,
            "xp": (r.get("exclude") or [None])[0], "xpf": (r.get("exclude") or [None])[0], "fd": None, "av": None, "mr": {},
            "prk": None, "pk": None, "dv": dv, "fg": None, "fs2": None, "frk": None, "fp": None, "fx": None}


def publish(out: list[dict], res: dict) -> None:
    from .screen_view import build_view
    from .util import now_kst
    for gid in {r.get("group_id") for r in out}:
        g = sorted([r for r in out if r.get("group_id") == gid and r.get("rank")], key=lambda r: r["rank"])
        for i, r in enumerate(g, 1):
            r["group_rank"] = i
    asof = max((r["asof_px"] for r in out if r.get("asof_px")), default=None)
    warns = [] if res["prices"] else ["시세 키(MASSIVE_API_KEY)가 아직 없어 가치·추세 지표와 순위를 계산하지 못했습니다"]
    summary = {"market": "US", "asof_price": asof.replace("-", "") if asof else "", "ranked": res["ranked"],
               "evaluated": res["with_financials"], "universe_collected": res["companies"], "universe_total": res["companies"],
               "collection_complete": True, "prev_asof": None, "etfs": res.get("etfs") or [],
               "status": {"built_at": now_kst().isoformat(timespec="minutes"), "warnings": warns, "history": None}}
    build_view(out, summary, REPORTS / "us", compact_fn=compact_us, site_name="us.html", keep_reports=False)
