"""0단계 데이터 확보 가능성 점검 (DESIGN 10장, reviews/2026-10-24_claude-response.md 하단 목록)

실행: GitHub Actions에서 KRX_API_KEY, DART_API_KEY 환경변수와 함께 실행.
  python scripts/phase0_probe.py --mode full     # 전체 점검 → reports/phase0/
  python scripts/phase0_probe.py --mode timing   # 당일 시세 제공 여부 1회 측정 → timing.csv 한 줄 추가

원칙
- 어떤 점검이 실패해도 스크립트는 멈추지 않고 결과(오류 포함)를 기록한다.
- API 키는 어떤 출력·파일에도 남기지 않는다 (scrub).
- 호출 수는 최소화하고 모두 기록한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import signal
import time
import traceback
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

KST = dt.timezone(dt.timedelta(hours=9))
KRX_KEY = os.environ.get("KRX_API_KEY", "").strip()
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
OUT = Path("reports/phase0")
UA = {"User-Agent": "quant-screener-phase0/0.1"}

KRX_BASES = ["https://data-dbg.krx.co.kr/svc/apis", "http://data-dbg.krx.co.kr/svc/apis"]
DART_BASE = "https://opendart.fss.or.kr/api"

CALLS = {"krx": 0, "dart": 0, "other": 0}
LOG: list[str] = []


# ───────────────────────────── 공통 ─────────────────────────────
def scrub(x):
    """키 문자열 제거 (재귀)."""
    if isinstance(x, str):
        for k in (KRX_KEY, DART_KEY):
            if k:
                x = x.replace(k, "***")
        return x
    if isinstance(x, dict):
        return {scrub(k): scrub(v) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    return x


def log(msg: str):
    line = f"[{dt.datetime.now(KST):%H:%M:%S}] {scrub(msg)}"
    print(line, flush=True)
    LOG.append(line)


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def safe(fn):
    """점검 함수 래퍼: 예외를 결과로 변환."""
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            log(f"!! {fn.__name__} 실패: {type(e).__name__}: {e}")
            return {"ok": False, "error": scrub(f"{type(e).__name__}: {e}"),
                    "trace": scrub(traceback.format_exc()[-1500:])}
    wrap.__name__ = fn.__name__
    return wrap


class Timeout(Exception):
    pass


def with_timeout(sec, fn, *a, **kw):
    def handler(signum, frame):
        raise Timeout(f"{sec}s 초과")
    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(sec)
    try:
        return fn(*a, **kw)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def first_list(obj):
    """응답 JSON에서 첫 번째 레코드 리스트를 찾는다 (KRX 블록명이 바뀌어도 동작)."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                return v
    return None


# ───────────────────────────── KRX ─────────────────────────────
KRX_STATE = {"base": None, "auth": None}  # 성공한 호출 방식 기억


def krx_get(api: str, basDd: str):
    """KRX OPEN API 호출. 반환: (rows|None, meta)."""
    tried = []
    combos = []
    bases = [KRX_STATE["base"]] if KRX_STATE["base"] else KRX_BASES
    auths = [KRX_STATE["auth"]] if KRX_STATE["auth"] else ["header", "query"]
    for b in bases:
        for a in auths:
            combos.append((b, a))
    for base, auth in combos:
        url = f"{base}/{api}"
        params = {"basDd": basDd}
        headers = dict(UA)
        if auth == "header":
            headers["AUTH_KEY"] = KRX_KEY
        else:
            params["AUTH_KEY"] = KRX_KEY
        CALLS["krx"] += 1
        try:
            r = requests.get(url, params=params, headers=headers, timeout=60)
        except Exception as e:  # noqa: BLE001
            tried.append({"base": base, "auth": auth, "error": scrub(str(e))[:300]})
            continue
        meta = {"base": base, "auth": auth, "status": r.status_code,
                "content_type": r.headers.get("content-type", ""), "bytes": len(r.content)}
        if r.status_code != 200:
            meta["body_head"] = scrub(r.text[:300])
            tried.append(meta)
            continue
        try:
            js = r.json()
        except ValueError:
            meta["body_head"] = scrub(r.text[:300])
            tried.append(meta)
            continue
        rows = first_list(js)
        if rows is None:
            meta["json_keys"] = list(js.keys()) if isinstance(js, dict) else str(type(js))
            meta["body_head"] = scrub(json.dumps(js, ensure_ascii=False)[:300])
            tried.append(meta)
            continue
        KRX_STATE["base"], KRX_STATE["auth"] = base, auth
        meta["block_keys"] = list(js.keys()) if isinstance(js, dict) else None
        return rows, meta
    return None, {"tried": tried}


