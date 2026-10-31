"""과거 성과 검증 (2단계 백테스트) — DESIGN 6·7장, config/strategy_v1.yaml, config/costs_v1.yaml.

원칙
  - 현재 화면과 **같은 코드**로 평가·점수를 낸다 (pipeline.evaluate_company, screen.filter_and_score).
  - 각 리밸런싱일에는 그 시점에 공시된(available_date ≤ 기준일) 재무만 쓴다.
    단, 재무 값은 최신 정정본이다 (최초본 XBRL 복원 D-17 전까지 한계로 표기).
  - 업종은 현재 분류를 쓴다 (과거 분류 미확보 — DESIGN 7.7).
  - 구간 보호: 기본은 개발 구간(2018-04-01 ~ 2022-03-31). 검증 구간은 전략이 'frozen'일 때만, 실행마다 열람 기록.
  - 기계 점검(--smoke): 데이터가 있는 아무 구간에서 구조만 확인하고 수익률은 출력하지 않는다.

체결·보유 (DESIGN 6)
  - 신호: 리밸런싱일 D 종가 기준 평가 → D+1 시가 체결, 동일 비중 20종목, 사업군 3종목 한도, 보유 종목은 30위 안이면 유지
  - 거래정지(시가 없음) 종목은 매매하지 못하고 계속 보유, 시세가 끊긴(상장폐지) 종목은 마지막 관측가로 현금화
  - 비용: 매수·매도 수수료 + 매도 거래세(시행일별) + 시나리오별 스프레드·시장충격
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
from collections import Counter, defaultdict

import numpy as np
import yaml

from .normalize.calendar import TradingCalendar
from .pipeline import company_periods, evaluate_company
from .score.metrics import Metric, merge_preferred_mcap
from .screen import filter_and_score, load_groups
from .storage import L0, L1, load_l0
from .util import CONFIG, REPORTS, log

DEV = ("2018-04-01", "2022-03-31")
VALIDATION_START = "2022-04-01"
N, BUFFER, GROUP_CAP = 20, 30, 3


# ── 설정 ──
def load_costs() -> dict:
    with (CONFIG / "costs_v1.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def sell_tax(costs: dict, day: str, market: str) -> float:
    rate = 0.0
    for t in costs["sell_tax"]:
        if f"{t['from']}".replace("-", "") <= day:
            rate = t.get(market, t.get("KOSPI"))
    return rate


def trade_cost(costs: dict, scenario: str, side: str, day: str, market: str, mcap: float | None,
               notional: float, tv20: float | None) -> float:
    """편도 비용(원). side: buy/sell."""
    parts = costs["scenarios"][scenario]
    rate = costs["commission"] if "commission" in parts else 0.0
    if side == "sell" and "sell_tax" in parts:
        rate += sell_tax(costs, day, market)
    if "spread_half" in parts:
        for b in costs["spread_half"]:
            if float(mcap or 0) >= float(b["mcap_min"]):
                rate += b["rate"]
                break
    if "impact" in parts and tv20:
        rate += costs["impact_per_participation_pct"] * (notional / tv20 * 100)
    return notional * rate


# ── 리밸런싱일 (DESIGN 6.3): 법정 제출기한 경과 후 5영업일째 ──
DEADLINES = [(3, 31), (5, 15), (8, 14), (11, 14)]   # 사업(90일)·1분기·반기·3분기(45일)


def rebalance_days(days: list[str], start: str, end: str) -> list[str]:
    out = []
    y0, y1 = int(start[:4]), int(end[:4])
    for y in range(y0, y1 + 1):
        for m, d in DEADLINES:
            dl = f"{y}{m:02d}{d:02d}"
            if not days or dl < days[0]:
                continue                 # 달력이 제출기한을 덮지 않으면 '5영업일째'를 셀 수 없음
            after = [x for x in days if x > dl]
            if len(after) >= 5:
                r = after[4]
                if start.replace("-", "") <= r <= end.replace("-", ""):
                    out.append(r)
    return sorted(set(out))


# ── 가격 저장소 (전 기간, numpy) ──
class PriceStore:
    """KRX 일별시세 전 기간을 (거래일 × 종목) 배열로. 결측 = nan."""

    def __init__(self):
        cal_days = sorted(p.name[:8] for p in (L0 / "krx" / "sto_stk_bydd_trd").glob("*.json.gz")
                          if load_l0(p)["meta"].get("rows"))
        self.days = cal_days
        self.day_idx = {d: i for i, d in enumerate(cal_days)}
        codes, names, mkt = {}, {}, {}
        per_day = []
        for d in cal_days:
            rows = []
            for api, mk in (("sto_stk_bydd_trd", "KOSPI"), ("sto_ksq_bydd_trd", "KOSDAQ")):
                p = L0 / "krx" / api / f"{d}.json.gz"
                if p.exists():
                    for r in load_l0(p)["payload"]:
                        c = r["ISU_CD"]
                        if c not in codes:
                            codes[c] = len(codes)
                        names[c], mkt[c] = r.get("ISU_NM"), mk
                        rows.append((codes[c], r))
            per_day.append(rows)
        self.codes = codes
        self.code_list = sorted(codes, key=codes.get)
        self.names, self.mkt = names, mkt
        D, M = len(cal_days), len(codes)
        f = lambda: np.full((D, M), np.nan)   # noqa: E731
        self.close, self.open, self.cmp, self.mcap, self.tv, self.shares = f(), f(), f(), f(), f(), f()
        self.admin = np.zeros((D, M), dtype=np.int8)   # 코스닥 소속부: 1 관리종목, 2 투자주의환기
        num = lambda v: float(str(v).replace(",", "")) if v not in (None, "", "-") else np.nan   # noqa: E731
        for i, rows in enumerate(per_day):
            for j, r in rows:
                self.close[i, j] = num(r.get("TDD_CLSPRC"))
                self.open[i, j] = num(r.get("TDD_OPNPRC"))
                self.cmp[i, j] = num(r.get("CMPPREVDD_PRC"))
                self.mcap[i, j] = num(r.get("MKTCAP"))
                self.tv[i, j] = num(r.get("ACC_TRDVAL"))
                self.shares[i, j] = num(r.get("LIST_SHRS"))
                sect = r.get("SECT_TP_NM") or ""
                self.admin[i, j] = 1 if "관리종목" in sect else (2 if "투자주의환기" in sect else 0)
        self.close[self.close <= 0] = np.nan
        self.open[self.open <= 0] = np.nan
        valid = ~np.isnan(self.close)
        idx = np.where(valid, np.arange(D)[:, None], -1)
        self.last_valid = np.maximum.accumulate(idx, axis=0)          # 각 날짜까지 마지막 관측 행
        self._snap = self._load_snapshots()
        self._rows_cache, self._mcap_cache = {}, {}

    # 월말 종목기본정보 스냅샷 (주식종류·증권구분·상장일)
    def _load_snapshots(self) -> list[tuple[str, dict]]:
        snaps = defaultdict(dict)
        for api in ("sto_stk_isu_base_info", "sto_ksq_isu_base_info"):
            for p in sorted((L0 / "krx" / api).glob("*.json.gz")):
                rec = load_l0(p)
                if rec["meta"].get("rows"):
                    for b in rec["payload"]:
                        snaps[p.name[:8]][b["ISU_SRT_CD"]] = b
        return sorted(snaps.items())

    def snapshot(self, day: str) -> dict:
        best = {}
        for d, s in self._snap:
            if d <= day:
                best = s
            else:
                break
        return best   # 기준일 이전 스냅샷이 없으면 빈 값 — 미래 스냅샷으로 대체하지 않는다 (4차 협의 통과 조건 1)

    # 수정주가 조정계수 (adjust.py와 같은 정의)
    def adjustment(self, policy: str = "confirmed_only") -> tuple[np.ndarray, np.ndarray]:
        """반환: (수정종가 배수 cum[D,M], 미확인 사건 표시 unver[D,M]).
        policy: confirmed_only = 미세·분할·병합만 적용(나머지 미적용+표시, 기본값 — 4차 협의 Q3 대기)
                all = 모든 기준가 조정 적용"""
        D, M = self.close.shape
        prev = np.vstack([np.full((1, M), np.nan), _ffill(self.close)[:-1]])
        prev_sh = np.vstack([np.full((1, M), np.nan), _ffill(self.shares)[:-1]])
        base = self.close - self.cmp
        with np.errstate(invalid="ignore", divide="ignore"):
            f = base / prev
            sr = self.shares / prev_sh
        ok = ~np.isnan(f) & (base > 0)
        f = np.where(ok, f, 1.0)
        event = np.abs(f - 1) > 1e-9
        minor = np.abs(f - 1) < 1e-3
        inverse = (np.abs(sr * f - 1) < 0.01) & (np.abs(sr - 1) > 1e-9)
        confirmed = minor | np.nan_to_num(inverse, nan=False).astype(bool)
        unver = event & ~confirmed
        # 공시로 확인된 사건(캐시)은 확인됨으로 (confirm_events가 채움)
        from .adjust import dart_confirmed
        for i, j in zip(*np.nonzero(unver)):
            if dart_confirmed(self.code_list[j], self.days[i]):
                unver[i, j] = False
        if policy == "confirmed_only":
            f = np.where(unver, 1.0, f)
        # 과거 가격 × (그 이후 모든 조정계수) → 날짜 t의 배수 = ∏_{s>t} f_s
        cum = np.ones((D, M))
        for i in range(D - 2, -1, -1):
            cum[i] = cum[i + 1] * f[i + 1]
        return cum, unver

    # evaluate_company용 시점 고정 인터페이스
    def rows_on(self, i: int) -> list[dict]:
        if i not in self._rows_cache:
            js = np.where(~np.isnan(self.close[i]))[0]
            self._rows_cache = {i: [{"ISU_CD": self.code_list[j], "TDD_CLSPRC": self.close[i, j], "MKTCAP": self.mcap[i, j]}
                                    for j in js]}
        return self._rows_cache[i]

    def mcap_map(self, i: int) -> dict:
        if i not in self._mcap_cache:
            snap = self.snapshot(self.days[i])
            rows = [r for r in self.rows_on(i) if not math.isnan(r["MKTCAP"])]
            self._mcap_cache = {i: merge_preferred_mcap(rows, list(snap.values()))}
        return self._mcap_cache[i]


def _ffill(a: np.ndarray) -> np.ndarray:
    D, M = a.shape
    idx = np.where(~np.isnan(a), np.arange(D)[:, None], 0)
    idx = np.maximum.accumulate(idx, axis=0)
    out = a[idx, np.arange(M)]
    return out


class AsOf:
    """evaluate_company가 요구하는 가격 인터페이스를 특정 거래일 i로 고정."""

    def __init__(self, store: PriceStore, i: int, adj_close: np.ndarray, unver: np.ndarray):
        self.s, self.i = store, i
        self.days = store.days[: i + 1]
        self.adj, self.unver = adj_close, unver

    def last_trading_day_of(self, code: str) -> str | None:
        j = self.s.codes.get(code)
        if j is None:
            return None
        k = self.s.last_valid[self.i, j]
        return None if k < 0 else self.s.days[k]

    def day(self, bd: str) -> list[dict]:
        return self.s.rows_on(self.s.day_idx[bd])

    def mcap_map(self, bd: str) -> dict:
        return self.s.mcap_map(self.s.day_idx[bd])

    def momentum(self, code: str, bd: str) -> Metric:
        j, i = self.s.codes.get(code), self.s.day_idx[bd]
        if j is None or i < 252:
            return Metric(None, "가격 이력 부족(13개월 미만)")
        if self.unver[i - 251: i + 1, j].any():
            return Metric(None, "수정주가 미검증(미확인 기업행위)")
        p12, p1 = self.adj[i - 252, j], self.adj[i - 21, j]
        if np.isnan(p12) or np.isnan(p1) or p12 <= 0:
            return Metric(None, "가격 결측")
        return Metric(float(p1 / p12 - 1))


# ── 포트폴리오 구성 ──
def select(ranked: list[dict], prev: list[str], n: int = N, buffer: int = BUFFER, cap: int = GROUP_CAP,
           untradable: set[str] = frozenset()) -> list[str]:
    """보유 유지(30위 안 또는 매매 불가) → 상위부터 채움. 사업군 3종목 한도."""
    rank = {r["stock_code"]: r["rank"] for r in ranked}
    grp = {r["stock_code"]: r["group_id"] for r in ranked}
    hold = [c for c in prev if c in untradable]
    keep = sorted([c for c in prev if c not in untradable and rank.get(c, 10 ** 9) <= buffer], key=lambda c: rank[c])
    out, cnt = list(hold), defaultdict(int)
    for c in hold:
        cnt[grp.get(c)] += 1
    for c in keep:
        if len(out) < n and cnt[grp[c]] < cap:
            out.append(c)
            cnt[grp[c]] += 1
    for r in ranked:
        c = r["stock_code"]
        if len(out) >= n:
            break
        if c not in out and cnt[r["group_id"]] < cap:
            out.append(c)
            cnt[r["group_id"]] += 1
    return out


# ── 성과 지표 ──
def perf(nav: list[float], days: list[str]) -> dict:
    a = np.array(nav, dtype=float)
    if len(a) < 2:
        return {}
    r = a[1:] / a[:-1] - 1
    years = (dt.datetime.strptime(days[-1], "%Y%m%d") - dt.datetime.strptime(days[0], "%Y%m%d")).days / 365.25
    peak = np.maximum.accumulate(a)
    mdd = float((a / peak - 1).min())
    vol = float(r.std(ddof=1) * math.sqrt(252)) if len(r) > 1 else None
    return {"total_return": float(a[-1] / a[0] - 1), "cagr": float((a[-1] / a[0]) ** (1 / years) - 1) if years > 0 else None,
            "mdd": mdd, "vol": vol, "sharpe_rf0": float(r.mean() / r.std(ddof=1) * math.sqrt(252)) if len(r) > 1 and r.std() > 0 else None}


def spearman(x: list[float], y: list[float]) -> float | None:
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None and math.isfinite(a) and math.isfinite(b)]
    if len(pairs) < 10:
        return None
    def rk(v):
        order = sorted(range(len(v)), key=lambda k: v[k])
        out = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for t in range(i, j + 1):
                out[order[t]] = (i + j) / 2
            i = j + 1
        return out
    rx, ry = rk([p[0] for p in pairs]), rk([p[1] for p in pairs])
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def confirm_events(d, until_min: float = 95) -> dict:
    """과거 전 기간 미확인 기업행위를 DART 공시로 확인 (사건별 캐시, 재개 가능). 과거 이력 수집이 끝난 뒤 남는 호출 예산으로."""
    from .adjust import confirm_with_dart, dart_confirmed
    from .http import BudgetExceeded
    from .util import elapsed_min
    store = PriceStore()
    _, unver = store.adjustment("all")
    corp = {c["stock_code"]: c["corp_code"] for c in d.corpcode() if c["stock_code"]}
    todo = [(store.code_list[j], store.days[i]) for i, j in zip(*np.nonzero(unver)) if dart_confirmed(store.code_list[j], store.days[i]) is None]
    done, stopped = 0, None
    try:
        for code, day in todo:
            if elapsed_min() > until_min:
                stopped = "시간 예산"
                break
            confirm_with_dart([{"code": code, "date": day, "class": "확인 필요"}], d, corp)
            done += 1
    except BudgetExceeded as e:
        stopped = str(e)
    return {"unverified_events": int(unver.sum()), "todo": len(todo), "checked": done, "stopped": stopped}


# ── 비교 실험 목록 (DESIGN 7.5 — 개발 구간에서만) ──
V1 = {"value": 0.40, "quality": 0.30, "trend": 0.20, "safety": 0.10}
VARIANTS = {
    "v1": {},
    "동일가중": {"weights": {"value": .25, "quality": .25, "trend": .25, "safety": .25}},
    "가치중심": {"weights": {"value": .60, "quality": .20, "trend": .10, "safety": .10}},
    "가치+퀄리티": {"weights": {"value": .50, "quality": .50, "trend": 0, "safety": 0}},
    "가치제외": {"weights": {"value": 0, "quality": .30, "trend": .20, "safety": .10}},
    "퀄리티제외": {"weights": {"value": .40, "quality": 0, "trend": .20, "safety": .10}},
    "추세제외": {"weights": {"value": .40, "quality": .30, "trend": 0, "safety": .10}},
    "안정성제외": {"weights": {"value": .40, "quality": .30, "trend": .20, "safety": 0}},
    "가치만": {"weights": {"value": 1, "quality": 0, "trend": 0, "safety": 0}},
    "퀄리티만": {"weights": {"value": 0, "quality": 1, "trend": 0, "safety": 0}},
    "추세만": {"weights": {"value": 0, "quality": 0, "trend": 1, "safety": 0}},
    "적자포함": {"exclude_loss": False},
    "F스코어미적용": {"exclude_fscore": False},
    "KSIC원분류": {"grouping": "ksic2"},
    "전체시장": {"grouping": "market"},
    "혼합(업종70·시장30)": {"grouping": "hybrid"},   # 2026-10-31 사전 등록 (사용자 요청, 수익률 보기 전) — EXPERIMENTS #4
}
KEEP = ("stock_code", "corp_code", "name", "status", "error", "induty_code", "mcap", "close", "metrics", "ttm", "stock",
        "fscore", "fscore_items", "interest_basis", "period", "flags", "fs_div", "audit")


def _index_series(store: PriceStore) -> dict[str, dict[str, float]]:
    out = {"KOSPI": {}, "KOSDAQ": {}}
    for api, name, key in (("idx_kospi_dd_trd", "코스피", "KOSPI"), ("idx_kosdaq_dd_trd", "코스닥", "KOSDAQ")):
        for p in sorted((L0 / "krx" / api).glob("*.json.gz")):
            rec = load_l0(p)
            for r in rec["payload"] if rec["meta"].get("rows") else []:
                if r.get("IDX_NM") == name:
                    try:
                        out[key][p.name[:8]] = float(str(r["CLSPRC_IDX"]).replace(",", ""))
                    except (TypeError, ValueError):
                        pass
    return out


def guard(mode: str, start: str, end: str) -> dict:
    with (CONFIG / "strategy_v1.yaml").open(encoding="utf-8") as f:
        strat = yaml.safe_load(f)
    if mode == "dev" and not (DEV[0] <= start and end <= DEV[1]):
        raise ValueError(f"개발 구간({DEV[0]} ~ {DEV[1]}) 밖은 dev 모드로 실행할 수 없음")
    if mode == "validation" and strat.get("status") != "frozen":
        raise ValueError("검증 구간은 전략 버전이 frozen일 때만 실행 (D-21)")
    if mode == "dev" and end >= VALIDATION_START:
        raise ValueError("검증 구간과 겹침")
    return strat


def load_delisting() -> dict[str, dict]:
    """KRX 상장폐지종목 현황 (FinanceDataReader 공개 캐시) — 하루 1회 L0에 저장, 캐시 중단 시 마지막 저장본 (4차 Q2)."""
    import io
    import requests
    from .exp_listing import FDR_CACHE
    from .http import UA
    from .storage import exists, save_l0
    from .util import today_kst
    d0 = L0 / "krx" / "delisting_cache"
    today = f"{today_kst():%Y%m%d}"
    path = d0 / f"{today}.json.gz"
    if not exists(path):
        rows, src = None, None
        for back in range(0, 15):
            d = today_kst() - dt.timedelta(days=back)
            try:
                r = requests.get(FDR_CACHE.format(d=f"{d:%Y-%m-%d}"), headers=UA, timeout=60)
            except Exception:  # noqa: BLE001
                break
            if r.status_code == 200:
                import pandas as pd
                df = pd.read_csv(io.StringIO(r.text), index_col=0, dtype={"Symbol": str, "ToSymbol": str})
                rows, src = df.astype(str).to_dict("records"), f"{d:%Y-%m-%d}"
                break
        if rows is not None:
            save_l0(path, source={"provider": "FinanceDataReader cache of KRX MDCSTAT23801", "file_date": src, "count": len(rows)},
                    payload=rows)
    files = sorted(d0.glob("*.json.gz")) if d0.exists() else []
    if not files:
        return {}
    return {str(r.get("Symbol", "")).zfill(6): r for r in load_l0(files[-1])["payload"]}


def prepare(start: str, end: str, adj_policy: str = "confirmed_only") -> dict:
    """리밸런싱일마다 전 종목 평가를 1회만 계산해 둔다 (변형 실험이 공유).
    수정주가는 두 정책을 모두 만들어 둔다: confirmed_only(확인된 사건만 — 기본) / all(전부 — 4차 Q3 민감도)."""
    store = PriceStore()
    cum_c, unver = store.adjustment("confirmed_only")
    cum_a, _ = store.adjustment("all")
    adj = {"confirmed_only": (store.close * cum_c, store.open * cum_c), "all": (store.close * cum_a, store.open * cum_a)}
    adj_close, adj_open = adj[adj_policy]
    cal = TradingCalendar.from_l0()
    from .collect.dart import DART
    corp = {c["stock_code"]: c["corp_code"] for c in DART(budget=1).corpcode() if c["stock_code"]}
    period_cache: dict[str, tuple] = {}
    last_total = store.last_valid[-1]
    rdays = rebalance_days(store.days, start, end)
    log(f"백테스트 준비: 리밸런싱 {len(rdays)}회 {rdays[:2]}…")
    signals = []
    for R in rdays:
        i = store.day_idx[R]
        if i + 1 >= len(store.days):
            break
        snap = store.snapshot(R)
        if not snap:
            # 기준일 이전 종목기본정보 스냅샷이 없음 → 그 회차는 미지원 (미래 스냅샷 대체 금지)
            signals.append({"R": R, "i": i, "evals": [], "meta": {}, "tv": {}, "halted": set(), "missing_fin": 0,
                            "no_snapshot": True})
            continue
        ctx = AsOf(store, i, adj_close, unver)
        evals, meta, miss_dead, miss_alive = [], {}, 0, 0
        for code, b in snap.items():
            if b.get("KIND_STKCERT_TP_NM") != "보통주" or b.get("SECUGRP_NM") != "주권" or "SPAC" in (b.get("SECT_TP_NM") or ""):
                continue
            j = store.codes.get(code)
            if j is None or np.isnan(store.close[i, j]):
                continue
            later_delisted = last_total[j] < len(store.days) - 1
            cc = corp.get(code)
            base, periods = (None, None)
            if cc:
                if cc not in period_cache:
                    try:
                        period_cache[cc] = company_periods({"corp_code": cc, "stock_code": code, "name": b.get("ISU_ABBRV"), "category": ""}, cal)
                    except FileNotFoundError:
                        period_cache[cc] = (None, None)
                base, periods = period_cache[cc]
            if base is None:
                # 재무 미매핑·미수집 — 이후 상장폐지 종목인지 따로 센다 (생존자 편향 점검, 4차 통과 조건 3)
                if later_delisted:
                    miss_dead += 1
                else:
                    miss_alive += 1
                continue
            try:
                ev = dict(base) if periods is None else evaluate_company(base, periods, ctx)
            except Exception as e:  # noqa: BLE001
                ev = {**base, "status": "error", "error": str(e)[:120]}
            evals.append({k: ev.get(k) for k in KEEP})
            meta[code] = {"market": store.mkt.get(code), "list_dd": b.get("LIST_DD"),
                          "sect": {1: "관리종목", 2: "투자주의환기종목"}.get(int(store.admin[i, j]), "")}
        tv = {c: [x for x in store.tv[max(0, i - 19): i + 1, store.codes[c]] if not np.isnan(x)] for c in meta}
        halted = {c for c, v in tv.items() if len(v) >= 5 and all(x == 0 for x in v[-5:])}
        signals.append({"R": R, "i": i, "evals": evals, "meta": meta, "tv": tv, "halted": halted,
                        "missing_fin": miss_dead + miss_alive, "missing_fin_delisted_later": miss_dead, "missing_fin_alive": miss_alive})
    return {"store": store, "adj_close": adj_close, "adj_open": adj_open, "adj": adj, "unver": unver, "signals": signals,
            "start": start, "end": end, "adj_policy": adj_policy, "index": _index_series(store), "delist": load_delisting()}


MERGER_PAT = ("합병", "이전상장", "완전자회사", "주식교환", "주식이전", "지주회사", "스팩소멸", "유가증권시장 상장")


def delist_proceeds(info: dict | None, last_obs_day: str, last_price: float, policy: str = "design") -> tuple[float, str]:
    """상장폐지 회수금액 (DESIGN 6.2). 반환: (회수가격, 근거).
    - 합병·이전상장·완전자회사화 등 → 마지막 관측가 (대가가 지급되는 유형)
    - 정리매매가 관측됨(마지막 관측일 ≥ 정리매매 시작일) → 정리매매 최종가 = 마지막 관측가
    - 그 외(정리매매 미관측·사유 미확보) → 0 (−100%, 보수적). policy='last'면 마지막 관측가(민감도)"""
    if policy == "last":
        return last_price, "민감도: 마지막 관측가"
    if info:
        reason = str(info.get("Reason") or "")
        if any(k in reason for k in MERGER_PAT):
            return last_price, f"대가 지급형({reason[:20]})"
        arr = str(info.get("ArrantEnforceDate") or "")[:10].replace("-", "")
        if arr and arr != "nan" and last_obs_day >= arr:
            return last_price, "정리매매 최종가"
        return 0.0, f"정리매매 미관측({reason[:20]})"
    return 0.0, "폐지 정보 미확보(−100%)"


def rebalance_step(units: dict[str, float], cash: float, p_open: dict[str, float], p_hold: dict[str, float],
                   targets: list[str], untradable: set[str], cost_fn, cap_notional: dict[str, float] | None = None) -> dict:
    """한 번의 리밸런싱. 순수 함수 (테스트용).
    units: 보유 수량, p_open: 체결가(시가), p_hold: 매매 불가 종목 평가가, targets: 목표 종목(매매 가능),
    cost_fn(code, side, notional) → 비용, cap_notional: 종목별 최대 보유 금액(유동성 한도).
    동일 비중 목표, 한도에 걸리면 그 종목만 줄이고 남는 금액은 현금."""
    cap_notional = cap_notional or {}
    value = cash + sum(u * (p_hold[c] if c in untradable else p_open[c]) for c, u in units.items())
    held = {c: units[c] for c in untradable if c in units}
    held_val = sum(u * p_hold[c] for c, u in held.items())
    budget = value - held_val
    cost = 0.0
    tgt: dict[str, float] = {}
    prev_cost = -1.0
    for _ in range(60):       # 비용이 투자 가능 금액을 줄이므로 수렴할 때까지 반복 (비율 비용이면 기하급수적으로 수렴)
        if abs(cost - prev_cost) < 1e-15:
            break
        prev_cost = cost
        w = (budget - cost) / len(targets) if targets else 0.0
        tgt = {c: max(0.0, min(w, cap_notional.get(c, float("inf")))) for c in targets}
        cost = 0.0
        for c, u in units.items():
            if c in held:
                continue
            cur = u * p_open[c]
            delta = tgt.get(c, 0.0) - cur
            if abs(delta) > 1e-12:
                cost += cost_fn(c, "buy" if delta > 0 else "sell", abs(delta))
        for c in targets:
            if c not in units and tgt[c] > 1e-12:
                cost += cost_fn(c, "buy", tgt[c])
    turnover = (sum(abs(tgt.get(c, 0.0) - units.get(c, 0.0) * p_open[c]) for c in set(targets) | (set(units) - set(held)))
                / max(value, 1e-12))
    new_units = dict(held)
    for c, v in tgt.items():
        if v > 0:
            new_units[c] = v / p_open[c]
    new_cash = budget - cost - sum(tgt.values())
    return {"units": new_units, "cash": new_cash, "cost": cost, "value": value, "turnover": turnover,
            "capped": [c for c in targets if tgt[c] < (budget - cost) / len(targets) - 1e-9] if targets else []}


def simulate(P: dict, opts: dict, scenario: str = "base", capital: float | None = None,
             delist_policy: str = "design", unver_hold: str = "price") -> dict:
    """unver_hold: price(관측 가격 그대로 — 4차 Q3 Claude A) / freeze(잔여 미확인 사건이 걸린 보유 기간은 진입가로 고정 — GPT B 근사)."""
    store, adj_close, adj_open, unver = P["store"], P["adj_close"], P["adj_open"], P["unver"]
    groups, costs = load_groups(), load_costs()
    capital = float(capital or costs["capital_krw"])
    end = P["end"]
    end_i = max(x for x in range(len(store.days)) if store.days[x] <= end.replace("-", ""))
    last_total = store.last_valid[-1]          # 종목별 전체 데이터의 마지막 관측 행
    nav_days, nav, holdings_log, ic_log, struct, bench, hit, expo = [], [], [], [], [], [], [], []
    delist_log, unver_log, xs_log = [], [], []
    units: dict[str, float] = {}
    cash, prev = 1.0, []
    sig = P["signals"]
    for k, S in enumerate(sig):
        R, i = S["R"], S["i"]
        evals = [{**r, "metrics": dict(r.get("metrics") or {})} for r in S["evals"]]
        out, ranked, by_g = filter_and_score(evals, S["meta"], dt.datetime.strptime(R, "%Y%m%d").date(), S["tv"],
                                             S["halted"], groups, opts)
        info = {r["stock_code"]: r for r in out}
        e = i + 1
        J = store.codes
        def last_px(c, upto):
            kk = store.last_valid[upto, J[c]]
            return adj_close[kk, J[c]] if kk >= 0 else np.nan
        untradable = {c for c in units if np.isnan(adj_open[e, J[c]])}
        target = select(ranked, prev, untradable=untradable) if ranked else list(untradable)
        targets = [c for c in target if c not in untradable and not np.isnan(adj_open[e, J[c]])]
        p_open = {c: adj_open[e, J[c]] for c in set(units) | set(targets) if c not in untradable}
        p_hold = {c: last_px(c, e) for c in untradable}
        value_now = cash + sum(u * (p_hold[c] if c in untradable else p_open[c]) for c, u in units.items())
        # 유동성 한도 (DESIGN 6.1): 종목별 보유 금액 ≤ 20일 평균 거래대금의 5% (운용금액 기준 환산)
        caps = {}
        for c in targets:
            tv20 = (info.get(c) or {}).get("tv20")
            if tv20:
                caps[c] = 0.05 * tv20 / (capital * max(value_now, 1e-12)) * value_now
        def cost_fn(c, side, notional):
            r = info.get(c) or {}
            return trade_cost(costs, scenario, side, store.days[e], store.mkt.get(c, "KOSPI"), r.get("mcap"),
                              notional * capital, r.get("tv20")) / capital
        st = rebalance_step(units, cash, p_open, p_hold, targets, untradable, cost_fn, caps)
        units, cash, prev = st["units"], st["cash"], list(st["units"])
        held_now = set(units)
        entry = {c: (p_open.get(c) if c in p_open else p_hold.get(c)) for c in units}
        holdings_log.append({"rebalance": R, "exec": store.days[e], "n": len(units), "turnover": round(st["turnover"], 4),
                             "cost": round(st["cost"] / max(st["value"], 1e-12), 6), "liquidity_capped": st["capped"],
                             "cash_weight": round(cash / max(st["value"], 1e-12), 4),
                             "holdings": [{"c": c, "n": store.names.get(c), "rank": (info.get(c) or {}).get("rank"),
                                           "g": (info.get(c) or {}).get("group_id")} for c in units]})
        struct.append({"rebalance": R, "universe": len(S["evals"]), "ranked": len(ranked), "missing_fin": S["missing_fin"],
                       "missing_fin_delisted_later": S.get("missing_fin_delisted_later"), "missing_fin_alive": S.get("missing_fin_alive"),
                       "no_snapshot": S.get("no_snapshot", False), "groups": len(by_g), "held": len(units), "untradable": len(untradable)})
        mc = [(info.get(c) or {}).get("mcap") or 0 for c in units]
        expo.append({"rebalance": R, "groups": dict(Counter((info.get(c) or {}).get("group_id") for c in units)),
                     "mcap_bucket": {"대형(1조+)": sum(m >= 1e12 for m in mc), "중형(2천억~1조)": sum(2e11 <= m < 1e12 for m in mc),
                                     "소형(2천억 미만)": sum(m < 2e11 for m in mc)},
                     "market": dict(Counter(store.mkt.get(c) for c in units))})
        last_round = k + 1 >= len(sig) or sig[k + 1]["i"] + 1 >= len(store.days)
        nxt = end_i if last_round else sig[k + 1]["i"] + 1
        # 보유 기간 중 잔여 미확인 기업행위 노출 (4차 통과 조건 5)
        frozen = set()
        for c in list(units):
            hits = np.nonzero(unver[e + 1: nxt + 1, J[c]])[0]
            if len(hits):
                unver_log.append({"rebalance": R, "code": c, "name": store.names.get(c), "event_day": store.days[e + 1 + hits[0]]})
                if unver_hold == "freeze":
                    frozen.add(c)
        # 일별 평가 + 상장폐지 실현 (DESIGN 6.2)
        for t in range(e, nxt + (1 if last_round else 0)):
            for c in list(units):
                j = J[c]
                if last_total[j] < t and last_total[j] < len(store.days) - 1:
                    lo = store.days[last_total[j]]
                    px, why = delist_proceeds((P.get("delist") or {}).get(c), lo, adj_close[last_total[j], j], delist_policy)
                    cash += units.pop(c) * px
                    delist_log.append({"code": c, "name": store.names.get(c), "last_obs": lo, "rule": why})
            v = cash + sum(u * (entry[c] if c in frozen else last_px(c, t)) for c, u in units.items())
            nav_days.append(store.days[t])
            nav.append(v)
        # IC·적중률: 체결일 시가 → 다음 체결일 시가 (마지막 회차는 구간 끝 종가) — 포트폴리오 구간과 일치
        fwd = {}
        for r in ranked:
            c = r["stock_code"]
            p0 = adj_open[e, J[c]]
            p1 = last_px(c, nxt) if last_round or np.isnan(adj_open[nxt, J[c]]) else adj_open[nxt, J[c]]
            if not np.isnan(p0) and not np.isnan(p1) and p0 > 0:
                fwd[c] = p1 / p0 - 1
        ic_log.append({"rebalance": R, "n": len(fwd),
                       "ic": spearman([r["composite"] for r in ranked if r["stock_code"] in fwd],
                                      [fwd[r["stock_code"]] for r in ranked if r["stock_code"] in fwd])})
        grp_avg = defaultdict(list)
        for r in ranked:
            if r["stock_code"] in fwd:
                grp_avg[r.get("score_group")].append(fwd[r["stock_code"]])
        hit += [fwd[c] > sum(grp_avg[info[c].get("score_group")]) / len(grp_avg[info[c].get("score_group")])
                for c in units if c in fwd and c in info and grp_avg.get(info[c].get("score_group"))]
        bench.append((store.days[e], store.days[nxt], float(np.mean(list(fwd.values()))) if fwd else 0.0))
        # 단면 기록 (D-27 · DESIGN 7.8): 점수·다음 기간 수익·사업군 평균·유니버스 평균·보유 여부 + 12개월(보조)
        gmean = {g: float(np.mean(v)) for g, v in grp_avg.items() if v}
        umean = float(np.mean(list(fwd.values()))) if fwd else None
        t12 = e + 250 if e + 250 < len(store.days) else None
        rows = []
        for r in ranked:
            c = r["stock_code"]
            if c not in fwd or r.get("score_group") not in gmean:
                continue
            f12 = None
            if t12 is not None:
                p0, p1 = adj_open[e, J[c]], last_px(c, t12)
                f12 = float(p1 / p0 - 1) if p0 > 0 and not np.isnan(p1) else None
            rows.append({"c": c, "s": r["composite"], "f": float(fwd[c]), "g": gmean[r["score_group"]], "u": umean,
                         "h": c in held_now, "f12": f12})
        xs_log.append({"rebalance": R, "start": store.days[e], "end": store.days[nxt], "rows": rows,
                       "nav0": None, "nav1": None})
        ic_log[-1]["ic_sector"] = spearman([x["s"] for x in rows], [x["f"] - x["g"] for x in rows])
    return {"nav_days": nav_days, "nav": nav, "holdings": holdings_log, "ic": ic_log, "structure": struct,
            "bench_ew": bench, "hit": hit, "exposure": expo, "delistings": delist_log, "unverified_held": unver_log,
            "xs": xs_log,
            "params": {"capital": capital, "delist_policy": delist_policy, "unver_hold": unver_hold, "scenario": scenario}}


def _mean_ci(v: list[float]) -> dict:
    """리밸런싱 단위 평균 ± 1.96·표준오차 (같은 분기 안의 종목 관측은 독립이 아니므로 분기를 한 관측으로)."""
    v = [x for x in v if x is not None and math.isfinite(x)]
    if not v:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    m = float(np.mean(v))
    se = float(np.std(v, ddof=1) / math.sqrt(len(v))) if len(v) > 1 else float("nan")
    return {"mean": m, "lo": m - 1.96 * se if len(v) > 1 else None, "hi": m + 1.96 * se if len(v) > 1 else None, "n": len(v)}


def cross_section(xs: list[dict]) -> dict:
    """5분위(주)·절대 점수 10점 구간(설명용) × 3개월(주)·12개월(보조). 1분위 = 점수 최상위.
    승률 = 다음 기간 수익률이 사업군 평균(주)·유니버스 평균(병기)을 넘은 비율."""
    Q = 5
    qs = {q: {"sec_ex": [], "uni_ex": [], "win_sec": [], "win_uni": [], "n": 0} for q in range(1, Q + 1)}
    bands = defaultdict(lambda: {"win_sec": [], "win_uni": [], "sec_ex": [], "n": 0})
    q12 = {q: {"sec_ex": [], "win_uni": [], "n": 0} for q in range(1, Q + 1)}
    ics = []
    for X in xs:
        rows = sorted(X["rows"], key=lambda x: -x["s"])
        if len(rows) < Q * 2:
            continue
        ics.append(spearman([x["s"] for x in rows], [x["f"] - x["g"] for x in rows]))
        for k, x in enumerate(rows):
            x["q"] = k * Q // len(rows) + 1
        for q in range(1, Q + 1):
            sub = [x for x in rows if x["q"] == q]
            qs[q]["sec_ex"].append(float(np.mean([x["f"] - x["g"] for x in sub])))
            qs[q]["uni_ex"].append(float(np.mean([x["f"] - x["u"] for x in sub])))
            qs[q]["win_sec"].append(float(np.mean([x["f"] > x["g"] for x in sub])))
            qs[q]["win_uni"].append(float(np.mean([x["f"] > x["u"] for x in sub])))
            qs[q]["n"] += len(sub)
            s12 = [x for x in sub if x.get("f12") is not None]
            if s12:
                umean12 = float(np.mean([x["f12"] for x in rows if x.get("f12") is not None]))
                q12[q]["sec_ex"].append(float(np.mean([x["f12"] for x in s12])) - umean12)
                q12[q]["win_uni"].append(float(np.mean([x["f12"] > umean12 for x in s12])))
                q12[q]["n"] += len(s12)
        by_b = defaultdict(list)
        for x in rows:
            by_b[min(int(x["s"] // 10) * 10, 90)].append(x)
        for b, sub in by_b.items():
            bands[b]["win_sec"].append(float(np.mean([x["f"] > x["g"] for x in sub])))
            bands[b]["win_uni"].append(float(np.mean([x["f"] > x["u"] for x in sub])))
            bands[b]["sec_ex"].append(float(np.mean([x["f"] - x["g"] for x in sub])))
            bands[b]["n"] += len(sub)
    quint = {q: {"sector_excess": _mean_ci(d["sec_ex"]), "universe_excess": _mean_ci(d["uni_ex"]),
                 "win_vs_sector": _mean_ci(d["win_sec"]), "win_vs_universe": _mean_ci(d["win_uni"]), "obs": d["n"]}
             for q, d in qs.items()}
    means = [quint[q]["sector_excess"]["mean"] for q in range(1, Q + 1)]
    inv = sum(1 for a, b in zip(means, means[1:]) if a is not None and b is not None and b > a) if all(m is not None for m in means) else None
    ic = [x for x in ics if x is not None]
    ic_sd = float(np.std(ic, ddof=1)) if len(ic) > 1 else None
    return {"quintiles": quint,
            "spread_top_minus_bottom": (means[0] - means[-1]) if None not in means else None,
            "adjacent_inversions": inv,
            "ic_sector": {"mean": float(np.mean(ic)) if ic else None, "std": ic_sd, "n": len(ic),
                          "ir": float(np.mean(ic) / ic_sd) if ic_sd else None,
                          "t": float(np.mean(ic) / ic_sd * math.sqrt(len(ic))) if ic_sd else None},
            "bands_10pt": {f"{b}~{b + 10}": {"win_vs_sector": _mean_ci(d["win_sec"]), "win_vs_universe": _mean_ci(d["win_uni"]),
                                             "sector_excess": _mean_ci(d["sec_ex"]), "obs": d["n"]} for b, d in sorted(bands.items())},
            "quintiles_12m": {q: {"universe_excess_mean": float(np.mean(d["sec_ex"])) if d["sec_ex"] else None,
                                  "win_vs_universe_mean": float(np.mean(d["win_uni"])) if d["win_uni"] else None, "obs": d["n"],
                                  "note": "12개월 수익은 분기마다 겹침 → 표준오차·신뢰구간 없음 (설명용)"} for q, d in q12.items()}}


def _ann(rets: list[float], years: float) -> float | None:
    if not rets or years <= 0:
        return None
    return float(np.prod([1 + r for r in rets]) ** (1 / years) - 1)


def judge(sim: dict, res: dict, conservative_excess: float | None = None) -> dict:
    """DESIGN 7.8 합격 기준 판정. 구간(개발/검증)과 관계없이 같은 계산 — 개발은 방향 진단, 판정은 검증 구간."""
    xs, nav, days = sim["xs"], sim["nav"], sim["nav_days"]
    pos = {d: k for k, d in enumerate(days)}
    per = []                                   # (시작일, 포트폴리오 구간 수익, 유니버스 구간 수익)
    for X in xs:
        a, b = pos.get(X["start"]), pos.get(X["end"])
        if a is None or b is None or not nav[a] or not X["rows"]:
            continue
        per.append((X["start"], nav[b] / nav[a] - 1, X["rows"][0]["u"]))
    if not per:
        return {"status": "자료 부족"}
    years = (dt.datetime.strptime(xs[-1]["end"], "%Y%m%d") - dt.datetime.strptime(xs[0]["start"], "%Y%m%d")).days / 365.25
    port_ann, uni_ann = _ann([p[1] for p in per], years), _ann([p[2] for p in per], years)
    excess = (port_ann - uni_ann) if port_ann is not None and uni_ann is not None else None
    held_sec = [float(np.mean([x["f"] - x["g"] for x in X["rows"] if x["h"]])) for X in xs if any(x["h"] for x in X["rows"])]
    cs = res.get("cross_section") or cross_section(xs)
    top = cs["quintiles"][1]["win_vs_sector"]
    yearly = defaultdict(lambda: [1.0, 1.0])
    for d, pr, ur in per:
        yearly[d[:4]][0] *= 1 + pr
        yearly[d[:4]][1] *= 1 + ur
    yr_pos = sum(1 for v in yearly.values() if v[0] > v[1])
    # 누적 기여 상위 3종목 제외 (보유 종목 동일비중 근사)
    contrib = defaultdict(float)
    for X in xs:
        h = [x for x in X["rows"] if x["h"]]
        for x in h:
            contrib[x["c"]] += (x["f"] - x["u"]) / len(h)
    top3 = [c for c, _ in sorted(contrib.items(), key=lambda kv: -kv[1])[:3]]
    eq_all, eq_wo = [], []
    for X in xs:
        h = [x for x in X["rows"] if x["h"]]
        if not h:
            continue
        eq_all.append(float(np.mean([x["f"] for x in h])))
        h2 = [x for x in h if x["c"] not in top3]
        eq_wo.append(float(np.mean([x["f"] for x in h2])) if h2 else X["rows"][0]["u"])
    wo_ex = (_ann(eq_wo, years) or 0) - (uni_ann or 0) if eq_wo else None
    # 구간 단위 MDD (포트폴리오·유니버스 같은 해상도)
    def mdd(rs):
        v = np.cumprod([1] + [1 + r for r in rs])
        return float((v / np.maximum.accumulate(v) - 1).min())
    mdd_p, mdd_u = mdd([p[1] for p in per]), mdd([p[2] for p in per])
    ic = cs["ic_sector"]
    g = {
        "1_초과수익": {"value": {"annual_excess_vs_universe": excess, "held_vs_sector_mean": float(np.mean(held_sec)) if held_sec else None},
                     "pass": excess is not None and excess >= 0.03 and bool(held_sec) and float(np.mean(held_sec)) > 0,
                     "rule": "연환산 ≥ 3%p (유니버스 동일비중 대비) + 보유 종목 사업군 대비 > 0"},
        "2_5분위": {"value": {"spread": cs["spread_top_minus_bottom"], "inversions": cs["adjacent_inversions"]},
                  "pass": (cs["spread_top_minus_bottom"] or 0) > 0 and cs["adjacent_inversions"] is not None and cs["adjacent_inversions"] <= 1,
                  "rule": "최상위 − 최하위 > 0, 인접 역전 ≤ 1"},
        "3_IC": {"value": ic, "pass": (ic["mean"] or 0) > 0 and (ic["ir"] or 0) >= 0.3, "rule": "평균 > 0, IR ≥ 0.3 (t값은 공개만)"},
        "4_승률": {"value": top, "pass": False if (top["mean"] or 0) <= 0.5 else (True if top["lo"] is not None and top["lo"] > 0.5 else None),
                 "rule": "최상위 5분위 사업군 대비 승률 > 50%, 95% 하한 > 50% (아니면 미판정)"},
        "5_안정성": {"value": {"years_positive": yr_pos, "years": len(yearly), "top3_removed": top3, "excess_without_top3": wo_ex},
                   "pass": yr_pos * 2 >= len(yearly) and wo_ex is not None and wo_ex > 0,
                   "rule": "연도 절반 이상 초과 > 0, 상위 3종목 제외해도 > 0 (동일비중 근사)"},
    }
    flags = []
    if mdd_p < mdd_u - 0.05:
        flags.append(f"사용자 재검토: 구간 MDD {mdd_p:.1%} (유니버스 {mdd_u:.1%})")
    if conservative_excess is None:
        flags.append("보수 비용 미측정 (bt-robust)")
    elif conservative_excess <= 0:
        flags.append("비용 민감: 보수 비용에서 초과수익 ≤ 0 — '통과' 표현 금지")
    vals = [x["pass"] for x in g.values()]
    status = "통과" if all(v is True for v in vals) else ("미판정" if all(v is not False for v in vals) else "실패")
    if status == "통과" and any(f.startswith("비용 민감") for f in flags):
        status = "통과(비용 민감)"
    return {"status": status, "gates": g, "flags": flags,
            "annual": {"portfolio_period_based": port_ann, "universe_ew": uni_ann, "mdd_period_portfolio": mdd_p, "mdd_period_universe": mdd_u},
            "data_gate": "사람 판정 — survivorship_check·limitations 수치 공개 (DESIGN 7.2)",
            "note": "개발 구간: 방향 진단. 판정은 검증 구간 (DESIGN 7.8)"}


def _survivorship(struct: list[dict]) -> dict:
    """재무 미매핑 비율: 이후 상장폐지 종목 vs 생존 종목. 차이가 크면 정식 성과로 인정하지 않는다 (4차 통과 조건 3)."""
    dead = sum(s.get("missing_fin_delisted_later") or 0 for s in struct)
    alive = sum(s.get("missing_fin_alive") or 0 for s in struct)
    uni = sum(s.get("universe") or 0 for s in struct)
    return {"missing_delisted_later": dead, "missing_alive": alive, "evaluated": uni,
            "no_snapshot_rounds": sum(1 for s in struct if s.get("no_snapshot"))}


def summarize(sim: dict, P: dict) -> dict:
    nav, days = sim["nav"], sim["nav_days"]
    ics = [x["ic"] for x in sim["ic"] if x["ic"] is not None]
    res = {"perf": perf(nav, days),
           "ic": {"mean": float(np.mean(ics)) if ics else None, "std": float(np.std(ics, ddof=1)) if len(ics) > 1 else None,
                  "ir": float(np.mean(ics) / np.std(ics, ddof=1)) if len(ics) > 1 and np.std(ics) > 0 else None, "n": len(ics)},
           "hit_rate": sum(sim["hit"]) / len(sim["hit"]) if sim["hit"] else None,
           "avg_turnover": float(np.mean([h["turnover"] for h in sim["holdings"]])) if sim["holdings"] else None,
           "avg_cost_per_rebalance": float(np.mean([h["cost"] for h in sim["holdings"]])) if sim["holdings"] else None,
           "benchmark_universe_ew": {"total_return": float(np.prod([1 + b[2] for b in sim["bench_ew"]]) - 1) if sim["bench_ew"] else None,
                                     "definition": "순위 대상 전체 동일비중, 체결일 시가→다음 체결일 시가, 비용 미차감"},
           "delistings": {"count": len(sim.get("delistings", [])), "by_rule": dict(Counter(d["rule"].split("(")[0] for d in sim.get("delistings", [])))},
           "unverified_held_events": len(sim.get("unverified_held", [])),
           "liquidity_capped_total": sum(len(h.get("liquidity_capped", [])) for h in sim["holdings"]),
           "survivorship_check": _survivorship(sim["structure"]),
           "params": sim.get("params")}
    res["cross_section"] = cross_section(sim.get("xs") or [])
    res["judgement"] = judge(sim, res) if sim.get("xs") else {"status": "자료 부족"}
    # 지수 벤치마크와 베타·국면별 성과
    for key, ser in P["index"].items():
        lv = [ser.get(d) for d in days]
        if days and all(v is not None for v in (lv[0], lv[-1])):
            ix = [v for v in lv if v is not None]
            res[f"benchmark_{key}"] = perf(ix, [d for d, v in zip(days, lv) if v is not None])
    kospi = P["index"].get("KOSPI", {})
    pr, mr = [], []
    for a, b, na, nb in zip(days, days[1:], nav, nav[1:]):
        if a in kospi and b in kospi and na:
            pr.append(nb / na - 1)
            mr.append(kospi[b] / kospi[a] - 1)
    if len(pr) > 20:
        x, y = np.array(mr), np.array(pr)
        res["beta_kospi"] = float(np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1))
    months = defaultdict(list)
    for d, v in zip(days, nav):
        months[d[:6]].append((d, v))
    reg = defaultdict(list)
    ms = sorted(months)
    for a, b in zip(ms, ms[1:]):
        d0, v0 = months[a][-1]
        d1, v1 = months[b][-1]
        if d0 in kospi and d1 in kospi:
            m = kospi[d1] / kospi[d0] - 1
            reg["상승(+2%↑)" if m > .02 else ("하락(−2%↓)" if m < -.02 else "횡보")].append((v1 / v0 - 1) - m)
    res["regime_excess_vs_kospi_monthly"] = {k: {"months": len(v), "avg_excess": float(np.mean(v))} for k, v in reg.items()}
    years = defaultdict(list)
    for d, v in zip(days, nav):
        years[d[:4]].append(v)
    ys = sorted(years)
    res["yearly"] = {y: float(years[y][-1] / (years[ys[n - 1]][-1] if n else years[y][0]) - 1) for n, y in enumerate(ys)}
    return res


def _write(out_dir, P, sim, res, mode, smoke):
    out_dir.mkdir(parents=True, exist_ok=True)
    if not smoke:
        with (out_dir / "nav.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "nav"])
            w.writerows(zip(sim["nav_days"], sim["nav"]))
        with (out_dir / "ic.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["rebalance", "ic", "n"])
            w.writeheader()
            w.writerows(sim["ic"])
        (out_dir / "exposure.json").write_text(json.dumps(sim["exposure"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "holdings.json").write_text(json.dumps(sim["holdings"], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _log_run(mode, start, end, strat, scenario, adj_policy, variant, res):
    log_path = REPORTS / "backtest" / "runs.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_at", "mode", "window", "strategy", "variant", "scenario", "adj_policy", "cagr", "mdd", "ic_mean"])
        p = res.get("perf", {})
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), mode, f"{start}~{end}", strat.get("version"), variant,
                    scenario, adj_policy, p.get("cagr"), p.get("mdd"), (res.get("ic") or {}).get("mean")])


LIMITS = ["재무 값: 첫 정정 전 시점은 최초본 XBRL(D-17), 정정 3회 이상 중간본은 미복원", "업종은 현재 분류",
          "코스피 관리종목 미반영(D-15)"]


def run(start: str = DEV[0], end: str = DEV[1], mode: str = "dev", scenario: str = "base",
        adj_policy: str = "confirmed_only", capital: float | None = None, delist_policy: str = "design",
        unver_hold: str = "price") -> dict:
    """전략 v1 단일 실행. mode: dev / validation(전략 frozen 필요) / smoke(구조 점검, 수익률 미출력)."""
    strat = guard(mode, start, end)
    P = prepare(start, end, adj_policy)
    sim = simulate(P, {}, scenario, capital, delist_policy, unver_hold)
    smoke = mode == "smoke"
    res = {"mode": mode, "window": [start, end], "scenario": scenario, "adj_policy": adj_policy, "variant": "v1",
           "strategy": strat.get("version"), "rebalances": len(sim["holdings"]), "structure": sim["structure"],
           "limitations": LIMITS + ["미확인 기업행위 처리 정책: " + adj_policy]}
    if smoke:
        res.update({"note": "기계 점검 — 수익률은 의도적으로 계산·출력하지 않음 (검증 구간 보호)",
                    "nav_points": len(sim["nav"]), "nav_finite": bool(all(math.isfinite(x) for x in sim["nav"])),
                    "delistings": len(sim["delistings"]), "unverified_held_events": len(sim["unverified_held"]),
                    "survivorship_check": _survivorship(sim["structure"])})
    else:
        res.update(summarize(sim, P))
        _log_run(mode, start, end, strat, scenario, adj_policy, "v1", res)
    out_dir = REPORTS / "backtest" / f"{mode}_{start}_{end}_{scenario}_{adj_policy}"
    _write(out_dir, P, sim, res, mode, smoke)
    if not smoke:
        from .backtest_view import build
        vf = REPORTS / "backtest" / "variants" / f"{start}_{end}_{scenario}" / "comparison.json"
        build(out_dir, vf if vf.exists() else None, P["index"])
    log(f"백테스트 완료 ({mode})")
    return {k: v for k, v in res.items() if k != "structure"} | {"structure_last": sim["structure"][-1] if sim["structure"] else None}


def run_variants(start: str = DEV[0], end: str = DEV[1], scenario: str = "base", adj_policy: str = "confirmed_only",
                 names: list[str] | None = None, mode: str = "dev") -> dict:
    """DESIGN 7.5 비교 실험 — 개발 구간 전용(smoke는 구조만). 평가는 1회, 변형별 점수·포트폴리오만 다시 계산."""
    if mode == "validation":
        raise ValueError("비교 실험은 개발 구간에서만 (DESIGN 7.5)")
    strat = guard(mode, start, end)
    P = prepare(start, end, adj_policy)
    table = {}
    for name in names or list(VARIANTS):
        sim = simulate(P, VARIANTS[name], scenario)
        if mode == "smoke":
            table[name] = {"rebalances": len(sim["holdings"]), "nav_finite": bool(all(math.isfinite(x) for x in sim["nav"])),
                           "avg_held": float(np.mean([h["n"] for h in sim["holdings"]])) if sim["holdings"] else 0}
            continue
        res = summarize(sim, P)
        table[name] = {"cagr": res["perf"].get("cagr"), "mdd": res["perf"].get("mdd"), "sharpe": res["perf"].get("sharpe_rf0"),
                       "ic_mean": res["ic"]["mean"], "ic_ir": res["ic"]["ir"], "hit_rate": res["hit_rate"],
                       "turnover": res["avg_turnover"]}
        _write(REPORTS / "backtest" / "variants" / f"{start}_{end}_{scenario}" / name, P, sim, res | {"variant": name}, mode, False)
        _log_run(mode, start, end, strat, scenario, adj_policy, name, res)
    out = {"mode": mode, "window": [start, end], "scenario": scenario, "adj_policy": adj_policy, "variants": table,
           "rule": "채택 기준(EXPERIMENTS.md 규칙 5): 비용 차감 후 IC 평균 개선 그리고 최대낙폭 악화 없음"}
    d = REPORTS / "backtest" / "variants" / f"{start}_{end}_{scenario}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "comparison.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if mode != "smoke" and "v1" in table:
        from .backtest_view import build
        build(d / "v1", d / "comparison.json", P["index"])
    return out


def run_robustness(start: str = DEV[0], end: str = DEV[1], scenario: str = "base", mode: str = "dev") -> dict:
    """4차 협의 결과의 민감도 묶음: 미확인 기업행위 정책 A·B·C, 상장폐지 회수 규칙, 운용금액, 비용 시나리오.
    Q3의 남은 이견(A vs B)은 이 숫자 비교로 사용자가 결정한다."""
    if mode == "validation":
        raise ValueError("민감도 분석은 개발 구간에서만")
    guard(mode, start, end)
    P = prepare(start, end, "confirmed_only")
    cases = {
        "Q3-A 확인된 사건만 보정·잔여 미보정 (기본)": dict(adj="confirmed_only", unver_hold="price"),
        "Q3-B 잔여 미확인 보유 기간 고정": dict(adj="confirmed_only", unver_hold="freeze"),
        "Q3-C 전부 보정": dict(adj="all", unver_hold="price"),
        "상장폐지 마지막가 (민감도)": dict(delist_policy="last"),
        "운용 1천만원": dict(capital=1.0e7), "운용 10억원": dict(capital=1.0e9),
        "비용 낙관": dict(scenario="optimistic"), "비용 보수": dict(scenario="conservative"),
    }
    table = {}
    for name, c in cases.items():
        P2 = dict(P)
        a = c.get("adj", "confirmed_only")
        P2["adj_close"], P2["adj_open"] = P["adj"][a]
        sim = simulate(P2, {}, c.get("scenario", scenario), c.get("capital"), c.get("delist_policy", "design"), c.get("unver_hold", "price"))
        if mode == "smoke":
            table[name] = {"nav_finite": bool(all(math.isfinite(x) for x in sim["nav"])), "delistings": len(sim["delistings"]),
                           "unverified_held": len(sim["unverified_held"])}
            continue
        r = summarize(sim, P2)
        table[name] = {"cagr": r["perf"].get("cagr"), "mdd": r["perf"].get("mdd"), "ic_mean": r["ic"]["mean"],
                       "delistings": r["delistings"]["count"], "unverified_held": r["unverified_held_events"],
                       "liquidity_capped": r["liquidity_capped_total"]}
    out = {"mode": mode, "window": [start, end], "cases": table}
    d = REPORTS / "backtest" / "robustness"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mode}_{start}_{end}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
