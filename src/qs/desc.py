"""회사 개요 B단계: 사업보고서 'II. 사업의 내용 > 1. 사업의 개요' 앞부분 2~3문장.

2026-11-01 사용자 결정: A(업종명·설립연도·홈페이지)는 바로, B는 과거 재무 수집이 끝난 뒤.
- 과거 재무 수집(L1 history_progress.complete)이 끝나기 전에는 아무 호출도 하지 않는다 (DART 일일 한도를 과거 재무에 양보).
- 회사마다 가장 최근 사업보고서 1건의 원문(document.xml, zip)을 받아 개요 문단만 뽑아 L0 dart/desc/<corp>.json.gz에 둔다.
  원문 zip은 저장하지 않는다 (크고, 다시 받을 수 있음).
- 같은 사업보고서(rcept_no)로 이미 뽑았으면 다시 받지 않는다 → 사실상 연 1회 갱신 (매년 3월 사업보고서가 나오면).
- 뽑지 못한 회사는 화면에서 업종명만 보인다 (A단계 그대로).
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from .collect.dart import BASE, DART
from .http import BudgetExceeded
from .storage import L1, dart_path, exists, load_l0, save_l0
from .util import elapsed_min, log, read_gz_json

UNTIL_MIN = 25          # 이 단계에 쓰는 최대 시간(분). 남은 회사는 다음 실행에서
LATE_MIN = 90           # 실행이 이미 이만큼 지났으면 이번에는 건너뜀 (뒤 단계·저장 시간 확보)
WORKERS = 4             # 원문은 커서 동시 요청을 적게
MAX_CHARS = 230

_ANNUAL = re.compile(r"사업보고서\s*\((\d{4})\.(\d{2})\)")


def latest_annual(filings: list[dict]) -> dict | None:
    """공시 목록에서 가장 최근 사업보고서(정정 포함 최신 접수) 한 건."""
    best = None
    for f in filings or []:
        if _ANNUAL.search(f.get("report_nm", "")) and (best is None or f["rcept_no"] > best["rcept_no"]):
            best = f
    return best


# ── 원문에서 개요 뽑기 ──
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[다요음함])\.\s+|(?<=[다요음함])\.$")
_SKIP_START = ("※", "주)", "(주1", "(단위", "가.", "나.", "다.", "라.", "1)", "2)", "(1)", "(2)", "-", "▶", "■", "□", "○", "●")
_BOILER = ("기재를 생략", "참고하시기", "참조하시기", "작성기준일", "본 보고서", "이 보고서", "해당사항 없", "기재하지 않")


def _decode(b: bytes) -> str:
    head = b[:200].decode("ascii", "ignore").lower()
    for enc in (("euc-kr" if "euc-kr" in head else "utf-8"), "utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "ignore")


def _section(xml: str) -> str | None:
    """'사업의 개요' 제목 뒤부터 다음 제목(TITLE) 전까지의 원문."""
    m = re.search(r"<TITLE[^>]*>\s*(?:\d+\.\s*)?사업의\s*개요\s*</TITLE>", xml)
    if not m:
        m = re.search(r"<TITLE[^>]*>[^<]{0,20}사업의\s*개요[^<]{0,20}</TITLE>", xml)
    if not m:
        return None
    rest = xml[m.end():]
    n = re.search(r"<TITLE[^>]*>", rest)
    return rest[: n.start()] if n else rest[:20000]


def _paragraphs(sec: str) -> list[str]:
    sec = re.sub(r"<TABLE.*?</TABLE>", " ", sec, flags=re.S | re.I)          # 표는 버림
    parts = re.split(r"</?(?:P|BR|SPAN-BR|TR)[^>]*>", sec, flags=re.I)
    out = []
    for p in parts:
        t = _WS.sub(" ", _TAG.sub(" ", p)).strip()
        t = t.replace("&cr;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
        t = _WS.sub(" ", t).strip()
        if t:
            out.append(t)
    return out


def summarize(paras: list[str], max_chars: int = MAX_CHARS) -> str | None:
    """앞쪽 문단에서 쓸 만한 문장 2~3개 (제목·주석·안내 문구 제외)."""
    picked: list[str] = []
    for p in paras[:12]:
        if len(p) < 25 or p.startswith(_SKIP_START) or any(b in p for b in _BOILER):
            continue
        for s in _SENT.split(p):
            s = s.strip().strip("·-").strip()
            if len(s) < 20 or any(b in s for b in _BOILER) or s.startswith(_SKIP_START):
                continue
            if s.endswith(("다", "요", "음", "함")):
                s += "."
            if sum(len(x) for x in picked) + len(s) > max_chars and picked:
                return " ".join(picked)
            picked.append(s)
            if len(picked) >= 3:
                return " ".join(picked)
    if not picked:
        return None
    text = " ".join(picked)
    return text if len(text) <= max_chars + 60 else text[: max_chars].rsplit(" ", 1)[0] + "…"


def extract(zip_bytes: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = sorted(z.namelist(), key=lambda n: (len(n), n))       # 본문 파일(접수번호.xml)이 보통 가장 짧은 이름
        for n in names:
            if not n.lower().endswith(".xml"):
                continue
            sec = _section(_decode(z.read(n)))
            if sec:
                return summarize(_paragraphs(sec))
    return None


# ── 수집 ──
def fetch_one(d: DART, corp_code: str) -> dict:
    path = dart_path("desc", f"{corp_code}.json.gz")
    ann = latest_annual(d.filings(corp_code))
    if not ann:
        return {"status": "no_annual"}
    if exists(path) and (load_l0(path)["payload"] or {}).get("rcept_no") == ann["rcept_no"]:
        return {"status": "cached"}
    r = d.http.get(f"{BASE}/document.xml", params={"crtfc_key": d.key, "rcept_no": ann["rcept_no"]}, timeout=180)
    if r.content[:2] != b"PK":
        txt = r.content[:300].decode("utf-8", "ignore")
        if "020" in txt and "한도" in txt or "<status>020" in txt:
            raise BudgetExceeded("DART 일일 호출 한도 초과(020)")
        return {"status": "no_document"}
    try:
        text = extract(r.content)
    except (zipfile.BadZipFile, KeyError):
        text = None
    save_l0(path, source={"provider": "DART", "api": "document.xml", "corp_code": corp_code, "rcept_no": ann["rcept_no"]},
            payload={"rcept_no": ann["rcept_no"], "rcept_dt": ann.get("rcept_dt"), "report_nm": ann.get("report_nm"),
                     "text": text})
    return {"status": "ok" if text else "not_found"}


def run(d: DART, force: bool = False, limit: int | None = None) -> dict:
    try:
        prog = read_gz_json(L1 / "history_progress.json.gz")
    except FileNotFoundError:
        prog = {}
    if not force and not prog.get("complete"):
        return {"skipped": "과거 재무 수집 중 (끝난 뒤 시작)", "history": f"{prog.get('processed')}/{prog.get('targets')}"}
    if elapsed_min() > LATE_MIN:
        return {"skipped": f"실행 {elapsed_min():.0f}분 경과, 다음 실행에서"}
    rows = read_gz_json(L1 / "universe.json.gz").get("rows", [])
    corps = [r["corp_code"] for r in rows if r.get("corp_code")][: limit or None]
    t0, stat, stopped = time.time(), {}, None

    def one(cc):
        if stopped or time.time() - t0 > UNTIL_MIN * 60 or elapsed_min() > LATE_MIN + UNTIL_MIN:
            return None
        try:
            return fetch_one(d, cc)
        except BudgetExceeded:
            raise
        except Exception as e:                       # 한 회사 실패가 전체를 멈추지 않게
            return {"status": "error", "err": str(e)[:80]}

    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(one, cc) for cc in corps]
        for f in futs:
            try:
                r = f.result()
            except BudgetExceeded as e:
                stopped = str(e)
                continue
            if r is None:
                stat["time_left"] = stat.get("time_left", 0) + 1
                continue
            stat[r["status"]] = stat.get(r["status"], 0) + 1
    have = sum(1 for cc in corps if exists(dart_path("desc", f"{cc}.json.gz")) and
               (load_l0(dart_path("desc", f"{cc}.json.gz"))["payload"] or {}).get("text"))
    res = {"targets": len(corps), "status": stat, "stopped": stopped, "with_text": have, **d.http.stats()}
    log(f"회사 개요(B): {res}")
    return res


def sample(d: DART, stock_codes: list[str]) -> list[dict]:
    """진단용: L0 없이 몇 회사만 받아 결과를 보여 준다 (저장하지 않음)."""
    t = time.time()
    corp = {c["stock_code"]: c for c in d.corpcode() if c.get("stock_code")}
    log(f"회사 코드 목록 {len(corp)}개 ({time.time() - t:.1f}s)")
    out = []
    for sc in stock_codes:
        c = corp.get(sc)
        if not c:
            continue
        ann = latest_annual(d.filings(c["corp_code"], refresh=True))
        if not ann:
            out.append({"stock_code": sc, "name": c.get("corp_name"), "text": None, "why": "사업보고서 없음"})
            continue
        t = time.time()
        r = d.http.get(f"{BASE}/document.xml", params={"crtfc_key": d.key, "rcept_no": ann["rcept_no"]}, timeout=180)
        text = extract(r.content) if r.content[:2] == b"PK" else None
        log(f"{sc} {c.get('corp_name')}: {len(r.content) // 1024}KB {time.time() - t:.1f}s → {(text or '(없음)')[:300]}")
        out.append({"stock_code": sc, "name": c.get("corp_name"), "report": ann["report_nm"], "kb": len(r.content) // 1024,
                    "text": text})
    return out