def find_trading_day_rows(api: str, start: dt.date, max_days: int = 10, step: int = 1):
    """start부터 하루씩 이동하며 레코드가 있는 첫 날을 찾는다."""
    d = start
    attempts = []
    for _ in range(max_days):
        if d.weekday() < 5:
            rows, meta = krx_get(api, d.strftime("%Y%m%d"))
            n = len(rows) if rows else 0
            attempts.append({"basDd": d.strftime("%Y%m%d"), "rows": n,
                             "status": meta.get("status"), "note": meta.get("tried") and "all-failed"})
            if n > 0:
                return d, rows, attempts
            if rows is None and "tried" in meta:
                # 인증/엔드포인트 자체 실패면 더 시도하지 않음
                return None, None, attempts + [{"fail_meta": meta}]
        d = d + dt.timedelta(days=step)
    return None, None, attempts


@safe
def krx_access():
    """K0: 4개(+지수 2개) 서비스 접근 가능 여부."""
    today = now_kst().date()
    out = {}
    for api in ["sto/stk_bydd_trd", "sto/ksq_bydd_trd", "sto/stk_isu_base_info",
                "sto/ksq_isu_base_info", "idx/kospi_dd_trd", "idx/kosdaq_dd_trd"]:
        day, rows, attempts = find_trading_day_rows(api, today, max_days=10, step=-1)
        rec = {"latest_day_with_data": day.isoformat() if day else None,
               "rows": len(rows) if rows else 0, "attempts": attempts[-3:]}
        if rows:
            rec["fields"] = list(rows[0].keys())
            rec["sample"] = rows[:2]
        out[api] = rec
        log(f"K0 {api}: 최근 데이터일 {rec['latest_day_with_data']} / {rec['rows']}건")
    out["call_style"] = dict(KRX_STATE)
    return {"ok": True, **out}


@safe
def krx_history_range():
    """K2: 과거 기준일 조회 가능 범위 (연도별 첫 영업일 탐색)."""
    out = {}
    for y in [1996, 2000, 2005, 2010, 2013, 2015, 2018, 2021, 2024]:
        day, rows, attempts = find_trading_day_rows("sto/stk_bydd_trd", dt.date(y, 1, 2), max_days=8)
        out[str(y)] = {"day": day.isoformat() if day else None, "rows": len(rows) if rows else 0}
        log(f"K2 {y}: {out[str(y)]}")
    return {"ok": True, "by_year": out}


@safe
def krx_survivorship(latest_rows_kospi, latest_rows_kosdaq):
    """K3: 과거 시세에 현재 상장폐지된 종목이 포함되는가 (생존자 편향 점검)."""
    def codes(rows):
        s = set()
        for r in rows or []:
            c = r.get("ISU_CD") or r.get("ISU_SRT_CD") or r.get("isu_cd")
            if c:
                s.add(str(c))
        return s
    now_codes = codes(latest_rows_kospi) | codes(latest_rows_kosdaq)
    out = {"now_count": len(now_codes)}
    for y in [2016, 2020]:
        d1, r1, _ = find_trading_day_rows("sto/stk_bydd_trd", dt.date(y, 1, 4), max_days=8)
        d2, r2, _ = find_trading_day_rows("sto/ksq_bydd_trd", dt.date(y, 1, 4), max_days=8)
        old = codes(r1) | codes(r2)
        gone = sorted(old - now_codes)
        sample = []
        names = {str(r.get("ISU_CD")): r.get("ISU_NM") for r in (r1 or []) + (r2 or [])}
        for c in gone[:15]:
            sample.append({"code": c, "name": names.get(c)})
        out[str(y)] = {"day": d1.isoformat() if d1 else None, "count": len(old),
                       "absent_now": len(gone), "absent_sample": sample}
        log(f"K3 {y}: {len(old)}종목 중 현재 없음 {len(gone)}")
    return {"ok": True, **out}


