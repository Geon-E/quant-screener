"""OpenDART 수집기.

- corpCode: 고유번호 ↔ 종목코드
- company.json: 업종코드(KSIC)·결산월
- list.json: 정기보고서 전 버전 (최초·정정) — 접수번호·접수일 (시점 정합성의 근거)
- fnlttSinglAcntAll: 보고서별 전체 재무 (CFS 우선, 없으면 OFS). 응답은 최신 정정본 (0단계 실측)
- fnlttXbrl: 정정 이력이 있는 보고서의 최초본 원본 XBRL (D-17)

모든 응답(데이터 없음 '013' 포함)을 L0에 남겨 재실행 시 다시 호출하지 않는다.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from xml.etree import ElementTree as ET

from ..http import Client
from ..storage import dart_path, exists, load_l0, save_l0
from ..util import log, now_kst, sha256_bytes, today_kst

BASE = "https://opendart.fss.or.kr/api"
REPRT = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업"}


class DART:
    def __init__(self, budget: int = 15000):
        self.key = os.environ.get("DART_API_KEY", "").strip()
        self.http = Client("DART", budget=budget, min_interval=0.12)

    # ── 저수준 ──
    def _json(self, path: str, params: dict) -> dict:
        r = self.http.get(f"{BASE}/{path}", params={"crtfc_key": self.key, **params}, timeout=90)
        r.raise_for_status()
        js = r.json()
        if js.get("status") == "020":
            from ..http import BudgetExceeded
            raise BudgetExceeded("DART 일일 호출 한도 초과(020)")
        return js

    # ── corpCode ──
    def corpcode(self) -> dict[str, dict]:
        """stock_code → {corp_code, corp_name, stock_code, modify_date}. 하루 1회만 받는다."""
        path = dart_path("corpcode", f"{today_kst():%Y%m%d}.json.gz")
        if not exists(path):
            r = self.http.get(f"{BASE}/corpCode.xml", params={"crtfc_key": self.key}, timeout=120)
            z = zipfile.ZipFile(io.BytesIO(r.content))
            root = ET.fromstring(z.read(z.namelist()[0]))
            items = []
            for el in root.iter("list"):
                items.append({k: (el.findtext(k) or "").strip() for k in ("corp_code", "corp_name", "stock_code", "modify_date")})
            save_l0(path, source={"provider": "DART", "api": "corpCode.xml", "count": len(items)}, payload=items)
        items = load_l0(path)["payload"]
        return items

    # ── company ──
    def company(self, corp_code: str) -> dict:
        path = dart_path("company", f"{corp_code}.json.gz")
        if not exists(path):
            js = self._json("company.json", {"corp_code": corp_code})
            save_l0(path, source={"provider": "DART", "api": "company.json", "corp_code": corp_code}, payload=js)
        return load_l0(path)["payload"]

    # ── 공시 목록 (정기보고서 전 버전) ──
    def filings(self, corp_code: str, refresh: bool = False) -> list[dict]:
        path = dart_path("list", f"{corp_code}.json.gz")
        if exists(path) and not refresh:
            return load_l0(path)["payload"]
        items, page = [], 1
        while True:
            js = self._json("list.json", {"corp_code": corp_code, "bgn_de": "20150101", "end_de": f"{today_kst():%Y%m%d}",
                                          "pblntf_ty": "A", "last_reprt_at": "N", "page_no": page, "page_count": 100})
            if js.get("status") not in ("000", "013"):
                raise RuntimeError(f"list.json {corp_code}: {js.get('status')} {js.get('message')}")
            items += js.get("list") or []
            if page >= int(js.get("total_page") or 1):
                break
            page += 1
        save_l0(path, source={"provider": "DART", "api": "list.json", "corp_code": corp_code, "count": len(items)},
                payload=items)
        return items

    # ── 재무 ──
    def fs(self, corp_code: str, year: int, reprt: str) -> tuple[str | None, dict]:
        """CFS 우선, 없으면 OFS. 반환: (사용한 fs_div 또는 None, 응답)."""
        last = None
        for fs_div in ("CFS", "OFS"):
            path = dart_path("fs", corp_code, f"{year}_{reprt}_{fs_div}.json.gz")
            if not exists(path):
                js = self._json("fnlttSinglAcntAll.json", {"corp_code": corp_code, "bsns_year": str(year),
                                                           "reprt_code": reprt, "fs_div": fs_div})
                save_l0(path, source={"provider": "DART", "api": "fnlttSinglAcntAll", "corp_code": corp_code,
                                      "bsns_year": year, "reprt_code": reprt, "fs_div": fs_div,
                                      "status": js.get("status"), "rows": len(js.get("list") or [])}, payload=js)
            js = load_l0(path)["payload"]
            last = js
            if js.get("status") == "000" and js.get("list"):
                return fs_div, js
        return None, last

    # ── 감사·검토의견 ──
    def audit(self, corp_code: str, year: int, reprt: str) -> dict:
        """회계감사인의 명칭 및 감사의견 (정기보고서 주요정보). 사업보고서=감사의견, 반기=검토의견."""
        path = dart_path("audit", corp_code, f"{year}_{reprt}.json.gz")
        if not exists(path):
            js = self._json("accnutAdtorNmNdAdtOpinion.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt})
            save_l0(path, source={"provider": "DART", "api": "accnutAdtorNmNdAdtOpinion", "corp_code": corp_code,
                                  "bsns_year": year, "reprt_code": reprt, "status": js.get("status")}, payload=js)
        return load_l0(path)["payload"]

    # ── 배당에 관한 사항 (사업보고서) ──
    def dividend(self, corp_code: str, year: int) -> dict:
        """주당 현금배당금·배당수익률·배당성향 등. 원천값 대조(xcheck)와 같은 L0 경로를 써서 중복 호출을 피한다."""
        path = dart_path("xcheck", "alotMatter", corp_code, f"{year}_11011.json.gz")
        if not exists(path):
            js = self._json("alotMatter.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": "11011"})
            save_l0(path, source={"provider": "DART", "api": "alotMatter", "corp_code": corp_code, "bsns_year": year,
                                  "reprt_code": "11011", "status": js.get("status"), "rows": len(js.get("list") or [])}, payload=js)
        return load_l0(path)["payload"]

    # ── 원본 XBRL ──
    def xbrl(self, rcept_no: str, reprt: str) -> dict:
        path = dart_path("xbrl", f"{rcept_no}.zip")
        meta_path = dart_path("xbrl", f"{rcept_no}.meta.json.gz")
        if exists(meta_path):
            return load_l0(meta_path)["payload"]
        r = self.http.get(f"{BASE}/fnlttXbrl.xml", params={"crtfc_key": self.key, "rcept_no": rcept_no,
                                                          "reprt_code": reprt}, timeout=120)
        info = {"rcept_no": rcept_no, "reprt_code": reprt, "status": r.status_code, "bytes": len(r.content),
                "content_type": r.headers.get("content-type", ""), "ok": False}
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            info["files"] = z.namelist()
            info["ok"] = True
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            info["sha256"] = sha256_bytes(r.content)
        except zipfile.BadZipFile:
            info["body_head"] = r.text[:300]
        save_l0(meta_path, source={"provider": "DART", "api": "fnlttXbrl.xml", "rcept_no": rcept_no}, payload=info)
        return info


def audit_opinion(js: dict) -> tuple[str | None, bool | None]:
    """감사의견 응답 → (당기 의견 원문, 적정 여부). 판단 불가면 (None, None)."""
    rows = js.get("list") or [] if js.get("status") == "000" else []
    cur = [r for r in rows if "당기" in (r.get("bsns_year") or "")] or rows[:1]
    for r in cur:
        op = (r.get("adt_opinion") or "").strip()
        if not op or op == "-":
            continue
        # 비적정 = 한정·부적정·의견거절·범위제한이 명시된 경우만. 반기 검토의견의 '예외사항 없음'·'해당사항 없음'은 적정
        # (2026-10-25 실데이터: 처음엔 '적정' 문구가 있어야 적정으로 봐서 검토의견 2건을 잘못 제외)
        bad = any(k in op for k in ("한정", "부적정", "거절", "범위제한", "의견표명 안함", "Qualified", "Adverse", "Disclaimer"))
        return op, not bad
    return None, None


def parse_dividend_hist(js: dict) -> dict:
    """사업보고서 배당 항목 하나로 3개년(당기·전기·전전기) 보통주 주당배당금을 읽는다 → {dps: [당기, 전기, 전전기], payout, yield_pct}."""
    from ..util import to_num
    out = {"dps": [None, None, None], "payout_pct": None, "yield_pct": None, "eps": None}
    if js.get("status") != "000":
        return out
    for r in js.get("list") or []:
        se = (r.get("se") or "").replace(" ", "")
        knd = (r.get("stock_knd") or "").replace(" ", "")
        common = (not knd) or ("보통" in knd)
        if "주당현금배당금" in se and common and out["dps"] == [None, None, None]:
            out["dps"] = [to_num(r.get("thstrm")), to_num(r.get("frmtrm")), to_num(r.get("lwfr"))]
        elif "현금배당성향" in se and out["payout_pct"] is None and ("연결" in se or "(" not in se):
            out["payout_pct"] = to_num(r.get("thstrm"))
        elif "현금배당수익률" in se and common and out["yield_pct"] is None:
            out["yield_pct"] = to_num(r.get("thstrm"))
        elif "주당순이익" in se and out["eps"] is None and ("연결" in se or "(" not in se):
            out["eps"] = to_num(r.get("thstrm"))
    return out


def parse_dividend(js: dict) -> dict:
    """배당 항목 → 당기 보통주 기준 {dps, yield_pct, payout_pct, total_mn}. 분기·중간배당은 사업보고서의 연간 합계를 따른다."""
    from ..util import to_num
    out = {"dps": None, "yield_pct": None, "payout_pct": None, "total_mn": None}
    if js.get("status") != "000":
        return out
    for r in js.get("list") or []:
        se = (r.get("se") or "").replace(" ", "")
        knd = (r.get("stock_knd") or "").replace(" ", "")
        common = (not knd) or ("보통" in knd)
        v = to_num(r.get("thstrm"))
        if "주당현금배당금" in se and common and out["dps"] is None:
            out["dps"] = v
        elif "현금배당수익률" in se and common and out["yield_pct"] is None:
            out["yield_pct"] = v
        elif "현금배당성향" in se and out["payout_pct"] is None and ("연결" in se or "(" not in se):
            out["payout_pct"] = v
        elif "현금배당금총액" in se and out["total_mn"] is None:
            out["total_mn"] = v
    return out


# ── 정기보고서 이름 해석 ──
_RPT = re.compile(r"(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")


def parse_report_nm(nm: str, acc_mt: str = "12") -> tuple[int, str, str] | None:
    """'[기재정정]분기보고서 (2025.09)' → (bsns_year, reprt_code, 정정구분). 12월 결산 기준 규칙.
    12월이 아닌 결산은 None (1단계에서는 해당 TTM 무효 — DESIGN 4.3)."""
    m = _RPT.search(nm or "")
    if not m:
        return None
    kind, y, mm = m.group(1), int(m.group(2)), m.group(3)
    amend = re.findall(r"\[([^\]]+)\]", nm.split(m.group(1))[0])
    tag = ",".join(amend) if amend else "최초"
    if acc_mt != "12":
        return None
    if kind == "사업보고서" and mm == "12":
        return y, "11011", tag
    if kind == "반기보고서" and mm == "06":
        return y, "11012", tag
    if kind == "분기보고서" and mm == "03":
        return y, "11013", tag
    if kind == "분기보고서" and mm == "09":
        return y, "11014", tag
    return None


def report_versions(filings: list[dict], acc_mt: str = "12") -> dict[tuple[int, str], list[dict]]:
    """(사업연도, 보고서코드) → 접수 순서대로 정렬된 버전 목록."""
    out: dict[tuple[int, str], list[dict]] = {}
    for f in filings:
        p = parse_report_nm(f.get("report_nm", ""), acc_mt)
        if not p:
            continue
        y, code, tag = p
        out.setdefault((y, code), []).append({"rcept_no": f["rcept_no"], "rcept_dt": f["rcept_dt"],
                                              "report_nm": f["report_nm"], "tag": tag})
    for v in out.values():
        v.sort(key=lambda x: x["rcept_no"])
    return out


def collect_company(d: DART, corp_code: str) -> dict:
    """한 회사의 기업개황·공시목록·재무(전 보고서)·정정 최초본 XBRL 수집."""
    comp = d.company(corp_code)
    acc_mt = (comp.get("acc_mt") or "12").zfill(2)
    fl = d.filings(corp_code, refresh=True)
    versions = report_versions(fl, acc_mt)
    res = {"corp_code": corp_code, "acc_mt": acc_mt, "reports": len(versions), "fs_ok": 0, "fs_none": 0,
           "amended": 0, "xbrl_ok": 0}
    if acc_mt != "12":
        # 12월 결산이 아니면 연도×보고서코드 전수 조회 (보고서명 해석 규칙이 다름)
        keys = [(y, c) for y in range(2015, today_kst().year + 1) for c in REPRT]
    else:
        keys = sorted(versions)
    for (y, code) in keys:
        fs_div, js = d.fs(corp_code, y, code)
        if fs_div:
            res["fs_ok"] += 1
        else:
            res["fs_none"] += 1
        if code in ("11011", "11012") and fs_div:
            a = d.audit(corp_code, y, code)
            if "audit_probe" not in res and a.get("list"):
                res["audit_probe"] = {"keys": sorted(a["list"][0].keys()), "first": {k: a["list"][0].get(k) for k in ("bsns_year", "adt_opinion")},
                                      "parsed": audit_opinion(a)}
        vs = versions.get((y, code), [])
        if len(vs) > 1:
            res["amended"] += 1
            info = d.xbrl(vs[0]["rcept_no"], code)
            res["xbrl_ok"] += int(bool(info.get("ok")))
    return res
