"""KRX OPEN API 수집기 (0단계 실측: https://data-dbg.krx.co.kr/svc/apis/{api}?basDd=, 헤더 AUTH_KEY).

- 일별: 코스피·코스닥 일별매매, 코스피·코스닥 지수 (매 거래일)
- 종목기본정보: 최근일 + 월말 스냅샷 (과거 소속부·주식종류 이력용)
- 휴장일도 빈 결과로 L0에 기록 → 다시 호출하지 않음, 거래일 달력으로 재사용
- 당일·전일 파일은 아직 확정 전일 수 있으므로 force 재수집 허용
"""
from __future__ import annotations

import datetime as dt
import os

from ..http import BudgetExceeded, Client
from ..storage import exists, krx_path, load_l0, save_l0
from ..util import log, today_kst

BASE = "https://data-dbg.krx.co.kr/svc/apis"
DAILY_APIS = ["sto/stk_bydd_trd", "sto/ksq_bydd_trd", "idx/kospi_dd_trd", "idx/kosdaq_dd_trd"]
BASE_INFO_APIS = ["sto/stk_isu_base_info", "sto/ksq_isu_base_info"]


def _rows(js):
    if isinstance(js, dict):
        for v in js.values():
            if isinstance(v, list):
                return v
    return None


class KRX:
    def __init__(self, budget: int = 3000):
        self.key = os.environ.get("KRX_API_KEY", "").strip()
        self.http = Client("KRX", budget=budget, min_interval=0.15)
        self.saved = 0
        self.skipped = 0

    def fetch(self, api: str, basDd: str):
        r = self.http.get(f"{BASE}/{api}", params={"basDd": basDd}, headers={"AUTH_KEY": self.key})
        if r.status_code != 200:
            raise RuntimeError(f"KRX {api} {basDd} HTTP {r.status_code}: {r.text[:200]}")
        js = r.json()
        rows = _rows(js)
        if rows is None:
            raise RuntimeError(f"KRX {api} {basDd} 응답 형식 이상: {str(js)[:200]}")
        return rows

    def collect(self, api: str, basDd: str, force: bool = False) -> int | None:
        path = krx_path(api, basDd)
        if exists(path) and not force:
            self.skipped += 1
            return load_l0(path)["meta"].get("rows")
        rows = self.fetch(api, basDd)
        save_l0(path, source={"provider": "KRX", "api": api, "basDd": basDd, "rows": len(rows)}, payload=rows)
        self.saved += 1
        return len(rows)


def weekdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def month_ends(start: dt.date, end: dt.date) -> set[str]:
    """각 월의 마지막 평일 (종목기본정보 월말 스냅샷 기준일 후보)."""
    last = {}
    for d in weekdays(start, end):
        last[(d.year, d.month)] = d
    return {d.strftime("%Y%m%d") for d in last.values()}


def run_backfill(start: dt.date, end: dt.date | None = None, budget: int = 3000) -> dict:
    """start~end 평일 일별 데이터 + 월말 종목기본정보. 최근 2영업일은 force 재수집."""
    end = end or today_kst()
    k = KRX(budget=budget)
    recent = {d.strftime("%Y%m%d") for d in list(weekdays(end - dt.timedelta(days=4), end))[-2:]}
    mends = month_ends(start, end)
    trading_days, stopped = 0, None
    try:
        # 최신일부터 거꾸로 → 예산이 모자라도 최근 데이터가 먼저 채워진다
        for d in sorted(weekdays(start, end), reverse=True):
            bd = d.strftime("%Y%m%d")
            force = bd in recent
            n = k.collect("sto/stk_bydd_trd", bd, force=force)
            if n == 0:
                continue  # 휴장일 → 나머지 API는 생략 (빈 결과는 기록됨)
            for api in DAILY_APIS[1:]:
                k.collect(api, bd, force=force)
            if bd in mends or bd in recent:
                for api in BASE_INFO_APIS:
                    k.collect(api, bd, force=force)
            trading_days += 1
    except BudgetExceeded as e:
        stopped = str(e)
        log(f"KRX 백필 중단(다음 실행에서 이어받음): {e}")
    res = {"start": start.isoformat(), "end": end.isoformat(), "saved": k.saved, "skipped": k.skipped,
           "trading_days_touched": trading_days, "stopped": stopped, **k.http.stats()}
    log(f"KRX 백필 결과: {res}")
    return res
