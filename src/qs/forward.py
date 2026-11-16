"""실시간(전향) 기록 — D-32.

검증 구간(2022-04~)까지 다 쓴 뒤라, 이제 점수를 공정하게 판정할 방법은 '앞으로 쌓이는 기록'뿐이다.
- 스냅샷: 지난 스냅샷에서 84일(약 한 분기) 이상 지난 첫 실행에서, 그날 화면의 순위를 그대로 기록한다.
  v1 상위 20, 혼합(업종70·시장30) 상위 20, 순위 대상 전체를 v1 점수 5분위로 나눈 목록, 종목별 사업군·종가.
  파일은 reports/forward/snapshots/<시세기준일>.json, 목차 index.json에 SHA-256을 함께 적어 사후 수정 여부를 확인할 수 있게 한다.
- 평가: 매 실행마다 각 스냅샷에서 '다음 스냅샷(없으면 오늘)'까지의 수정주가 수익률을 계산해 reports/forward/status.json에 쓴다.
  비교: 순위 대상 전체 동일비중, 같은 사업군 평균. 비용은 빼지 않는다(백테스트 기본 비용은 분기 약 0.4%).
- 판정 규칙은 docs/DECISIONS.md D-32 (기록 시작 전 고정).
한계: 상장폐지·거래정지 종목은 마지막 거래 가격으로 고정(그 뒤 현금 보유와 같음). 배당 미포함.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict

from .storage import L2
from .util import REPORTS, log, read_gz_json

SNAP_EVERY_DAYS = 84
TOP_N = 20
MIN_RANKED = 300          # 순위 대상이 이보다 적은 날(데이터 이상)은 스냅샷을 만들지 않음
HYBRID_W = 0.7
DIR = REPORTS / "forward"


def _d(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y%m%d").date()


def _index() -> list[dict]:
    p = DIR / "index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _v2_part(ranked: list[dict]) -> dict | None:
    """v2(D-37): v1 순서에서 '진짜인가' 거름 해당 종목을 뺀 순위. 상위 20과 v2 순위 대상 5분위."""
    v2 = sorted([r for r in ranked if r.get("rank_v2")], key=lambda r: r["rank_v2"])
    if not v2:
        return None
    n = len(v2)
    return {"top": [r["stock_code"] for r in v2[:TOP_N]],
            "quintiles": {str(q + 1): [r["stock_code"] for r in v2[q * n // 5:(q + 1) * n // 5]] for q in range(5)},
            "ranked_v2": n, "gated": {r["stock_code"]: r.get("truth") or [] for r in ranked if r.get("truth")}}


def make_snapshot(out: list[dict], asof: str) -> dict:
    ranked = [r for r in out if r.get("rank") and r.get("close")]
    v1 = sorted(ranked, key=lambda r: r["rank"])
    hy = sorted([r for r in ranked if r.get("composite") is not None and r.get("mkt_composite") is not None],
                key=lambda r: (-(HYBRID_W * r["composite"] + (1 - HYBRID_W) * r["mkt_composite"]), r["stock_code"]))
    n = len(v1)
    quint = {str(q + 1): [r["stock_code"] for r in v1[q * n // 5:(q + 1) * n // 5]] for q in range(5)}
    snap = {"asof": asof, "strategy": "v1 (frozen, D-30)", "ranked": n,
            "top": {"v1": [r["stock_code"] for r in v1[:TOP_N]], "hybrid": [r["stock_code"] for r in hy[:TOP_N]]},
            "quintiles_v1": quint,
            "rows": {r["stock_code"]: {"g": r.get("group_id"), "px": r["close"], "sc": r.get("composite"),
                                       "n": r.get("name")} for r in ranked}}
    v2 = _v2_part(ranked)
    if v2:
        snap["top"]["v2"] = v2["top"]
        snap["quintiles_v2"] = v2["quintiles"]
        snap["v2_meta"] = {"rule": "config/strategy_v2.yaml (D-37)", "ranked_v2": v2["ranked_v2"], "gated": v2["gated"]}
    return snap


def maybe_v2_supplement(out: list[dict], asof: str) -> dict | None:
    """D-37: 이미 있는 마지막 v1 스냅샷에 v2가 없고, 이번 화면의 시세 기준일이 그 스냅샷과 같으면
    같은 날 v2 목록을 보충 파일로 붙인다(v1·v2가 같은 시작점에서 비교되도록). 기존 스냅샷 파일은 바꾸지 않는다."""
    idx = _index()
    if not idx or idx[-1]["asof"] != asof or idx[-1].get("v2_file"):
        return None
    base = json.loads((DIR / idx[-1]["file"]).read_text(encoding="utf-8"))
    if "v2" in base.get("top", {}):
        return None
    ranked = [r for r in out if r.get("rank") and r.get("close")]
    v2 = _v2_part(ranked)
    if not v2:
        return None
    if {r["stock_code"] for r in ranked} != set(base["rows"]):
        log("!! 실시간 기록: 순위 대상이 v1 스냅샷과 달라 v2 보충 생략")
        return None
    sup = {"asof": asof, "strategy": "v2 (D-37)", "supplement_of": idx[-1]["file"], "top": {"v2": v2["top"]},
           "quintiles_v2": v2["quintiles"], "v2_meta": {"rule": "config/strategy_v2.yaml (D-37)", "ranked_v2": v2["ranked_v2"],
                                                       "gated": v2["gated"]}}
    body = json.dumps(sup, ensure_ascii=False, sort_keys=True, indent=0)
    p = DIR / "snapshots" / f"{asof}_v2.json"
    if p.exists():
        return None
    p.write_text(body, encoding="utf-8")
    idx[-1]["v2_file"] = f"snapshots/{asof}_v2.json"
    idx[-1]["v2_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    (DIR / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"실시간 기록: v2 보충 {asof} (v2 순위 대상 {v2['ranked_v2']}개)")
    return {"asof": asof, "ranked_v2": v2["ranked_v2"]}


def maybe_snapshot(out: list[dict], asof: str) -> dict | None:
    idx = _index()
    if idx and (_d(asof) - _d(idx[-1]["asof"])).days < SNAP_EVERY_DAYS:
        return None
    if sum(1 for r in out if r.get("rank")) < MIN_RANKED:
        log("!! 실시간 기록: 순위 대상이 너무 적어 스냅샷 건너뜀")
        return None
    snap = make_snapshot(out, asof)
    body = json.dumps(snap, ensure_ascii=False, sort_keys=True, indent=0)
    (DIR / "snapshots").mkdir(parents=True, exist_ok=True)
    p = DIR / "snapshots" / f"{asof}.json"
    if p.exists():
        return None                                   # 같은 기준일은 한 번만 (덮어쓰지 않음)
    p.write_text(body, encoding="utf-8")
    idx.append({"asof": asof, "file": f"snapshots/{asof}.json", "sha256": hashlib.sha256(body.encode()).hexdigest(),
                "ranked": snap["ranked"]})
    (DIR / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"실시간 기록: 스냅샷 {asof} (순위 대상 {snap['ranked']}개)")
    return {"asof": asof, "ranked": snap["ranked"]}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def period_stats(snap: dict, ret: dict[str, float | None]) -> dict:
    """한 스냅샷 기간의 성적. ret: 종목코드 → 기간 수익률(없으면 None)."""
    rows = snap["rows"]
    uni = _mean([ret.get(c) for c in rows])
    by_g = defaultdict(list)
    for c, r in rows.items():
        if ret.get(c) is not None:
            by_g[r["g"]].append(ret[c])
    g_mean = {g: sum(v) / len(v) for g, v in by_g.items()}
    rel = {c: (ret[c] - g_mean[rows[c]["g"]]) if ret.get(c) is not None and rows[c]["g"] in g_mean else None for c in rows}
    out = {"universe_ew": uni, "priced": sum(1 for c in rows if ret.get(c) is not None), "ranked": len(rows)}
    for k, codes in snap["top"].items():
        m = _mean([ret.get(c) for c in codes])
        out[f"top20_{k}"] = m
        out[f"top20_{k}_excess"] = None if m is None or uni is None else m - uni
    out["quintile_vs_sector"] = {q: _mean([rel.get(c) for c in codes]) for q, codes in snap["quintiles_v1"].items()}
    out["quintile_ret"] = {q: _mean([ret.get(c) for c in codes]) for q, codes in snap["quintiles_v1"].items()}
    if snap.get("quintiles_v2"):
        out["quintile_v2_vs_sector"] = {q: _mean([rel.get(c) for c in codes]) for q, codes in snap["quintiles_v2"].items()}
    return out


def evaluate() -> dict:
    idx = _index()
    if not idx:
        return {"snapshots": 0}
    from .adjust import adjusted, detect_events, load_series
    first = _d(idx[0]["asof"])
    series = load_series(days=(dt.date.today() - first).days + 30)
    ev = defaultdict(list)
    for e in detect_events(series):
        ev[e["code"]].append(e)
    adj = {c: adjusted(s, ev.get(c, [])) for c, s in series.items()}
    last_day = max((a[-1][0] for a in adj.values() if a), default=None)

    def px_on(code: str, d: str) -> float | None:
        a = adj.get(code)
        if not a:
            return None
        v = None
        for day, p in a:                  # 그 날 이전 마지막 수정종가
            if day > d:
                break
            v = p
        return v

    periods = []
    for i, s in enumerate(idx):
        snap = json.loads((DIR / s["file"]).read_text(encoding="utf-8"))
        if s.get("v2_file"):                                  # D-37 보충 (같은 날 v2 목록)
            sup = json.loads((DIR / s["v2_file"]).read_text(encoding="utf-8"))
            snap["top"] = {**snap["top"], **sup["top"]}
            snap["quintiles_v2"] = sup.get("quintiles_v2")
        end = idx[i + 1]["asof"] if i + 1 < len(idx) else last_day
        ret = {}
        for c in snap["rows"]:
            b, e = px_on(c, s["asof"]), px_on(c, end)
            ret[c] = (e / b - 1) if b and e else None
        periods.append({"from": s["asof"], "to": end, "complete": i + 1 < len(idx), **period_stats(snap, ret)})
    res = {"snapshots": len(idx), "periods": periods, "rule": "docs/DECISIONS.md D-32",
           "updated": last_day, "note": "비용·배당 미반영. 판정은 스냅샷 4회·1년 이상 쌓인 뒤"}
    (DIR / "status.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"snapshots": len(idx), "last": periods[-1] if periods else None}


def run() -> dict:
    try:
        out = read_gz_json(L2 / "screen.json.gz")
    except FileNotFoundError:
        return {"skipped": "screen 결과 없음"}
    hist = sorted((L2 / "screen_history").glob("*.json.gz"))
    if not hist:
        return {"skipped": "시세 기준일 기록 없음"}
    asof = hist[-1].name[:8]                       # 이번 실행의 screen이 쓴 시세 기준일
    snap = maybe_snapshot(out, asof)
    sup = maybe_v2_supplement(out, asof)
    ev = evaluate()
    res = {"snapshot": snap, "v2_supplement": sup, **ev}
    log(f"실시간 기록: {json.dumps({'snapshot': snap, 'snapshots': ev.get('snapshots')}, ensure_ascii=False)}")
    return res