@safe
def krx_adjustment_check(latest_rows):
    """K6: 수정주가 제공 여부 — 필드 목록에서 '수정/adj' 흔적 확인."""
    fields = list(latest_rows[0].keys()) if latest_rows else []
    hint = [f for f in fields if re.search(r"ADJ|adj|수정", f)]
    return {"ok": True, "fields": fields, "adjustment_fields": hint,
            "note": "수정주가 필드가 없으면 기업행위(분할·병합·증자) 정보로 직접 보정 필요"}


@safe
def krx_base_info_detail():
    """K4: 종목기본정보 — 주식종류(보통/우선), 소속부(관리종목 등) 표현 확인."""
    today = now_kst().date()
    out = {}
    for api in ["sto/stk_isu_base_info", "sto/ksq_isu_base_info"]:
        day, rows, _ = find_trading_day_rows(api, today, max_days=10, step=-1)
        if not rows:
            out[api] = {"rows": 0}
            continue
        fields = list(rows[0].keys())
        uniq = {}
        for f in fields:
            vals = {str(r.get(f)) for r in rows}
            if 1 < len(vals) <= 25:
                uniq[f] = sorted(vals)
        out[api] = {"day": day.isoformat(), "rows": len(rows), "fields": fields,
                    "categorical_values": uniq}
    return {"ok": True, **out}


def timing_probe():
    """D-08: 오늘(KST) 시세가 지금 시점에 제공되는가."""
    t = now_kst()
    rows, meta = krx_get("sto/stk_bydd_trd", t.strftime("%Y%m%d"))
    n = len(rows) if rows else 0
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / "timing.csv"
    new = not f.exists()
    with f.open("a", encoding="utf-8") as fp:
        if new:
            fp.write("run_at_kst,weekday,basDd,rows,status\n")
        fp.write(f"{t:%Y-%m-%d %H:%M},{t:%a},{t:%Y%m%d},{n},{meta.get('status')}\n")
    log(f"timing: {t:%Y-%m-%d %H:%M} 당일 시세 {n}건")


# ───────────────────────────── DART ─────────────────────────────
def dart_get(path: str, params: dict, raw: bool = False):
    CALLS["dart"] += 1
    p = {"crtfc_key": DART_KEY, **params}
    r = requests.get(f"{DART_BASE}/{path}", params=p, headers=UA, timeout=90)
    if raw:
        return r
    r.raise_for_status()
    return r.json()


CORP = {}  # stock_code → corp_code


@safe
def dart_corpcode():
    """D1: 고유번호 목록 → 상장사 수, 종목코드 매핑."""
    r = dart_get("corpCode.xml", {}, raw=True)
    meta = {"status": r.status_code, "content_type": r.headers.get("content-type", ""), "bytes": len(r.content)}
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml = z.read(z.namelist()[0])
    root = ET.fromstring(xml)
    total = listed = 0
    for el in root.iter("list"):
        total += 1
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            listed += 1
            CORP[sc] = el.findtext("corp_code")
    log(f"D1 corpCode: 전체 {total}, 종목코드 있는 회사 {listed}")
    return {"ok": True, **meta, "total": total, "with_stock_code": listed}


