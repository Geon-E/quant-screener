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
WORKERS = 6             # 원문 한 건이 수 초~수십 초 (DART 응답 속도에 따라)
MAX_CHARS = 230

_ANNUAL = re.compile(r"사업보고서\s*\((\d{4})\.(\d{2})\)")


def annuals(filings: list[dict]) -> list[dict]:
    """사업보고서 목록, 최근 접수부터 (정정 포함)."""
    return sorted([f for f in filings or [] if _ANNUAL.search(f.get("report_nm", ""))], key=lambda f: f["rcept_no"], reverse=True)


def latest_annual(filings: list[dict]) -> dict | None:
    a = annuals(filings)
    return a[0] if a else None


def get_document(d: DART, filings: list[dict]) -> tuple[dict | None, bytes | None]:
    """최근 사업보고서부터 원문(zip)을 받을 수 있는 첫 건 (기재정정 원문이 없는 경우가 있어 최대 3건 시도)."""
    for f in annuals(filings)[:3]:
        r = d.http.get(f"{BASE}/document.xml", params={"crtfc_key": d.key, "rcept_no": f["rcept_no"]}, timeout=180)
        if r.content[:2] == b"PK":
            return f, r.content
        txt = r.content[:300].decode("utf-8", "ignore")
        if "<status>020" in txt or ('"020"' in txt):
            raise BudgetExceeded("DART 일일 호출 한도 초과(020)")
    return None, None


# ── 원문에서 개요 뽑기 ──
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[다요음함])\.\s*")       # '…있습니다.농기계…'처럼 띄어쓰기 없는 경계도 문장 끝으로
_SKIP_START = ("※", "주)", "주1", "주2", "(주1", "[", "<", "【", "(단위", "가.", "나.", "다.", "라.", "1)", "2)", "(1)", "(2)", "-", "▶", "■", "□", "○", "●")
_MARK = re.compile(r"^(?:[가-하]\.|\(?\d{1,2}\)|\d{1,2}\.|[①-⑳]|[■□▶○●◆◇▣\-])\s*")
_BOILER = ("기재를 생략", "참고하시기", "참조하시기", "작성기준일", "본 보고서", "이 보고서", "해당사항 없", "기재하지 않",
           "아래의 표", "아래 표", "다음과 같습니다", "아래와 같습니다", "다음 표", "하기 표")


def _decode(b: bytes) -> str:
    head = b[:200].decode("ascii", "ignore").lower()
    for enc in (("euc-kr" if "euc-kr" in head else "utf-8"), "utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "ignore")


_TITLE = re.compile(r"<TITLE[^>]*>(.*?)</TITLE>", re.S)


def _section(xml: str) -> str | None:
    """'사업의 개요' 제목 뒤부터 같은 단계의 다음 제목('2. …', 'III. …') 전까지의 원문.
    '1. (제조서비스업)사업의 개요'처럼 업종이 붙은 제목, 제목 안의 꾸밈 태그도 허용. 하위 제목('가. …')은 본문으로 둔다."""
    for m in _TITLE.finditer(xml):
        t = _WS.sub(" ", _TAG.sub("", m.group(1))).strip()
        if re.search(r"사업의\s*개요", t) and len(t) <= 40:
            rest = xml[m.end():]
            for n in _TITLE.finditer(rest):
                nt = _WS.sub(" ", _TAG.sub("", n.group(1))).strip()
                if re.match(r"^(\d+\.|[IVX]+\.)", nt):
                    return rest[: n.start()]
            return rest[:40000]
    return None


def _paragraphs(sec: str, tables: bool = False) -> list[str]:
    if not tables:
        sec = re.sub(r"<TABLE.*?</TABLE>", " ", sec, flags=re.S | re.I)      # 표는 버림 (본문이 없을 때만 표 안 글을 씀)
    sec = _TITLE.sub("</P>", sec)                                           # 하위 제목은 짧아서 걸러짐
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
    for p in paras[:20]:
        p = _MARK.sub("", p).strip()                     # 앞머리 번호·기호('가.', '(1)', '■')는 떼고 본문만
        if len(p) < 25 or p.startswith(_SKIP_START) or any(b in p for b in _BOILER):
            continue
        for s in _SENT.split(p):
            s = s.strip().strip("·-").strip()
            if len(s) < 20 or any(b in s for b in _BOILER) or s.startswith(_SKIP_START):
                continue
            if not s.endswith(("다", "요", "음", "함")):   # 문장으로 끝나지 않는 조각(표 숫자·제목)은 버림
                continue
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
                return summarize(_paragraphs(sec)) or summarize(_paragraphs(sec, tables=True))
    return None


# ── 수집 ──
def fetch_one(d: DART, corp_code: str) -> dict:
    path = dart_path("desc", f"{corp_code}.json.gz")
    filings = d.filings(corp_code)
    ann = latest_annual(filings)
    if not ann:
        return {"status": "no_annual"}
    if exists(path) and (load_l0(path)["payload"] or {}).get("latest") == ann["rcept_no"]:
        return {"status": "cached"}
    used, content = get_document(d, filings)
    if not content:
        return {"status": "no_document"}
    try:
        text = extract(content)
    except (zipfile.BadZipFile, KeyError):
        text = None
    ann = {**used, "latest": ann["rcept_no"]}
    save_l0(path, source={"provider": "DART", "api": "document.xml", "corp_code": corp_code, "rcept_no": ann["rcept_no"]},
            payload={"rcept_no": ann["rcept_no"], "latest": ann["latest"], "rcept_dt": ann.get("rcept_dt"),
                     "report_nm": ann.get("report_nm"), "text": text})
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
        t = time.time()
        ann, content = get_document(d, d.filings(c["corp_code"], refresh=True))
        if not content:
            log(f"{sc} {c.get('corp_name')}: 원문 없음")
            continue
        text = extract(content)
        log(f"{sc} {c.get('corp_name')} [{ann['report_nm']}] {len(content) // 1024}KB {time.time() - t:.1f}s → {(text or '(없음)')[:300]}")
        if not text:      # 진단: 개요 구간의 앞 문단들
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                for n in z.namelist():
                    sec = _section(_decode(z.read(n)))
                    if sec:
                        log(f"   구간 {len(sec)}자, 원문 앞부분: {_WS.sub(' ', sec[:700])}")
                        log(f"   문단: {[p[:80] for p in _paragraphs(sec)[:8]]}")
        out.append({"stock_code": sc, "name": c.get("corp_name"), "text": text})
    return out
