"""1단계 검증 실험: 코스피 관리종목 소스(D-15) · 상장폐지 소스(D-19).

D-15 관리종목
  정답지: KRX 코스닥 일별시세의 소속부(SECT_TP_NM) = '관리종목(소속부없음)'  ← 거래소 공식, 매 거래일
  후보 A: DART 거래소공시 '시장조치/안내'(I003)의 관리종목 지정·해제 제목으로 상태 복원
  후보 B: KIND 관리종목 현황 (현재 목록만, 지정일 포함) — robots.txt가 허용할 때만 수집
  방법: 코스닥에서 A를 정답지와 매일 비교해 누락률·오탐률을 잰 뒤, 같은 방법을 코스피에 적용.
        코스피 현재 시점은 A와 B를 교차 비교.

D-19 상장폐지
  KRX 일별시세: 마지막 관측일 (가격 이력의 끝)
  KRX 상장폐지종목 현황(MDCSTAT23801) — FinanceDataReader 공개 캐시(GitHub): 공식 폐지일·사유·정리매매 기간
  KIND 상장폐지 현황 — robots.txt 허용 시 시도
  DART 시장조치 공시 중 상장폐지 제목
  방법: 백필 기간에 시세가 끊긴 종목을 각 소스가 얼마나 설명하는지, 날짜가 맞는지, 과거 커버리지는 어디까지인지.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import urllib.robotparser
from collections import Counter, defaultdict

import requests

from .collect.dart import DART
from .http import UA
from .storage import L0, dart_path, exists, load_l0, save_l0
from .util import REPORTS, log, today_kst

ADMIN_TRUTH = "관리종목(소속부없음)"
RE_ADMIN = re.compile(r"관리종목")
RE_RELEASE = re.compile(r"관리종목\s*(지정)?\s*해제")
RE_NOT_EVENT = re.compile(r"우려|예고|가능성|사유\s*발생\s*예정|안내")
RE_DELIST = re.compile(r"상장폐지")
KIND = "https://kind.krx.co.kr"
FDR_CACHE = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing/delisting/{d}.csv"


# ── 공통 ──
def classify_title(nm: str) -> str | None:
    """시장조치 공시 제목 → 'designate' / 'release' / 'delist' / None."""
    t = re.sub(r"\[[^\]]*\]", "", nm or "")      # [정정] 등 제거
    if RE_ADMIN.search(t):
        if RE_RELEASE.search(t):
            return "release"
        if RE_NOT_EVENT.search(t):
            return None
        if re.search(r"관리종목\s*지정", t):
            return "designate"
        return None
    if RE_DELIST.search(t) and not re.search(r"우려|예고|가능성|이의신청|심사", t):
        return "delist"
    return None


def windows(start: dt.date, end: dt.date, months: int = 3):
    s = start
    while s <= end:
        m = s.month - 1 + months
        e = dt.date(s.year + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1)
        yield s, min(e, end)
        s = e + dt.timedelta(days=1)


def robots_allows(path: str) -> tuple[bool | None, str]:
    """KIND robots.txt 확인. (허용 여부, 근거). 확인 불가면 None → 수집하지 않음."""
    try:
        r = requests.get(f"{KIND}/robots.txt", headers=UA, timeout=30)
    except Exception as e:  # noqa: BLE001
        return None, f"robots.txt 접속 실패: {type(e).__name__}"
    if r.status_code == 404:
        return True, "robots.txt 없음(404) → 제한 없음"
    if r.status_code != 200:
        return None, f"robots.txt HTTP {r.status_code}"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    ok = rp.can_fetch(UA["User-Agent"], f"{KIND}{path}")
    return ok, f"robots.txt 규칙: {'허용' if ok else '금지'} ({path}) — 원문 앞부분: {r.text[:200]!r}"


# ── DART 시장조치 공시 ──
def collect_dart_notices(d: DART, start: dt.date) -> list[dict]:
    out = []
    today = today_kst()
    for cls in ("Y", "K"):
        for s, e in windows(start, today):
            path = dart_path("market_notice", cls, f"{s:%Y%m%d}.json.gz")
            recent = (today - e).days < 10
            if not exists(path) or recent:
                items, page = [], 1
                while True:
                    js = d._json("list.json", {"corp_cls": cls, "bgn_de": f"{s:%Y%m%d}", "end_de": f"{e:%Y%m%d}",
                                               "pblntf_detail_ty": "I003", "page_no": page, "page_count": 100})
                    if js.get("status") not in ("000", "013"):
                        raise RuntimeError(f"list.json I003 {cls} {s}: {js.get('status')} {js.get('message')}")
                    items += js.get("list") or []
                    if page >= int(js.get("total_page") or 1):
                        break
                    page += 1
                save_l0(path, source={"provider": "DART", "api": "list.json", "pblntf_detail_ty": "I003", "corp_cls": cls,
                                      "bgn_de": f"{s:%Y%m%d}", "end_de": f"{e:%Y%m%d}", "count": len(items)}, payload=items)
            out += load_l0(path)["payload"]
    return out


def admin_events(notices: list[dict]) -> dict[str, list[tuple[str, str, str]]]:
    """종목코드 → [(접수일, 'designate'|'release', 제목)] (같은 날 같은 종류는 1건)."""
    ev = defaultdict(set)
    for n in notices:
        k = classify_title(n.get("report_nm", ""))
        if k in ("designate", "release") and n.get("stock_code"):
            ev[n["stock_code"]].add((n["rcept_dt"], k, n["report_nm"].strip()))
    return {c: sorted(v) for c, v in ev.items()}


def state_on(events: list[tuple[str, str, str]], day: str) -> bool:
    """day(YYYYMMDD) 기준 관리종목 상태 = 그날까지 마지막 사건이 지정이면 True.
    같은 날 지정·해제가 함께 있으면 해제를 나중으로 본다 (정렬상 'designate' < 'release')."""
    st = False
    for d, k, _ in events:
        if d > day:
            break
        st = (k == "designate")
    return st


# ── KRX ──
def krx_daily(api: str) -> dict[str, list[dict]]:
    out = {}
    for p in sorted((L0 / "krx" / api).glob("*.json.gz")):
        rec = load_l0(p)
        if rec["meta"].get("rows"):
            out[p.name[:8]] = rec["payload"]
    return out


# ── KIND ──
def kind_admin_list() -> dict:
    ok, why = robots_allows("/investwarn/adminissue.do")
    res = {"robots": why, "allowed": ok}
    if not ok:
        return res
    try:
        import pandas as pd
        r = requests.get(f"{KIND}/investwarn/adminissue.do?method=searchAdminIssueSub&currentPageSize=5000"
                         f"&forward=adminissue_down", headers=UA, timeout=60)
        res["http"] = r.status_code
        df = pd.read_html(io.StringIO(r.content.decode("euc-kr", "replace")), header=0)[0]
        res["columns"] = list(df.columns)
        res["rows"] = df.astype(str).to_dict("records")
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res


def kind_delisting() -> dict:
    ok, why = robots_allows("/investwarn/delcompany.do")
    res = {"robots": why, "allowed": ok}
    if not ok:
        return res
    try:
        import pandas as pd
        r = requests.post(f"{KIND}/investwarn/delcompany.do",
                          data={"method": "searchDelCompanySub", "currentPageSize": "3000", "pageIndex": "1",
                                "orderMode": "1", "orderStat": "D", "forward": "delcompany_down", "marketType": "",
                                "fromDate": "2015-01-01", "toDate": f"{today_kst():%Y-%m-%d}"},
                          headers=UA, timeout=90)
        res["http"] = r.status_code
        df = pd.read_html(io.StringIO(r.content.decode("euc-kr", "replace")), header=0)[0]
        res["columns"] = list(df.columns)
        res["count"] = len(df)
        res["rows"] = df.astype(str).to_dict("records")
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res


def fdr_delisting() -> dict:
    """KRX 상장폐지종목 현황의 FinanceDataReader 공개 캐시 — 최근 날짜 파일을 찾는다."""
    import pandas as pd
    for i in range(0, 15):
        d = today_kst() - dt.timedelta(days=i)
        url = FDR_CACHE.format(d=f"{d:%Y-%m-%d}")
        try:
            r = requests.get(url, headers=UA, timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), index_col=0, dtype={"Symbol": str, "ToSymbol": str})
            return {"file_date": f"{d:%Y-%m-%d}", "count": len(df), "columns": list(df.columns),
                    "rows": df.astype(str).to_dict("records")}
    return {"error": "최근 15일 캐시 파일 없음"}


# ── 실험 본체 ──
def run(d: DART) -> dict:
    out_dir = REPORTS / "phase1" / "exp_listing"
    out_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {}

    notices = collect_dart_notices(d, dt.date(2015, 1, 1))
    titles = Counter()
    for n in notices:
        k = classify_title(n.get("report_nm", ""))
        if k or RE_ADMIN.search(n.get("report_nm", "")) or RE_DELIST.search(n.get("report_nm", "")):
            titles[(k or "제외", re.sub(r"\[[^\]]*\]", "", n["report_nm"]).strip()[:60])] += 1
    with (out_dir / "dart_titles.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["분류", "제목", "건수"])
        for (k, t), c in titles.most_common():
            w.writerow([k, t, c])
    ev = admin_events(notices)
    by_year = Counter(n["rcept_dt"][:4] for n in notices if classify_title(n.get("report_nm", "")) in ("designate", "release"))
    res["dart"] = {"notices_I003": len(notices), "admin_event_stocks": len(ev), "admin_events_by_year": dict(sorted(by_year.items())),
                   **d.http.stats()}

    # D-15 보정: 코스닥 정답지 vs DART 복원
    ksq = krx_daily("sto_ksq_bydd_trd")
    days = sorted(ksq)
    miss_tot = fp_tot = truth_tot = pred_tot = 0
    worst, mism = [], Counter()
    for day in days:
        rows = ksq[day]
        listed = {r["ISU_CD"] for r in rows}
        truth = {r["ISU_CD"] for r in rows if r.get("SECT_TP_NM") == ADMIN_TRUTH}
        pred = {c for c in listed if c in ev and state_on(ev[c], day)}
        miss, fp = truth - pred, pred - truth
        miss_tot += len(miss); fp_tot += len(fp); truth_tot += len(truth); pred_tot += len(pred)
        for c in miss:
            mism[(c, "누락")] += 1
        for c in fp:
            mism[(c, "오탐")] += 1
    names = {r["ISU_CD"]: r["ISU_NM"] for day in days[-30:] for r in ksq[day]}
    with (out_dir / "kosdaq_calibration_mismatch.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "type", "days", "dart_events"])
        for (c, t), n in mism.most_common():
            w.writerow([c, names.get(c, ""), t, n, " | ".join(f"{a} {b}" for a, b, _ in ev.get(c, []))])
    res["kosdaq_calibration"] = {
        "days": len(days), "from": days[0] if days else None, "to": days[-1] if days else None,
        "truth_stock_days": truth_tot, "dart_stock_days": pred_tot,
        "miss_rate": round(miss_tot / max(truth_tot, 1), 4), "false_pos_rate": round(fp_tot / max(pred_tot, 1), 4),
        "stocks_with_mismatch": len({c for c, _ in mism}),
        "latest_day_truth": len({r["ISU_CD"] for r in ksq[days[-1]] if r.get("SECT_TP_NM") == ADMIN_TRUTH}) if days else None,
    }

    # D-15 적용: 코스피 현재 — DART 복원 vs KIND
    stk = krx_daily("sto_stk_bydd_trd")
    last = max(stk)
    kospi_listed = {r["ISU_CD"]: r["ISU_NM"] for r in stk[last]}
    dart_kospi = {c for c in kospi_listed if c in ev and state_on(ev[c], last)}
    kind = kind_admin_list()
    kind_codes, kind_kospi = set(), set()
    if kind.get("rows"):
        for r in kind["rows"]:
            code = next((str(v).zfill(6) for k, v in r.items() if "코드" in str(k)), None)
            nm = next((v for k, v in r.items() if "회사" in str(k) or "종목" in str(k)), None)
            if code:
                kind_codes.add(code)
            if code in kospi_listed or (nm and nm in kospi_listed.values()):
                kind_kospi.add(code if code in kospi_listed else next(c for c, n in kospi_listed.items() if n == nm))
    res["kospi_current"] = {
        "asof": last, "dart_admin": sorted(f"{c} {kospi_listed[c]}" for c in dart_kospi),
        "kind": {k: v for k, v in kind.items() if k != "rows"}, "kind_rows": len(kind.get("rows") or []),
        "kind_kospi_admin": sorted(f"{c} {kospi_listed.get(c, '')}" for c in kind_kospi),
        "only_dart": sorted(f"{c} {kospi_listed[c]}" for c in dart_kospi - kind_kospi) if kind.get("rows") else None,
        "only_kind": sorted(f"{c} {kospi_listed.get(c, '')}" for c in kind_kospi - dart_kospi) if kind.get("rows") else None,
    }
    # KIND로 코스닥 현재도 교차 (KIND 자체의 정확도 — 정답지 대비)
    if kind.get("rows"):
        t_now = {r["ISU_CD"] for r in ksq[days[-1]] if r.get("SECT_TP_NM") == ADMIN_TRUTH}
        k_now = kind_codes & {r["ISU_CD"] for r in ksq[days[-1]]}
        res["kind_vs_kosdaq_truth"] = {"truth": len(t_now), "kind": len(k_now), "miss": sorted(t_now - k_now)[:30],
                                        "extra": sorted(k_now - t_now)[:30]}

    # D-19 상장폐지
    # 시장별 마지막 거래일 기준 (한 시장의 최신 파일이 비어 있으면 그 시장 전체가 '사라진' 것으로 오판하지 않도록)
    lastseen, nm, mkt_of = {}, {}, {}
    for mk, data in (("KOSPI", stk), ("KOSDAQ", ksq)):
        for day in sorted(data):
            for r in data[day]:
                lastseen[r["ISU_CD"]] = max(day, lastseen.get(r["ISU_CD"], ""))
                nm[r["ISU_CD"]] = (r["ISU_NM"], r.get("MKT_NM"))
                mkt_of[r["ISU_CD"]] = mk
    mlast = {"KOSPI": max(stk), "KOSDAQ": max(ksq)}
    gone = {c: dd for c, dd in lastseen.items() if dd < mlast[mkt_of[c]]}
    fdr = fdr_delisting()
    kdl = kind_delisting()
    dart_delist = defaultdict(list)
    for n in notices:
        if classify_title(n.get("report_nm", "")) == "delist" and n.get("stock_code"):
            dart_delist[n["stock_code"]].append(n["rcept_dt"])
    fmap = {}
    if fdr.get("rows"):
        for r in fdr["rows"]:
            fmap[str(r.get("Symbol", "")).zfill(6)] = r
    rows_out, stat = [], Counter()
    for c, dd in sorted(gone.items(), key=lambda x: x[1]):
        f = fmap.get(c)
        fdate = (f or {}).get("DelistingDate", "")[:10].replace("-", "")
        gap = None
        if fdate:
            try:
                gap = (dt.datetime.strptime(fdate, "%Y%m%d") - dt.datetime.strptime(dd, "%Y%m%d")).days
            except ValueError:
                pass
        st = "FDR 없음" if not f else ("날짜 일치(≤7일)" if gap is not None and 0 <= gap <= 7 else f"날짜 차이 {gap}일")
        stat[st] += 1
        rows_out.append([c, nm[c][0], nm[c][1], dd, fdate, gap, (f or {}).get("Reason", ""), (f or {}).get("ToSymbol", ""),
                         " ".join(sorted(set(dart_delist.get(c, []))))[:60], st])
    with (out_dir / "delisting_reconcile.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "market", "krx_last_seen", "fdr_delist_date", "gap_days", "fdr_reason", "to_symbol",
                    "dart_delist_notice", "status"])
        w.writerows(rows_out)
    fyears = Counter()
    if fdr.get("rows"):
        for r in fdr["rows"]:
            fyears[str(r.get("DelistingDate", ""))[:4]] += 1
    res["delisting"] = {
        "krx_gone_in_backfill": len(gone), "match": dict(stat),
        "fdr": {k: v for k, v in fdr.items() if k != "rows"},
        "fdr_by_year_2015plus": {y: n for y, n in sorted(fyears.items()) if y >= "2015"},
        "kind": {k: v for k, v in kdl.items() if k != "rows"},
        "dart_delist_notice_stocks": len(dart_delist),
    }
    import json
    (out_dir / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log(f"실험(관리종목·상장폐지): {res['kosdaq_calibration']} / {res['delisting']['match']}")
    return res
