"""API 호출 공통: 재시도, 호출 수 집계, 실행당 호출 예산."""
from __future__ import annotations

import time

import requests

from .util import log, scrub

UA = {"User-Agent": "quant-screener/0.1 (personal research)"}


class BudgetExceeded(Exception):
    """실행당 호출 예산 소진 — 정상 중단 신호 (다음 실행에서 이어받기)."""


class Client:
    def __init__(self, name: str, budget: int, min_interval: float = 0.0):
        self.name = name
        self.budget = budget
        self.calls = 0
        self.errors = 0
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, *, params=None, headers=None, timeout=60, retries=3) -> requests.Response:
        last_exc = None
        for attempt in range(retries):
            if self.calls >= self.budget:
                raise BudgetExceeded(f"{self.name} 호출 예산 {self.budget} 소진")
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self.calls += 1
            self._last = time.monotonic()
            try:
                r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last_exc = e
                self.errors += 1
                backoff = 2 ** attempt * 2
                log(f"{self.name} 재시도 {attempt + 1}/{retries} ({scrub(str(e))[:120]}) {backoff}s 대기")
                time.sleep(backoff)
        raise RuntimeError(scrub(f"{self.name} 호출 실패: {last_exc}"))

    def stats(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "budget": self.budget}
