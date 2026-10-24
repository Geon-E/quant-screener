"""거래일 달력: KRX L0 일별시세(행 수 > 0)에서 만든다. 범위 밖 날짜는 평일 기준으로 근사하고 표시한다."""
from __future__ import annotations

import bisect
import datetime as dt

from ..storage import L0, load_l0


class TradingCalendar:
    def __init__(self, days: list[dt.date]):
        self.days = sorted(set(days))

    @classmethod
    def from_l0(cls) -> "TradingCalendar":
        d = L0 / "krx" / "sto_stk_bydd_trd"
        days = []
        for p in d.glob("*.json.gz"):
            if load_l0(p)["meta"].get("rows"):
                days.append(dt.datetime.strptime(p.name[:8], "%Y%m%d").date())
        return cls(days)

    def covers(self, d: dt.date) -> bool:
        return bool(self.days) and self.days[0] <= d <= self.days[-1]

    def next_trading_day(self, d: dt.date) -> tuple[dt.date, bool]:
        """d 다음 거래일 (d 당일 제외). 반환: (날짜, 달력 근거 여부)."""
        if self.covers(d):
            i = bisect.bisect_right(self.days, d)
            if i < len(self.days):
                return self.days[i], True
        n = d + dt.timedelta(days=1)
        while n.weekday() >= 5:
            n += dt.timedelta(days=1)
        return n, False

    def add_trading_days(self, d: dt.date, n: int) -> tuple[dt.date, bool]:
        exact = True
        for _ in range(n):
            d, ok = self.next_trading_day(d)
            exact = exact and ok
        return d, exact
