"""현재 시점 스크리너 (방침 B): 전 종목 업종 대비 점수 — DESIGN 5장 / config/strategy_v1.yaml.

흐름: 전 종목 L1(재무)·KRX(시세) → 회사별 지표(pipeline.build_company 재사용)
     → 유니버스 필터 → 사업군 배정(config/peer_groups_v1.csv) → 사업군 내 윈저라이즈·백분위
     → 묶음 점수(최소 충족 조건) → 종합 점수 → 위험 필터(제외/경고) → 순위
결측은 50점으로 채우지 않는다. 점수 공식은 '가설'이며 과거 성과로 검증되지 않았다 (화면에 표시).
"""
from __future__ import annotations

import csv
import datetime as dt
import math
from collections import Counter, defaultdict

import yaml

from .normalize.calendar import TradingCalendar
from .pipeline import Prices, build_company
from .score.metrics import Winsor, percentile_ranks
from .storage import L1, L2, load_l0
from .util import CONFIG, REPORTS, log, read_gz_json, write_gz_json

BUCKETS = {
    "value": ["E/P", "B/P", "EBIT/EV", "PBR-ROE잔차"],
    "quality": ["ROE", "GP/A", "CFO/NI"],
    "trend": ["12-1모멘텀", "영업이익증가율"],
    "safety": ["이자보상배율"],
}
WEIGHTS = {"value": 0.40, "quality": 0.30, "trend": 0.20, "safety": 0.10}
MIN_MCAP, MIN_TV20 = 5.0e10, 5.0e8


