"""현재 시점 스크리너 (방침 B): 전 종목 업종 대비 점수 — DESIGN 5장 / config/strategy_v1.yaml.

흐름: 전 종목 L1(재무)·KRX(시세) → 회사별 지표(pipeline.build_company 재사용)
     → 유니버스 필터 → 사업군 배정(config/peer_groups_v1.csv) → 사업군 내 윈저라이즈·백분위
     → 묶음 점수(최소 충족 조건) → 종합 점수 → 위험 필터(제외/경고) → 순위
결측은 50점으로 채우지 않는다. 점수 공식은 '가설'이며 과거 성과로 검증되지 않았다 (화면에 표시).
"""
from __future__ import annotations

import json

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


def load_overrides() -> dict[str, dict]:
    """개별 종목 사업군 보정 (DESIGN 5.2: config/peer_overrides.csv — 적용일·사유 기록)."""
    p = CONFIG / "peer_overrides.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return {r["stock_code"]: r for r in csv.DictReader(f)}


def group_names() -> dict[str, str]:
    with (CONFIG / "peer_groups_v1.csv").open(encoding="utf-8") as f:
        return {r["group_id"]: r["group_name"] for r in csv.DictReader(f)}


def group_of(ksic: str | None, groups) -> tuple[str | None, str | None]:
    k = (ksic or "").strip()
    for p, gid, gname in groups:
        if k.startswith(p):
            return gid, gname
    return None, None


# 화면 검토 1차(ui1) Q7a: 주 제외 사유 = 아래 순서에서 처음 해당하는 사유 (데이터·상장 상태 → 유동성 → 비교 대상 → 재무 위험 → 지표 부족)
EXCLUDE_ORDER = [("시세 없음", "시세 없음"), ("사용 가능한 재무 없음", "재무 없음"), ("12월 외 결산", "12월 외 결산"),
                 ("사업군 미배정", "업종 미배정"), ("금융업", "금융업"), ("관리종목", "관리종목"), ("투자주의환기", "투자주의환기"),
                 ("거래정지", "거래정지"), ("상장 1년 미만", "상장 1년 미만"), ("시가총액", "소형주(500억 미만)"),
                 ("20일 평균 거래대금", "거래대금 미달"), ("비교 대상 부족", "비교 대상 부족"), ("자본잠식", "자본잠식"),
                 ("감사·검토의견", "감사의견 비적정"), ("적자", "적자"), ("F-스코어", "F-스코어 낮음"), ("지표 부족", "지표 부족")]


def primary_exclusion(reasons: list[str]) -> tuple[str, str] | None:
    """(짧은 이름, 원래 사유). 사유가 없으면 None. 목록에 없는 사유는 맨 뒤 순위로 그대로."""
    if not reasons:
        return None
    best = None
    for r in reasons:
        rank = next((k for k, (key, _) in enumerate(EXCLUDE_ORDER) if key in r), len(EXCLUDE_ORDER))
        short = EXCLUDE_ORDER[rank][1] if rank < len(EXCLUDE_ORDER) else r
        if best is None or rank < best[0]:
            best = (rank, short, r)
    return best[1], best[2]


# ── 배당 (배당 탭) ──
def dividend_info(corp_code: str | None, close: float | None) -> dict | None:
    """가장 최근 사업보고서의 배당 항목 → 3개년 주당배당금, 현재가 기준 배당수익률, 배당성향, 배당 흐름 표시.
    원천: DART alotMatter (L0 xcheck/alotMatter/<corp>/<연도>_11011)."""
    from .collect.dart import parse_dividend_hist
    from .storage import dart_path, exists
    from .util import to_num
    if not corp_code:
        return None
    close = to_num(close) if close is not None else None   # KRX 종가는 문자열
    for y in range(dt.date.today().year, 2022, -1):
        p = dart_path("xcheck", "alotMatter", corp_code, f"{y}_11011.json.gz")
        if not exists(p):
            continue
        h = parse_dividend_hist(load_l0(p)["payload"])
        d0, d1, d2 = h["dps"]
        if d0 is None and d1 is None:
            continue
        paid = [x for x in (d0, d1, d2) if x is not None]
        streak = sum(1 for x in (d0, d1, d2) if x and x > 0) if all(x is not None for x in (d0, d1, d2)) else None
        trend = None
        if d0 is not None and d1 is not None:
            trend = "증가" if d0 > d1 * 1.001 else ("감소" if d0 < d1 * 0.999 else "유지")
            if d0 == 0 and d1 > 0:
                trend = "중단"
        warn = []
        if h["payout_pct"] is not None and h["payout_pct"] > 100:
            warn.append(f"배당성향 {h['payout_pct']:.0f}% — 이익보다 많이 배당")
        if trend in ("감소", "중단"):
            warn.append(f"배당 {trend}")
        return {"year": y, "dps": d0, "dps1": d1, "dps2": d2, "payout": h["payout_pct"],
                "yield_now": (d0 / close * 100) if d0 and close else (0.0 if d0 == 0 else None),
                "yield_report": h["yield_pct"], "streak3": streak, "trend": trend, "warn": warn, "n": len(paid)}
    return None