def fs_rows(corp_code, year, reprt, fs_div):
    js = dart_get("fnlttSinglAcntAll.json", {"corp_code": corp_code, "bsns_year": str(year),
                                              "reprt_code": reprt, "fs_div": fs_div})
    return js.get("status"), js.get("message"), js.get("list") or []


@safe
def dart_fs_structure():
    """D2·D5: 삼성전자 재무 응답 구조 — rcept_no 포함 여부, 누적/3개월 금액 필드, 지배주주 계정."""
    cc = CORP.get("005930", "00126380")
    out = {}
    for label, year, reprt in [("FY2024_사업", 2024, "11011"), ("FY2025_3분기", 2025, "11014"),
                               ("FY2026_반기", 2026, "11012")]:
        st, msg, rows = fs_rows(cc, year, reprt, "CFS")
        rec = {"status": st, "message": msg, "rows": len(rows)}
        if rows:
            rec["fields"] = list(rows[0].keys())
            rec["rcept_no_values"] = sorted({r.get("rcept_no") for r in rows if r.get("rcept_no")})
            rec["sj_div"] = sorted({r.get("sj_div") for r in rows})
            owners = [r for r in rows if re.search(r"지배기업|지배주주|소유주", r.get("account_nm") or "")]
            rec["owner_accounts"] = [{k: r.get(k) for k in ("sj_div", "account_id", "account_nm",
                                                            "thstrm_amount", "thstrm_add_amount")}
                                     for r in owners[:12]]
            ni = [r for r in rows if r.get("sj_div") in ("IS", "CIS") and
                  re.search(r"당기순이익|분기순이익|반기순이익", r.get("account_nm") or "")]
            rec["net_income_rows"] = [{k: r.get(k) for k in ("sj_div", "account_id", "account_nm",
                                                             "thstrm_nm", "thstrm_amount", "thstrm_add_amount")}
                                      for r in ni[:8]]
            rec["has_add_amount"] = any(r.get("thstrm_add_amount") not in (None, "") for r in rows)
        out[label] = rec
        log(f"D2 {label}: status {st}, {len(rows)}행")
    return {"ok": True, "corp_code": cc, **out}


@safe
def dart_year_range():
    """D3: 재무 API 제공 시작 연도 (삼성전자 사업보고서)."""
    cc = CORP.get("005930", "00126380")
    out = {}
    for y in [2013, 2014, 2015, 2016]:
        st, msg, rows = fs_rows(cc, y, "11011", "CFS")
        out[str(y)] = {"status": st, "message": msg, "rows": len(rows)}
        log(f"D3 {y}: {st} {len(rows)}행")
    return {"ok": True, **out}


@safe
def dart_company():
    """D4: 기업개황 업종코드."""
    out = {}
    for sc in ["005930", "005380", "035720", "000660"]:
        cc = CORP.get(sc)
        if not cc:
            continue
        js = dart_get("company.json", {"corp_code": cc})
        out[sc] = {k: js.get(k) for k in ("status", "corp_name", "induty_code", "corp_cls", "acc_mt", "est_dt")}
    log(f"D4 company: {out}")
    return {"ok": True, **out}


def list_all(params, max_pages=10):
    items, page = [], 1
    while page <= max_pages:
        js = dart_get("list.json", {**params, "page_no": page, "page_count": 100})
        if js.get("status") != "000":
            return items, js.get("status"), js.get("message")
        items += js.get("list") or []
        if page >= int(js.get("total_page") or 1):
            break
        page += 1
    return items, "000", "ok"