# ── 사업군 ──
def load_groups() -> list[tuple[str, str, str]]:
    """[(접두어, 그룹ID, 그룹명)] 긴 접두어 우선."""
    out = []
    with (CONFIG / "peer_groups_v1.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for p in r["ksic_prefixes"].split(";"):
                out.append((p.strip(), r["group_id"], r["group_name"]))
    return sorted(out, key=lambda x: -len(x[0]))


def group_of(ksic: str | None, groups) -> tuple[str | None, str | None]:
    k = (ksic or "").strip()
    for p, gid, gname in groups:
        if k.startswith(p):
            return gid, gname
    return None, None


def reliability(n: int) -> str:
    return "높음" if n >= 15 else ("보통" if n >= 10 else "낮음")


# ── PBR-ROE 잔차 (DESIGN 5.3) ──
def pbr_roe_residuals(rows: list[dict]) -> dict[str, float | None]:
    """한 사업군: log(PBR) = a + b·ROE 최소제곱. 유효 표본 ≥ 15, R² ≥ 0.1일 때만. 반환은 −잔차(클수록 싸다)."""
    pts = [(r["stock_code"], math.log(1 / r["metrics"]["B/P"]), r["metrics"]["ROE"]) for r in rows
           if r["metrics"].get("B/P") and r["metrics"]["B/P"] > 0 and r["metrics"].get("ROE") is not None]
    out = {r["stock_code"]: None for r in rows}
    if len(pts) < 15:
        return out
    xs = [p[2] for p in pts]
    ys = [p[1] for p in pts]
    w = Winsor.fit(xs)
    xs = [w.apply(x) for x in xs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return out
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    res = [y - (a + b * x) for x, y in zip(xs, ys)]
    sst = sum((y - my) ** 2 for y in ys)
    r2 = 1 - sum(e * e for e in res) / sst if sst else 0
    if r2 < 0.1:
        return out
    for (code, _, _), e in zip(pts, res):
        out[code] = -e
    return out


# ── 점수 ──
def score_group(rows: list[dict]) -> None:
    """사업군 하나: 지표별 윈저라이즈(1/99%) → 백분위 → 묶음 → 종합. rows를 제자리 갱신."""
    resid = pbr_roe_residuals(rows)
    for r in rows:
        r["metrics"]["PBR-ROE잔차"] = resid.get(r["stock_code"])
    pct = {}
    for m in [m for ms in BUCKETS.values() for m in ms]:
        vals = {r["stock_code"]: r["metrics"].get(m) for r in rows}
        w = Winsor.fit([v for v in vals.values() if v is not None])
        pct[m] = percentile_ranks({k: w.apply(v) for k, v in vals.items()})
    for r in rows:
        c = r["stock_code"]
        r["pct"] = {m: (None if pct[m][c] is None else round(pct[m][c], 1)) for m in pct}
        b, why = {}, []
        for name, ms in BUCKETS.items():
            v = [r["pct"][m] for m in ms if r["pct"][m] is not None]
            need = {"value": 2, "quality": 2, "trend": 1, "safety": 1}[name]
            ok = len(v) >= need and (name != "value" or r["pct"]["E/P"] is not None or r["pct"]["B/P"] is not None)
            b[name] = round(sum(v) / len(v), 1) if ok else None
            if not ok:
                why.append(f"{ {'value': '가치', 'quality': '퀄리티', 'trend': '추세', 'safety': '안정성'}[name]} 지표 부족")
        r["buckets"] = b
        r["composite"] = None if why else round(sum(WEIGHTS[k] * b[k] for k in WEIGHTS), 1)
        if why:
            r["exclude"].extend(why)


def risk_flags(r: dict, debt_top10: float | None) -> None:
    t, s, m = r.get("ttm") or {}, r.get("stock") or {}, r["metrics"]
    eo, ni = s.get("equity_owners"), t.get("ni_owners")
    if eo is not None and eo <= 0:
        r["exclude"].append("자본잠식(지배주주 자본 ≤ 0)")
    if ni is not None and ni < 0:
        r["exclude"].append("적자(TTM 지배주주 순이익 < 0)")
    items = r.get("fscore_items") or {}
    if r.get("fscore") is not None and sum(v is not None for v in items.values()) >= 7 and r["fscore"] <= 3:
        r["exclude"].append(f"F-스코어 {r['fscore']:.1f} ≤ 3")
    op, nit, cfo = t.get("op_income"), t.get("ni_total"), t.get("cfo")
    if op and op > 0 and nit is not None and nit > op * 1.5:
        r["warn"].append("일회성 이익 의심(순이익 > 영업이익×1.5)")
    if nit is not None and nit > 0 and cfo is not None and cfo < 0:
        r["warn"].append("이익의 질(순이익 > 0, 영업현금흐름 < 0, TTM 기준)")
    dr = r.get("debt_ratio")
    if dr is not None and debt_top10 is not None and dr >= debt_top10:
        r["warn"].append("부채비율 사업군 상위 10%")
    if r.get("group_id") == "G30":
        r["warn"].append("지주회사(구조적 할인)")
    if (r.get("interest_basis") or "").startswith("금융원가"):
        r["warn"].append("이자보상배율이 금융원가 대용으로 계산됨")


def run() -> dict:
    uni = read_gz_json(L1 / "universe.json.gz")
    targets = [u for u in uni["rows"] if u.get("status") == "ok"]
    cal = TradingCalendar.from_l0()
    px = Prices()
    groups = load_groups()
    last_bd = px.days[-1]
    asof = dt.datetime.strptime(last_bd, "%Y%m%d").date()

    # 20일 평균 거래대금·최근 거래 여부
    recent = px.days[-20:]
    tv = defaultdict(list)
    for bd in recent:
        for r in px.day(bd):
            tv[r["ISU_CD"]].append(float(r.get("ACC_TRDVAL") or 0))
    halted = {c for c, v in tv.items() if len(v) >= 5 and all(x == 0 for x in v[-5:])}

    out, stat = [], Counter()
    for u in targets:
        row = {"corp_code": u["corp_code"], "stock_code": u["stock_code"], "name": u["name"], "category": ""}
        try:
            r = build_company(row, cal, px)
        except Exception as e:  # noqa: BLE001
            r = {**row, "status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}"}
        r.update({"market": u["market"], "sect": u["sect"], "list_dd": u["list_dd"], "exclude": [], "warn": []})
        stat[r.get("status")] += 1
        if r.get("status") != "ok":
            r["exclude"].append({"no_price": "시세 없음", "no_financials": "사용 가능한 재무 없음",
                                 "excluded_non_dec": "12월 외 결산"}.get(r.get("status"), r.get("error") or r.get("status")))
            r.setdefault("metrics", {})
        gid, gname = group_of(r.get("induty_code"), groups)
        r["group_id"], r["group_name"] = gid, gname
        # 유니버스 필터 (DESIGN 5.1)
        if gid is None:
            r["exclude"].append(f"사업군 미배정(KSIC {r.get('induty_code')})")
        elif gid == "EX":
            r["exclude"].append("금융업(D-06)")
        if (r.get("mcap") or 0) < MIN_MCAP:
            r["exclude"].append("시가총액 500억 미만")
        t20 = tv.get(u["stock_code"]) or []
        r["tv20"] = sum(t20) / len(t20) if t20 else None
        if (r["tv20"] or 0) < MIN_TV20:
            r["exclude"].append("20일 평균 거래대금 5억 미만")
        if u["stock_code"] in halted:
            r["exclude"].append("거래정지(최근 5거래일 거래 없음)")
        if "관리종목" in u["sect"]:
            r["exclude"].append("관리종목")
        if "투자주의환기" in u["sect"]:
            r["exclude"].append("투자주의환기종목")
        if u.get("list_dd") and (asof - dt.datetime.strptime(u["list_dd"], "%Y%m%d").date()).days < 365:
            r["exclude"].append("상장 1년 미만")
        s = r.get("stock") or {}
        r["debt_ratio"] = (s["liabilities"] / s["equity_total"]) if s.get("liabilities") is not None and s.get("equity_total") and s["equity_total"] > 0 else None
        out.append(r)

    # 사업군별 점수 — 유니버스 필터를 통과한 종목끼리만 비교
    by_g = defaultdict(list)
    for r in out:
        if not r["exclude"]:
            by_g[r["group_id"]].append(r)
    for gid, rows in by_g.items():
        drs = sorted(x["debt_ratio"] for x in rows if x["debt_ratio"] is not None)
        top10 = drs[int(len(drs) * 0.9)] if len(drs) >= 10 else None
        score_group(rows)
        for r in rows:
            r["group_n"] = len(rows)
            r["reliability"] = reliability(len(rows))
            risk_flags(r, top10)
    # 순위
    ranked = sorted([r for r in out if not r["exclude"] and r.get("composite") is not None], key=lambda r: -r["composite"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for gid in by_g:
        g = [r for r in ranked if r["group_id"] == gid]
        for i, r in enumerate(g, 1):
            r["group_rank"] = i

    write_gz_json(L2 / "screen.json.gz", out)
    # 실전 추적 (DESIGN 7.6): 날짜별 점수 기록 — 백테스트와 별개로 실제 사후 성과를 확인하는 근거. 시세 기준일별 1개 파일
    hist = [{"c": r["stock_code"], "n": r.get("name"), "g": r.get("group_id"), "sc": r.get("composite"), "rk": r.get("rank"),
             "grk": r.get("group_rank"), "b": r.get("buckets"), "x": r["exclude"], "px": r.get("close"), "mc": r.get("mcap"),
             "pd": r.get("period")} for r in out]
    write_gz_json(L2 / "screen_history" / f"{last_bd}.json.gz", {"asof": last_bd, "strategy": "v1", "rows": hist})
    exc = Counter(x for r in out for x in r["exclude"])
    res = {"asof_price": last_bd, "universe_collected": len(uni["rows"]), "universe_total": uni["total"],
           "collection_complete": uni.get("complete"), "evaluated": len(out), "build_status": dict(stat),
           "ranked": len(ranked), "groups": {g: len(v) for g, v in sorted(by_g.items())},
           "exclusions": dict(exc.most_common())}
    log(f"스크리너: 순위 {len(ranked)} / 평가 {len(out)}")
    from .screen_view import build_view
    build_view(out, res, REPORTS / "screen")
    return res