# ── 금융업 (금융업 탭) — 본 순위에서 빠진 은행·보험·증권 등을 따로 비교 (점수는 가설, 성과 미검증) ──
def fin_subgroup(ksic: str | None, name: str | None) -> str:
    k, n = ksic or "", name or ""
    if "지주" in n or k.startswith("64992"):
        return "금융지주"
    if k.startswith("641"):
        return "은행"
    if k.startswith("65"):
        return "보험"
    if k.startswith("6612") or "증권" in n:
        return "증권"
    if k.startswith("649") or any(x in n for x in ("카드", "캐피탈", "파이낸셜", "저축")):
        return "카드·캐피탈·기타여신"
    return "기타 금융"


def financial_scores(out: list[dict]) -> int:
    """금융업 종목 중 다른 제외 사유가 없는 종목끼리: 장부가수익률(B/P)·ROE·현재 배당수익률 백분위 평균 = 금융 점수."""
    rows = [r for r in out if r.get("group_id") == "EX"]
    for r in rows:
        r["fin_group"] = fin_subgroup(r.get("induty_code"), r.get("name"))
        r["fin_other_ex"] = [x for x in r["exclude"] if not x.startswith("금융업")]
    elig = [r for r in rows if not r["fin_other_ex"] and r.get("status") == "ok"]
    if len(elig) < 5:
        return 0
    keys = {"B/P": lambda r: (r.get("metrics") or {}).get("B/P"), "ROE": lambda r: (r.get("metrics") or {}).get("ROE"),
            "배당": lambda r: (r.get("dividend") or {}).get("yield_now")}
    pct = {}
    for k, f in keys.items():
        vals = {r["stock_code"]: f(r) for r in elig}
        w = Winsor.fit([v for v in vals.values() if v is not None])
        pct[k] = percentile_ranks({c: (w.apply(v) if v is not None else None) for c, v in vals.items()})
    for r in elig:
        ps = [pct[k][r["stock_code"]] for k in keys if pct[k][r["stock_code"]] is not None]
        r["fin_pct"] = {k: (None if pct[k][r["stock_code"]] is None else round(pct[k][r["stock_code"]], 1)) for k in keys}
        r["fin_score"] = round(sum(ps) / len(ps), 1) if len(ps) >= 2 else None
    ranked = sorted([r for r in elig if r.get("fin_score") is not None], key=lambda r: (-r["fin_score"], r["stock_code"]))
    for i, r in enumerate(ranked, 1):
        r["fin_rank"] = i
    return len(ranked)


MIN_GROUP_N = 10   # D-26: 비교 대상이 이보다 적은 사업군은 백분위가 무의미 → 순위에서 빼고 사유 표시


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
def score_group(rows: list[dict], weights: dict | None = None) -> None:
    """사업군 하나: 지표별 윈저라이즈(1/99%) → 백분위 → 묶음 → 종합. rows를 제자리 갱신.
    weights: 묶음 가중치 (기본 v1). 가중치 0인 묶음은 최소 조건을 요구하지 않는다 (비교 실험용)."""
    weights = weights or WEIGHTS
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
            if not ok and weights.get(name, 0) > 0:
                why.append(f"{ {'value': '가치', 'quality': '퀄리티', 'trend': '추세', 'safety': '안정성'}[name]} 지표 부족")
        r["buckets"] = b
        wsum = sum(w for w in weights.values() if w > 0)
        r["composite"] = None if why else round(sum(w * b[k] for k, w in weights.items() if w > 0) / wsum, 1)
        if why:
            r["exclude"].extend(why)