@safe
def dart_amendment_behavior():
    """D2(핵심): 정정공시가 있을 때 fnlttSinglAcntAll이 어떤 접수번호(최초/정정)의 값을 주는가."""
    # 회사 지정 없는 검색은 기간 3개월 이내 제한 → 구간 분할
    items, st = [], "000"
    for bgn, end in [("20250401", "20250630"), ("20250701", "20250930"), ("20251001", "20251231")]:
        part, st, msg = list_all({"bgn_de": bgn, "end_de": end, "pblntf_ty": "A",
                                  "pblntf_detail_ty": "A001", "last_reprt_at": "N"}, max_pages=10)
        items += part
        log(f"D2b list.json {bgn}~{end}: {len(part)}건 (status {st} {msg})")
    amended = [i for i in items if "정정" in (i.get("report_nm") or "") and i.get("stock_code")
               and "2024.12" in (i.get("report_nm") or "")]
    samples = []
    seen = set()
    for it in amended:
        cc = it["corp_code"]
        if cc in seen:
            continue
        seen.add(cc)
        versions, _, _ = list_all({"corp_code": cc, "bgn_de": "20250101", "end_de": "20251231",
                                   "pblntf_detail_ty": "A001", "last_reprt_at": "N"}, max_pages=2)
        versions = [v for v in versions if "2024.12" in (v.get("report_nm") or "")]
        versions.sort(key=lambda v: v.get("rcept_no"))
        if len(versions) < 2:
            continue
        fs_div_used, returned = None, []
        for fs_div in ("CFS", "OFS"):
            st2, _, rows = fs_rows(cc, 2024, "11011", fs_div)
            if rows:
                fs_div_used = fs_div
                returned = sorted({r.get("rcept_no") for r in rows if r.get("rcept_no")})
                break
        vnos = [v["rcept_no"] for v in versions]
        verdict = ("정정본(최신)" if returned and returned[-1] == vnos[-1]
                   else "최초본" if returned and returned[-1] == vnos[0]
                   else "중간본/불명" if returned else "재무 데이터 없음")
        samples.append({"corp_name": it.get("corp_name"), "stock_code": it.get("stock_code"),
                        "versions": [{"rcept_no": v["rcept_no"], "rcept_dt": v.get("rcept_dt"),
                                      "report_nm": v.get("report_nm")} for v in versions],
                        "fs_div": fs_div_used, "returned_rcept_no": returned, "verdict": verdict})
        log(f"D2b {it.get('corp_name')}: 버전 {len(versions)}개 → 반환 {returned} → {verdict}")
        if len(samples) >= 6:
            break
    tally = {}
    for s in samples:
        tally[s["verdict"]] = tally.get(s["verdict"], 0) + 1
    return {"ok": True, "searched": len(items), "amended_candidates": len(amended),
            "samples": samples, "tally": tally}


@safe
def dart_xbrl_original(amend_result):
    """D7: 접수번호별 원본(XBRL) 재무제표 파일 조회 가능 여부 — 최초본 접수번호로 시도."""
    samples = (amend_result or {}).get("samples") or []
    if not samples:
        return {"ok": False, "error": "정정 표본 없음"}
    first = samples[0]["versions"][0]["rcept_no"]
    r = dart_get("fnlttXbrl.xml", {"rcept_no": first, "reprt_code": "11011"}, raw=True)
    info = {"rcept_no": first, "status": r.status_code, "content_type": r.headers.get("content-type", ""),
            "bytes": len(r.content)}
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        info["zip_files"] = z.namelist()[:10]
    except zipfile.BadZipFile:
        info["body_head"] = scrub(r.text[:300])
    log(f"D7 XBRL 원본: {info.get('status')} {info.get('bytes')}B")
    return {"ok": True, **info}


@safe
def dart_filing_lag():
    """D6: 정기보고서 접수일 분포 (최근 분기보고서 — 법정기한 대비 실제 공시 시점)."""
    items, st, msg = list_all({"bgn_de": "20251001", "end_de": "20251231", "pblntf_ty": "A",
                               "pblntf_detail_ty": "A003", "last_reprt_at": "Y"}, max_pages=30)
    q3 = [i for i in items if "2025.09" in (i.get("report_nm") or "") and i.get("stock_code")
          and i.get("corp_cls") in ("Y", "K")]
    by_day = {}
    for i in q3:
        by_day[i["rcept_dt"]] = by_day.get(i["rcept_dt"], 0) + 1
    log(f"D6 2025 3분기보고서(상장 Y/K): {len(q3)}건")
    return {"ok": True, "total": len(q3), "by_rcept_dt": dict(sorted(by_day.items()))}


