"""수정주가 (D-16).

원리 — KRX 공식 기준가
  KRX 일별시세의 '전일대비(CMPPREVDD_PRC)'는 그날의 '기준가' 대비 변동이다.
  액면분할·병합·권리락·인적분할 등이 있으면 거래소가 기준가를 조정하므로
      기준가_t = 종가_t − 전일대비_t
      조정계수_t = 기준가_t ÷ 종가_{t−1}
  가 거래소가 적용한 조정 비율이 된다. 기업행위가 없는 날은 정확히 1.
  → 과거 종가에 이후 모든 조정계수를 곱하면 수정종가 (현금배당은 기준가 조정 대상이 아니므로 미반영 = 배당 미포함, 설계와 일치).

검증 (D-16: 조정계수·효력일 기록, 연속성 테스트, 공시 확인)
  1) 검산: 수정종가 일간 수익률 = KRX 공식 등락률(FLUC_RT) (조정계수를 같은 KRX 필드에서 만들었으므로
     독립 검증이 아니라 계산·필드 정합성 확인일 뿐이다)
  1') 독립 교차검증: 비공식 수정주가(FinanceDataReader)와 수익률 비교 — 표본 종목
  2) 분류: 상장주식수 변화와 조정계수가 역수 관계면 분할·병합으로 확인
  3) 공시: 그 외 사건(권리락·인적분할 등)은 DART 공시 제목으로 확인
  미확인 사건이 기준일 이전 13개월 안에 있으면 그 종목의 모멘텀은 무효.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
from collections import defaultdict

from .storage import L0, L1, load_l0
from .util import REPORTS, log, to_num, write_gz_json

MINOR = 1e-3        # 기준가 끝전 조정 수준
INVERSE_TOL = 0.01  # 주식수 비율 × 조정계수 ≈ 1 허용오차
DART_PAT = re.compile(r"주식분할|주식병합|무상증자|유상증자|감자|분할|합병|권리락|주식배당|액면|주식교환|주식이전")


def load_series() -> dict[str, list[dict]]:
    """종목코드 → 날짜순 [{d, close, cmp, fluc, shares, name, mkt}]."""
    out: dict[str, list[dict]] = defaultdict(list)
    for api in ("sto_stk_bydd_trd", "sto_ksq_bydd_trd"):
        for p in sorted((L0 / "krx" / api).glob("*.json.gz")):
            rec = load_l0(p)
            if not rec["meta"].get("rows"):
                continue
            d = p.name[:8]
            for r in rec["payload"]:
                c = to_num(r.get("TDD_CLSPRC"))
                if not c or c <= 0:
                    continue
                out[r["ISU_CD"]].append({"d": d, "close": c, "cmp": to_num(r.get("CMPPREVDD_PRC")),
                                         "fluc": to_num(r.get("FLUC_RT")), "shares": to_num(r.get("LIST_SHRS")),
                                         "name": r.get("ISU_NM"), "mkt": r.get("MKT_NM")})
    for v in out.values():
        v.sort(key=lambda x: x["d"])
    return dict(out)


def classify(f: float, share_ratio: float | None) -> str:
    if abs(f - 1) < MINOR:
        return "미세 기준가 조정"
    if share_ratio and abs(share_ratio * f - 1) < INVERSE_TOL and share_ratio != 1:
        return "주식분할" if f < 1 else "주식병합"
    if share_ratio and abs(share_ratio - 1) < 1e-9:
        return "권리락(증자·주식배당 등)" if f < 1 else "기준가 상향(감자 등)"
    return "인적분할·감자 등 복합"


def detect_events(series: dict[str, list[dict]]) -> list[dict]:
    ev = []
    for code, s in series.items():
        for a, b in zip(s, s[1:]):
            if b["cmp"] is None:
                continue
            base = b["close"] - b["cmp"]
            if base <= 0:
                continue
            f = base / a["close"]
            if abs(f - 1) > 1e-9:
                sr = (b["shares"] / a["shares"]) if a["shares"] and b["shares"] else None
                ev.append({"code": code, "name": b["name"], "mkt": b["mkt"], "date": b["d"], "prev_date": a["d"],
                           "prev_close": a["close"], "base": base, "factor": f, "share_ratio": sr,
                           "class": classify(f, sr)})
    ev.sort(key=lambda e: (e["code"], e["date"]))
    return ev


def adjusted(series_one: list[dict], events_one: list[dict]) -> list[tuple[str, float]]:
    """수정종가 = 종가 × (그 날 이후 모든 조정계수의 곱)."""
    fac = {e["date"]: e["factor"] for e in events_one}
    out, cum = [], 1.0
    for x in reversed(series_one):
        out.append((x["d"], x["close"] * cum))
        cum *= fac.get(x["d"], 1.0)
    out.reverse()
    return out


def continuity(series_one: list[dict], adj: list[tuple[str, float]]) -> float:
    """수정종가 일간 수익률과 KRX 공식 등락률의 최대 차이(%p)."""
    worst = 0.0
    for (x0, (d0, a0)), (x1, (d1, a1)) in zip(zip(series_one, adj), zip(series_one[1:], adj[1:])):
        if x1["fluc"] is None or x1["cmp"] is None:
            continue
        r = (a1 / a0 - 1) * 100
        # 공식 등락률은 소수 둘째 자리 반올림 → 허용
        worst = max(worst, abs(r - x1["fluc"]))
    return worst


def confirm_with_dart(events: list[dict], dart, corp_by_stock: dict[str, str]) -> None:
    """확인이 필요한 사건(분할·병합·미세 외)을 DART 공시 제목으로 확인. 사건 전 120일 ~ 당일."""
    for e in events:
        if e["class"] in ("미세 기준가 조정", "주식분할", "주식병합"):
            e["confirmed_by"] = "주식수 역수 관계" if e["class"] in ("주식분할", "주식병합") else "미세(적용만)"
            continue
        cc = corp_by_stock.get(e["code"][:5] + "0") or corp_by_stock.get(e["code"])
        if not cc:
            e["confirmed_by"] = None
            e["dart_note"] = "고유번호 없음"
            continue
        d = dt.datetime.strptime(e["date"], "%Y%m%d").date()
        js = dart._json("list.json", {"corp_code": cc, "bgn_de": f"{d - dt.timedelta(days=120):%Y%m%d}",
                                      "end_de": f"{d:%Y%m%d}", "page_count": 100})
        hits = [x for x in (js.get("list") or []) if DART_PAT.search(x.get("report_nm", ""))]
        if hits:
            e["confirmed_by"] = "DART 공시"
            e["dart_note"] = " | ".join(f"{h['rcept_dt']} {h['report_nm'].strip()}" for h in hits[:3])
        else:
            e["confirmed_by"] = None
            e["dart_note"] = f"관련 공시 없음 (status {js.get('status')})"


def run(confirm_codes: set[str] | None = None, dart=None) -> dict:
    series = load_series()
    events = detect_events(series)
    by_code = defaultdict(list)
    for e in events:
        by_code[e["code"]].append(e)

    # 검산 (전 종목) — 독립 검증 아님, 계산·필드 정합성 확인
    worst, bad = 0.0, []
    for code, s in series.items():
        c = continuity(s, adjusted(s, by_code.get(code, [])))
        worst = max(worst, c)
        if c > 0.02:
            bad.append((code, s[-1]["name"], round(c, 4)))

    # 공시 확인: 지정 종목(표본·우선주 포함)만 — 전 종목은 2단계에서
    if dart is not None:
        from .collect.dart import DART  # noqa: F401
        corp = {c["stock_code"]: c["corp_code"] for c in dart.corpcode() if c["stock_code"]}
        target = [e for e in events if confirm_codes is None or e["code"][:5] + "0" in confirm_codes or e["code"] in confirm_codes]
        confirm_with_dart(target, dart, corp)

    # 독립 교차검증: 표본 종목 + 사건이 있는 종목 일부
    xcodes = sorted(set(list(confirm_codes or [])[:40]) | {e["code"] for e in events if e["class"] != "미세 기준가 조정"})[:80]
    xc = cross_check_fdr(xcodes, series, by_code)

    write_gz_json(L1 / "krx" / "adj_events.json.gz", events)
    out = REPORTS / "phase1" / "adjustment_events.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "name", "market", "prev_close", "base_price", "factor", "share_ratio", "class",
                    "confirmed_by", "dart_note"])
        for e in events:
            w.writerow([e["date"], e["code"], e["name"], e["mkt"], e["prev_close"], e["base"], round(e["factor"], 6),
                        None if e["share_ratio"] is None else round(e["share_ratio"], 6), e["class"],
                        e.get("confirmed_by", ""), e.get("dart_note", "")])
    cls = defaultdict(int)
    for e in events:
        cls[e["class"]] += 1
    res = {"stocks": len(series), "events": len(events), "by_class": dict(cls),
           "continuity_worst_pp": round(worst, 4), "continuity_fail": bad[:20], "continuity_fail_count": len(bad),
           "confirmed": sum(1 for e in events if e.get("confirmed_by")),
           "unconfirmed_checked": sum(1 for e in events if "confirmed_by" in e and not e["confirmed_by"]),
           "fdr_cross_check": xc}
    log(f"수정주가: {res}")
    return res


def cross_check_fdr(codes: list[str], series: dict, by_code: dict) -> dict:
    """비공식 수정주가와의 독립 교차검증: 같은 기간 누적수익률 차이(%p)."""
    try:
        import FinanceDataReader as fdr  # noqa: N813
    except ImportError:
        return {"available": False, "reason": "FinanceDataReader 미설치"}
    res = {}
    for code in codes:
        s = series.get(code)
        if not s or len(s) < 30:
            continue
        adj = adjusted(s, by_code.get(code, []))
        try:
            df = fdr.DataReader(code, f"{s[0]['d'][:4]}-{s[0]['d'][4:6]}-{s[0]['d'][6:]}",
                                f"{s[-1]['d'][:4]}-{s[-1]['d'][4:6]}-{s[-1]['d'][6:]}")
            closes = {d.strftime("%Y%m%d"): float(v) for d, v in df["Close"].items()}
        except Exception as e:  # noqa: BLE001
            res[code] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            continue
        common = [(d, a) for d, a in adj if d in closes]
        if len(common) < 30:
            res[code] = {"error": "공통 날짜 부족"}
            continue
        (d0, a0), (d1, a1) = common[0], common[-1]
        ours = (a1 / a0 - 1) * 100
        theirs = (closes[d1] / closes[d0] - 1) * 100
        res[code] = {"name": s[-1]["name"], "from": d0, "to": d1, "ours_pct": round(ours, 3), "fdr_pct": round(theirs, 3),
                     "diff_pp": round(ours - theirs, 3), "events": len(by_code.get(code, []))}
    return {"available": True, "results": res}


def verified_for(code: str, events: list[dict], start: str, end: str) -> tuple[bool, list[dict]]:
    """[start, end] 구간의 사건이 모두 확인됐는가 (미세·분할·병합은 자체 확인, 나머지는 DART 확인 필요)."""
    rel = [e for e in events if e["code"] == code and start < e["date"] <= end]
    ok = all(e.get("confirmed_by") or e["class"] in ("미세 기준가 조정", "주식분할", "주식병합") for e in rel)
    return ok, rel