def risk_flags(r: dict, debt_top10: float | None, exclude_loss: bool = True, exclude_fscore: bool = True) -> None:
    t, s, m = r.get("ttm") or {}, r.get("stock") or {}, r["metrics"]
    eo, ni = s.get("equity_owners"), t.get("ni_owners")
    if eo is not None and eo <= 0:
        r["exclude"].append("자본잠식(지배주주 자본 ≤ 0)")
    if ni is not None and ni < 0:
        (r["exclude"] if exclude_loss else r["warn"]).append("적자(TTM 지배주주 순이익 < 0)")
    a = r.get("audit") or {}
    if a.get("clean") is False:
        r["exclude"].append(f"감사·검토의견 비적정({a.get('opinion')}, {a.get('period')})")
    items = r.get("fscore_items") or {}
    if r.get("fscore") is not None and sum(v is not None for v in items.values()) >= 7 and r["fscore"] <= 3:
        (r["exclude"] if exclude_fscore else r["warn"]).append(f"F-스코어 {r['fscore']:.1f} ≤ 3")
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


def filter_and_score(evals: list[dict], meta: dict[str, dict], asof: dt.date, tv: dict[str, list[float]],
                     halted: set[str], groups, opts: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """평가 결과 → 유니버스 필터·사업군 점수·위험 필터·순위. 현재 화면과 백테스트가 같은 규칙을 쓰도록 분리.
    meta[code] = {market, sect, list_dd}. tv[code] = 최근 20거래일 거래대금 목록.
    opts (비교 실험용, 기본 = 전략 v1): weights, exclude_loss, exclude_fscore,
          grouping = table(사업군 표) | ksic2(KSIC 앞 2자리) | market(전체 시장)"""
    opts = opts or {}
    grouping = opts.get("grouping", "table")
    overrides, gnames = load_overrides(), group_names()
    out = []
    for r in evals:
        u = meta.get(r["stock_code"], {})
        r.update({"market": u.get("market"), "sect": u.get("sect") or "", "list_dd": u.get("list_dd"), "exclude": [], "warn": []})
        if r.get("status") != "ok":
            r["exclude"].append({"no_price": "시세 없음", "no_financials": "사용 가능한 재무 없음",
                                 "excluded_non_dec": "12월 외 결산"}.get(r.get("status"), r.get("error") or r.get("status")))
            r.setdefault("metrics", {})
        gid, gname = group_of(r.get("induty_code"), groups)
        ov = overrides.get(r["stock_code"])
        if ov:
            gid, gname = ov["to_group"], gnames.get(ov["to_group"], ov["to_group"])
            r["group_override"] = ov["reason"]
        elif gid == "EX" and (r.get("induty_code") or "").startswith("649") and any(k in (r.get("name") or "") for k in ("홀딩스", "지주")) \
                and "금융" not in (r.get("name") or ""):
            # KSIC 기타 금융업(649)으로 등록된 사업 지주회사 → 지주회사 사업군 (금융업으로 잘못 제외되지 않도록)
            gid, gname = "G30", gnames.get("G30")
            r["group_override"] = "사업 지주회사(이름 규칙, KSIC 649 등록)"
        elif gid == "G30" and "금융" in (r.get("name") or ""):
            # 이름에 '금융'이 들어간 지주회사(64992)는 금융지주로 보고 제외 — 보정표에 없는 소형 금융지주용 규칙
            gid, gname = "EX", gnames.get("EX")
            r["group_override"] = "금융지주회사(이름 규칙, D-06)"
        r["group_id"], r["group_name"] = gid, gname
        # 유니버스 필터 (DESIGN 5.1)
        if gid is None:
            r["exclude"].append(f"사업군 미배정(KSIC {r.get('induty_code')})")
        elif gid == "EX":
            r["exclude"].append("금융업(D-06)")
        if (r.get("mcap") or 0) < MIN_MCAP:
            r["exclude"].append("시가총액 500억 미만")
        t20 = tv.get(r["stock_code"]) or []
        r["tv20"] = sum(t20) / len(t20) if t20 else None
        if (r["tv20"] or 0) < MIN_TV20:
            r["exclude"].append("20일 평균 거래대금 5억 미만")
        if r["stock_code"] in halted:
            r["exclude"].append("거래정지(최근 5거래일 거래 없음)")
        if "관리종목" in r["sect"]:
            r["exclude"].append("관리종목")
        if "투자주의환기" in r["sect"]:
            r["exclude"].append("투자주의환기종목")
        if r.get("list_dd") and (asof - dt.datetime.strptime(r["list_dd"], "%Y%m%d").date()).days < 365:
            r["exclude"].append("상장 1년 미만")
        s = r.get("stock") or {}
        r["debt_ratio"] = (s["liabilities"] / s["equity_total"]) if s.get("liabilities") is not None and s.get("equity_total") and s["equity_total"] > 0 else None
        out.append(r)

    # 사업군별 점수 — 유니버스 필터를 통과한 종목끼리만 비교
    by_g = defaultdict(list)
    for r in out:
        if not r["exclude"]:
            key = {"table": r["group_id"], "ksic2": "K" + (r.get("induty_code") or "")[:2], "market": "ALL"}[grouping]
            r["score_group"] = key
            by_g[key].append(r)
    for gid, rows in by_g.items():
        drs = sorted(x["debt_ratio"] for x in rows if x["debt_ratio"] is not None)
        top10 = drs[int(len(drs) * 0.9)] if len(drs) >= 10 else None
        score_group(rows, opts.get("weights"))
        for r in rows:
            r["group_n"] = len(rows)
            r["reliability"] = reliability(len(rows))
            if len(rows) < MIN_GROUP_N:
                r["exclude"].append(f"비교 대상 부족(사업군 {len(rows)}개 < {MIN_GROUP_N}, D-26)")
            risk_flags(r, top10, opts.get("exclude_loss", True), opts.get("exclude_fscore", True))
    # 순위
    ranked = sorted([r for r in out if not r["exclude"] and r.get("composite") is not None],
                    key=lambda r: (-r["composite"], r["stock_code"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for gid in by_g:
        g = [r for r in ranked if r.get("score_group") == gid]
        for i, r in enumerate(g, 1):
            r["group_rank"] = i
    return out, ranked, by_g


def run() -> dict:
    uni = read_gz_json(L1 / "universe.json.gz")
    targets = [u for u in uni["rows"] if u.get("status") == "ok"]
    cal = TradingCalendar.from_l0()
    px = Prices()
    groups = load_groups()
    last_bd = px.days[-1]
    asof = dt.datetime.strptime(last_bd, "%Y%m%d").date()

    # 20일 평균 거래대금·최근 거래 여부
    tv = defaultdict(list)
    for bd in px.days[-20:]:
        for r in px.day(bd):
            tv[r["ISU_CD"]].append(float(r.get("ACC_TRDVAL") or 0))
    halted = {c for c, v in tv.items() if len(v) >= 5 and all(x == 0 for x in v[-5:])}

    evals, stat = [], Counter()
    for u in targets:
        row = {"corp_code": u["corp_code"], "stock_code": u["stock_code"], "name": u["name"], "category": ""}
        try:
            r = build_company(row, cal, px)
        except Exception as e:  # noqa: BLE001
            r = {**row, "status": "error", "error": f"{type(e).__name__}: {str(e)[:150]}"}
        stat[r.get("status")] += 1
        evals.append(r)
    meta = {u["stock_code"]: dict(u) for u in targets}
    # 코스피 관리종목: KIND 현재 목록(하루 1회 스냅샷)으로 표시 — 코스닥은 KRX 소속부에 이미 있음 (4차 Q1)
    from .collect.kind import latest as kind_latest
    kind_date, kind_admin = kind_latest()
    for c, u in meta.items():
        if c in kind_admin and "관리종목" not in (u.get("sect") or ""):
            u["sect"] = (u.get("sect") or "") + " 관리종목(KIND)"
    out, ranked, by_g = filter_and_score(evals, meta, asof, tv, halted, groups)
    for r in out:
        r["dividend"] = dividend_info(r.get("corp_code"), r.get("close")) if r.get("status") == "ok" else None
    fin_n = financial_scores(out)

    write_gz_json(L2 / "screen.json.gz", out)
    # 실전 추적 (DESIGN 7.6): 날짜별 점수 기록 — 백테스트와 별개로 실제 사후 성과를 확인하는 근거. 시세 기준일별 1개 파일
    hist = [{"c": r["stock_code"], "n": r.get("name"), "g": r.get("group_id"), "sc": r.get("composite"), "rk": r.get("rank"),
             "grk": r.get("group_rank"), "b": r.get("buckets"), "x": r["exclude"], "px": r.get("close"), "mc": r.get("mcap"),
             "pd": r.get("period")} for r in out]
    # 전날(직전 시세 기준일) 순위 — 화면의 순위 변화 표시용
    prev_files = sorted(f for f in (L2 / "screen_history").glob("*.json.gz") if f.name.split(".")[0] < last_bd) \
        if (L2 / "screen_history").exists() else []
    prev_asof, prev_rank = None, {}
    if prev_files:
        ph = read_gz_json(prev_files[-1])
        prev_asof = ph.get("asof")
        prev_rank = {x["c"]: x.get("rk") for x in ph.get("rows", [])}
    for r in out:
        r["prev_rank"] = prev_rank.get(r["stock_code"]) if prev_asof else None
        r["prev_known"] = r["stock_code"] in prev_rank
    write_gz_json(L2 / "screen_history" / f"{last_bd}.json.gz", {"asof": last_bd, "strategy": "v1", "rows": hist})
    exc = Counter(x for r in out for x in r["exclude"])
    res = {"asof_price": last_bd, "universe_collected": len(uni["rows"]), "universe_total": uni["total"],
           "collection_complete": uni.get("complete"), "evaluated": len(out), "build_status": dict(stat),
           "ranked": len(ranked), "groups": {g: len(v) for g, v in sorted(by_g.items())},
           "exclusions": dict(exc.most_common()), "prev_asof": prev_asof, "fin_ranked": fin_n,
           "div_payers": sum(1 for r in out if (r.get("dividend") or {}).get("dps"))}
    # 데이터 상태 (DESIGN 8: 기준일·갱신 성공·품질 점검)
    from .collect.krx import suspicious_empty, weekdays
    from .util import now_kst
    warn = []
    recent_wd = [d.strftime("%Y%m%d") for d in weekdays(asof - dt.timedelta(days=14), asof + dt.timedelta(days=7))]
    bad = [d for d in recent_wd if suspicious_empty(d)]
    if bad:
        warn.append(f"KRX 코스피 시세가 비어 있는 날 {', '.join(bad)} — 매일 다시 받는 중, 그동안 시세 기준일은 {last_bd}")
    if not uni.get("complete"):
        warn.append(f"재무 수집 진행 중 {len(uni['rows']):,}/{uni['total']:,}개사 — 나머지는 다음 자동 실행에서")
    try:
        hp = read_gz_json(L1 / "history_progress.json.gz")
        hist = {"processed": hp.get("processed"), "targets": hp.get("targets"), "complete": hp.get("complete")}
    except FileNotFoundError:
        hist = None
    res["status"] = {"built_at": now_kst().isoformat(timespec="minutes"), "warnings": warn, "history": hist,
                     "kind_admin": {"date": kind_date, "count": len(kind_admin)} if kind_date else None}
    log(f"스크리너: 순위 {len(ranked)} / 평가 {len(out)}")
    # 게시 보호 (2026-10-31): 순위 대상이 직전 게시본의 70% 미만으로 줄면 화면을 바꾸지 않는다
    # (데이터 복구 중·DART 한도 소진 실행에서 빈 화면이 게시되는 것을 막음 — 마지막 정상 화면 유지)
    prev_path = REPORTS / "screen" / "screen.json"
    try:
        prev_ranked = json.loads(prev_path.read_text(encoding="utf-8")).get("ranked") or 0
    except (FileNotFoundError, ValueError):
        prev_ranked = 0
    if prev_ranked and res["ranked"] < prev_ranked * 0.7:
        res["published"] = False
        res["status"]["warnings"].append(f"순위 대상 {res['ranked']}개 < 직전 게시 {prev_ranked}개의 70% — 화면 갱신 보류(마지막 정상 화면 유지)")
        log("!! " + res["status"]["warnings"][-1])
        return res
    from .screen_view import build_view
    build_view(out, res, REPORTS / "screen")
    res["published"] = True
    return res