# ───────────────────────── 상장폐지 이력 후보 ─────────────────────────
@safe
def delisting_sources():
    """상장폐지 목록 확보 경로 후보 점검."""
    out = {}
    try:
        import FinanceDataReader as fdr  # noqa: N813
        CALLS["other"] += 1
        df = with_timeout(120, fdr.StockListing, "KRX-DELISTING")
        out["fdr_KRX_DELISTING"] = {"ok": True, "rows": len(df), "columns": list(map(str, df.columns)),
                                    "sample": json.loads(df.head(3).to_json(orient="records", force_ascii=False,
                                                                            date_format="iso"))}
    except Exception as e:  # noqa: BLE001
        out["fdr_KRX_DELISTING"] = {"ok": False, "error": scrub(f"{type(e).__name__}: {e}")[:300]}
    for name, url in [("KIND_delcompany", "https://kind.krx.co.kr/investwarn/delcompany.do?method=searchDelCompanyMain")]:
        try:
            CALLS["other"] += 1
            r = requests.get(url, headers=UA, timeout=30)
            out[name] = {"status": r.status_code, "bytes": len(r.content),
                         "has_table_hint": ("상장폐지" in r.text)}
        except Exception as e:  # noqa: BLE001
            out[name] = {"ok": False, "error": scrub(str(e))[:300]}
    log(f"상장폐지 소스: { {k: v.get('ok', v.get('status')) for k, v in out.items()} }")
    return {"ok": True, **out}


# ───────────────────────────── 보고서 ─────────────────────────────
def verdict_rows(R):
    def g(path, default=None):
        cur = R
        for p in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(p)
        return cur if cur is not None else default

    rows = []
    k0 = g(["krx_access"], {})
    for api in ["sto/stk_bydd_trd", "sto/ksq_bydd_trd", "sto/stk_isu_base_info",
                "sto/ksq_isu_base_info", "idx/kospi_dd_trd", "idx/kosdaq_dd_trd"]:
        rec = k0.get(api) or {}
        rows.append((f"KRX {api}", "가능" if rec.get("rows") else "불가/미승인",
                     f"최근 데이터일 {rec.get('latest_day_with_data')} · {rec.get('rows', 0)}건"))
    hist = g(["krx_history_range", "by_year"], {})
    have = [y for y, v in hist.items() if v.get("rows")]
    rows.append(("KRX 과거 시세 조회 범위", f"{min(have)}년~" if have else "확인 실패",
                 ", ".join(f"{y}:{v.get('rows')}" for y, v in hist.items())))
    sv = g(["krx_survivorship"], {})
    s16 = sv.get("2016") or {}
    rows.append(("과거 시세에 상장폐지 종목 포함", "포함" if s16.get("absent_now") else "확인 필요",
                 f"2016년 초 {s16.get('count')}종목 중 현재 없음 {s16.get('absent_now')}"))
    adj = g(["krx_adjustment"], {})
    rows.append(("수정주가 필드", "있음" if adj.get("adjustment_fields") else "없음 → 직접 보정 필요",
                 ", ".join(adj.get("adjustment_fields") or [])))
    yr = g(["dart_year_range"], {})
    rows.append(("DART 재무 제공 시작 연도", "", ", ".join(f"{y}:{v.get('status')}/{v.get('rows')}"
                                                     for y, v in yr.items() if isinstance(v, dict))))
    am = g(["dart_amendment"], {})
    rows.append(("정정공시 시 재무 API 반환값", json.dumps(am.get("tally"), ensure_ascii=False) if am.get("tally") else "표본 없음",
                 f"표본 {len(am.get('samples') or [])}건"))
    xb = g(["dart_xbrl_original"], {})
    rows.append(("접수번호별 원본(XBRL) 조회", "가능" if xb.get("zip_files") else "불가/확인 필요",
                 f"HTTP {xb.get('status')} · {xb.get('bytes')}B"))
    ds = g(["delisting"], {})
    f1 = ds.get("fdr_KRX_DELISTING") or {}
    rows.append(("상장폐지 목록 (FinanceDataReader)", "가능" if f1.get("ok") else "불가",
                 f"{f1.get('rows')}건" if f1.get("ok") else (f1.get("error") or "")[:120]))
    k = ds.get("KIND_delcompany") or {}
    rows.append(("상장폐지 목록 (KIND 페이지)", "접속 가능" if k.get("status") == 200 else "불가",
                 f"HTTP {k.get('status')}"))
    return rows


