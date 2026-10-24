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
from collections import defaultdict

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
        return best or (self._snap[0][1] if self._snap else {})

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


# ── 실행 ──
def run(start: str = DEV[0], end: str = DEV[1], mode: str = "dev", scenario: str = "base",
        adj_policy: str = "confirmed_only") -> dict:
    """mode: dev(개발 구간) / validation(검증 구간 — 전략 frozen 필요) / smoke(구조 점검, 수익률 미출력)."""
    with (CONFIG / "strategy_v1.yaml").open(encoding="utf-8") as f:
        strat = yaml.safe_load(f)
    if mode == "dev" and not (DEV[0] <= start and end <= DEV[1]):
        raise ValueError(f"개발 구간({DEV[0]} ~ {DEV[1]}) 밖은 dev 모드로 실행할 수 없음")
    if mode == "validation" and strat.get("status") != "frozen":
        raise ValueError("검증 구간은 전략 버전이 frozen일 때만 실행 (D-21)")
    if mode == "dev" and end >= VALIDATION_START:
        raise ValueError("검증 구간과 겹침")

    store = PriceStore()
    cum, unver = store.adjustment(adj_policy)
    adj_close, adj_open = store.close * cum, store.open * cum
    cal = TradingCalendar.from_l0()
    groups = load_groups()
    costs = load_costs()
    from .collect.dart import DART
    corp = {c["stock_code"]: c["corp_code"] for c in DART(budget=1).corpcode() if c["stock_code"]}
    period_cache: dict[str, tuple] = {}

    rdays = rebalance_days(store.days, start, end)
    log(f"백테스트 {mode}: 리밸런싱 {len(rdays)}회 {rdays[:2]}…")
    nav_days, nav, holdings_log, ic_log, struct = [], [], [], [], []
    units: dict[str, float] = {}
    cash = 1.0
    prev: list[str] = []
    bench = []
    hit = []
    for k, R in enumerate(rdays):
        i = store.day_idx[R]
        if i + 1 >= len(store.days):
            break
        snap = store.snapshot(R)
        ctx = AsOf(store, i, adj_close, unver)
        evals, meta, miss = [], {}, 0
        for code, b in snap.items():
            if b.get("KIND_STKCERT_TP_NM") != "보통주" or b.get("SECUGRP_NM") != "주권" or "SPAC" in (b.get("SECT_TP_NM") or ""):
                continue
            j = store.codes.get(code)
            if j is None or np.isnan(store.close[i, j]):
                continue
            cc = corp.get(code)
            if not cc:
                miss += 1
                continue
            if cc not in period_cache:
                try:
                    period_cache[cc] = company_periods({"corp_code": cc, "stock_code": code, "name": b.get("ISU_ABBRV"), "category": ""}, cal)
                except FileNotFoundError:
                    period_cache[cc] = (None, None)
            base, periods = period_cache[cc]
            if base is None:
                miss += 1
                continue
            if periods is None:
                ev = dict(base)
            else:
                try:
                    ev = evaluate_company(base, periods, ctx)
                except Exception as e:  # noqa: BLE001
                    ev = {**base, "status": "error", "error": str(e)[:120]}
            evals.append(ev)
            meta[code] = {"market": store.mkt.get(code), "list_dd": b.get("LIST_DD"),
                          "sect": {1: "관리종목", 2: "투자주의환기종목"}.get(int(store.admin[i, j]), "")}
        tv = {c: [x for x in store.tv[max(0, i - 19): i + 1, store.codes[c]] if not np.isnan(x)] for c in meta}
        halted = {c for c, v in tv.items() if len(v) >= 5 and all(x == 0 for x in v[-5:])}
        out, ranked, by_g = filter_and_score(evals, meta, dt.datetime.strptime(R, "%Y%m%d").date(), tv, halted, groups)

        # 체결일 e = R 다음 거래일 시가
        e = i + 1
        def px_open(c):
            j = store.codes[c]
            return adj_open[e, j]
        def px_last(c, upto):
            j = store.codes[c]
            kk = store.last_valid[upto, j]
            return adj_close[kk, j] if kk >= 0 else np.nan

        # 현재 보유 평가 (e 시가, 매매 불가면 마지막 관측가)
        untradable = {c for c in units if np.isnan(px_open(c)) and store.last_valid[e, store.codes[c]] >= e - 5}
        value = cash + sum(u * (px_open(c) if not np.isnan(px_open(c)) else px_last(c, e)) for c, u in units.items())
        target = select(ranked, prev, untradable=untradable) if ranked else list(untradable)
        tradable_target = [c for c in target if c not in untradable and not np.isnan(px_open(c))]
        # 매도
        info = {r["stock_code"]: r for r in out}
        cost = 0.0
        new_units = {c: units[c] for c in untradable if c in units}
        for c, u in units.items():
            if c in new_units:
                continue
            p = px_open(c)
            p = px_last(c, e) if np.isnan(p) else p
            notional = u * p
            keep_w = (c in tradable_target)
            if not keep_w:
                cost += trade_cost(costs, scenario, "sell", store.days[e], store.mkt.get(c, "KOSPI"),
                                   (info.get(c) or {}).get("mcap"), notional, (info.get(c) or {}).get("tv20"))
        # 매수·비중 조정 (동일 비중)
        invest = value - cost - sum(new_units[c] * px_last(c, e) for c in new_units)
        w = invest / len(tradable_target) if tradable_target else 0.0
        for c in tradable_target:
            p = px_open(c)
            old = units.get(c, 0.0) * p
            delta = w - old
            if abs(delta) > 1e-12:
                side = "buy" if delta > 0 else "sell"
                cost += trade_cost(costs, scenario, side, store.days[e], store.mkt.get(c, "KOSPI"),
                                   (info.get(c) or {}).get("mcap"), abs(delta), (info.get(c) or {}).get("tv20"))
        invest = value - cost - sum(new_units[c] * px_last(c, e) for c in new_units)
        w = invest / len(tradable_target) if tradable_target else 0.0
        turnover = sum(abs(w - units.get(c, 0.0) * px_open(c)) for c in tradable_target) / max(value, 1e-12)
        for c in tradable_target:
            new_units[c] = w / px_open(c)
        cash = 0.0 if tradable_target else value - cost
        units = new_units
        prev = list(units)
        holdings_log.append({"rebalance": R, "exec": store.days[e], "n": len(units), "turnover": round(turnover, 4),
                             "cost": round(cost / max(value, 1e-12), 6),
                             "holdings": [{"c": c, "n": store.names.get(c), "rank": (info.get(c) or {}).get("rank"),
                                           "g": (info.get(c) or {}).get("group_id")} for c in units]})
        struct.append({"rebalance": R, "universe": len(evals), "ranked": len(ranked), "missing_fin": miss,
                       "groups": len(by_g), "held": len(units), "untradable": len(untradable)})

        # 다음 체결일까지 일별 평가 (마지막 회차는 구간 끝까지만)
        end_i = max(x for x in range(len(store.days)) if store.days[x] <= end.replace("-", ""))
        last_round = k + 1 >= len(rdays) or store.day_idx[rdays[k + 1]] + 1 >= len(store.days)
        nxt = end_i if last_round else store.day_idx[rdays[k + 1]] + 1
        for t in range(e, nxt + (1 if last_round else 0)):
            v = cash + sum(u * px_last(c, t) for c, u in units.items())
            nav_days.append(store.days[t])
            nav.append(v)

        # IC·적중률: 이번 신호 vs 다음 체결일까지 수익률
        fwd = {}
        for r in ranked:
            c = r["stock_code"]
            p0, p1 = px_open(c), px_last(c, nxt)
            if not np.isnan(p0) and not np.isnan(p1) and p0 > 0:
                fwd[c] = p1 / p0 - 1
        ic = spearman([r["composite"] for r in ranked if r["stock_code"] in fwd], [fwd[r["stock_code"]] for r in ranked if r["stock_code"] in fwd])
        grp_avg = defaultdict(list)
        for r in ranked:
            if r["stock_code"] in fwd:
                grp_avg[r["group_id"]].append(fwd[r["stock_code"]])
        hits = [fwd[c] > sum(grp_avg[info[c]["group_id"]]) / len(grp_avg[info[c]["group_id"]])
                for c in units if c in fwd and c in info and grp_avg.get(info[c]["group_id"])]
        hit += hits
        ic_log.append({"rebalance": R, "ic": ic, "n": len(fwd)})
        # 벤치마크 (DESIGN 7.1-a): 같은 시점 순위 대상 전체 동일비중, 비용 미차감
        bench.append((store.days[e], store.days[nxt], float(np.mean(list(fwd.values()))) if fwd else 0.0))

    res = {"mode": mode, "window": [start, end], "scenario": scenario, "adj_policy": adj_policy,
           "strategy": strat.get("version"), "rebalances": len(holdings_log), "structure": struct,
           "limitations": ["재무 값은 최신 정정본(최초본 복원 D-17 전)", "업종은 현재 분류", "코스피 관리종목 미반영(D-15)",
                           "미확인 기업행위 처리 정책: " + adj_policy]}
    out_dir = REPORTS / "backtest" / f"{mode}_{start}_{end}_{scenario}_{adj_policy}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if mode == "smoke":
        res["note"] = "기계 점검 — 수익률은 의도적으로 계산·출력하지 않음 (검증 구간 보호)"
        res["nav_points"] = len(nav)
        res["nav_finite"] = bool(all(math.isfinite(x) for x in nav))
    else:
        ics = [x["ic"] for x in ic_log if x["ic"] is not None]
        res.update({"perf": perf(nav, nav_days),
                    "ic": {"mean": float(np.mean(ics)) if ics else None, "std": float(np.std(ics, ddof=1)) if len(ics) > 1 else None,
                           "ir": float(np.mean(ics) / np.std(ics, ddof=1)) if len(ics) > 1 and np.std(ics) > 0 else None, "n": len(ics)},
                    "hit_rate": sum(hit) / len(hit) if hit else None,
                    "benchmark_universe_ew": {"total_return": float(np.prod([1 + b[2] for b in bench]) - 1) if bench else None,
                                              "periods": len(bench)},
                    "avg_turnover": float(np.mean([h["turnover"] for h in holdings_log])) if holdings_log else None,
                    "avg_cost_per_rebalance": float(np.mean([h["cost"] for h in holdings_log])) if holdings_log else None})
        with (out_dir / "nav.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "nav"])
            w.writerows(zip(nav_days, nav))
        with (out_dir / "ic.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["rebalance", "ic", "n"])
            w.writeheader()
            w.writerows(ic_log)
    (out_dir / "holdings.json").write_text(json.dumps(holdings_log, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    # 실험 기록 (DESIGN 7.4) — 기계 점검은 실험이 아니므로 기록하지 않음
    if mode != "smoke":
        log_path = REPORTS / "backtest" / "runs.csv"
        new = not log_path.exists()
        with log_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["run_at", "mode", "window", "strategy", "scenario", "adj_policy", "cagr", "mdd", "ic_mean"])
            p = res.get("perf", {})
            w.writerow([dt.datetime.now().isoformat(timespec="seconds"), mode, f"{start}~{end}", strat.get("version"),
                        scenario, adj_policy, p.get("cagr"), p.get("mdd"), (res.get("ic") or {}).get("mean")])
    log(f"백테스트 완료: {out_dir}")
    return {k: v for k, v in res.items() if k != "structure"} | {"structure_last": struct[-1] if struct else None}