def write_report(R):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "phase0_raw.json").write_text(json.dumps(scrub(R), ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    lines = [f"# 0단계 점검 결과 — {now_kst():%Y-%m-%d %H:%M} KST", "",
             "> 자동 생성 (`scripts/phase0_probe.py`). 원자료: `phase0_raw.json`", "",
             "| 항목 | 판정 | 근거 |", "|---|---|---|"]
    for a, b, c in verdict_rows(R):
        lines.append(f"| {a} | {b} | {scrub(str(c)).replace('|', '/')} |")
    lines += ["", f"호출 수: KRX {CALLS['krx']} · DART {CALLS['dart']} · 기타 {CALLS['other']}", "",
              "## 실행 로그", "```", *LOG, "```"]
    (OUT / "phase0_report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "timing", "amend"], default="full")
    args = ap.parse_args()
    if not KRX_KEY or not DART_KEY:
        log(f"키 확인: KRX {'있음' if KRX_KEY else '없음'} / DART {'있음' if DART_KEY else '없음'}")
    if args.mode == "timing":
        timing_probe()
        return
    if args.mode == "amend":
        # 정정공시 점검만 재실행 → 기존 raw에 병합
        raw_f = OUT / "phase0_raw.json"
        R = json.loads(raw_f.read_text(encoding="utf-8")) if raw_f.exists() else {}
        dart_corpcode()
        R["dart_amendment"] = dart_amendment_behavior()
        R["dart_xbrl_original"] = dart_xbrl_original(R["dart_amendment"])
        R["amend_rerun_at"] = now_kst().isoformat()
        R["calls_amend"] = dict(CALLS)
        write_report(R)
        log(f"정정 점검 완료. 호출 수 {CALLS}")
        return

    R = {"run_at": now_kst().isoformat()}
    R["krx_access"] = krx_access()
    k0 = R["krx_access"] if isinstance(R["krx_access"], dict) else {}
    latest_kospi = (k0.get("sto/stk_bydd_trd") or {}).get("sample")
    # 생존자 점검에는 전체 행이 필요 → 다시 조회
    d, rows_kospi, _ = find_trading_day_rows("sto/stk_bydd_trd", now_kst().date(), 10, -1)
    d2, rows_kosdaq, _ = find_trading_day_rows("sto/ksq_bydd_trd", now_kst().date(), 10, -1)
    R["krx_history_range"] = krx_history_range()
    R["krx_survivorship"] = krx_survivorship(rows_kospi, rows_kosdaq)
    R["krx_adjustment"] = krx_adjustment_check(rows_kospi or latest_kospi)
    R["krx_base_info"] = krx_base_info_detail()

    R["dart_corpcode"] = dart_corpcode()
    R["dart_fs_structure"] = dart_fs_structure()
    R["dart_year_range"] = dart_year_range()
    R["dart_company"] = dart_company()
    R["dart_amendment"] = dart_amendment_behavior()
    R["dart_xbrl_original"] = dart_xbrl_original(R["dart_amendment"])
    R["dart_filing_lag"] = dart_filing_lag()

    R["delisting"] = delisting_sources()
    R["calls"] = CALLS
    write_report(R)
    log(f"완료. 호출 수 {CALLS}")


if __name__ == "__main__":
    main()
