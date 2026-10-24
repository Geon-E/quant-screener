"""1단계 검증 표본 선정 (RESULT.md 완료 기준 1).

층화 규칙 (결정적 — 같은 데이터면 같은 표본):
  고정      : 대형주·지주·기업행위 사례 (이름으로 지정)
  정정      : 0단계 실측에서 정정공시가 확인된 기업 (reports/phase0/phase0_raw.json)
  데이터 기반: 후보군을 종목코드 해시 순으로 훑어 할당량을 채움
               적자 / 12월 외 결산 / 비표준 계정 / 코스닥 소형 / 상장폐지
금융업(KSIC 64~66)은 D-06에 따라 제외.
선정 결과는 config/sample_v1.csv에 고정 저장 — 이후 실행은 파일을 그대로 사용 (재선정은 --reselect).
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .collect.dart import DART
from .collect.krx import BASE_INFO_APIS
from .http import BudgetExceeded
from .storage import L0, load_l0
from .util import CONFIG, REPORTS, log, to_num

SAMPLE_FILE = CONFIG / "sample_v1.csv"
FIELDS = ["stock_code", "corp_code", "name", "market", "category", "reason"]

FIXED = [
    ("005930", "대형", "대형주 · 액면분할(2018) 기업행위"),
    ("000660", "대형", "대형주"),
    ("005380", "대형", "대형주 · 우선주 다수(시총 합산 검증)"),
    ("035420", "대형", "대형주"),
    ("051910", "대형", "대형주 · 우선주"),
    ("035720", "기업행위", "액면분할(2021)"),
    ("003550", "지주", "지주회사 · 지배/비지배 구분"),
    ("034730", "지주", "지주회사 · 지배/비지배 차이 큼"),
]
QUOTA = {"적자": 4, "12월외결산": 2, "비표준계정": 3, "코스닥소형": 4, "상장폐지": 2}
MAX_POOL = 400


def _latest_rows(api: str):
    d = L0 / "krx" / api.replace("/", "_")
    for p in sorted(d.glob("*.json.gz"), reverse=True):
        rec = load_l0(p)
        if rec["meta"].get("rows"):
            return p.name[:8], rec["payload"]
    return None, []


def _earliest_rows(api: str):
    d = L0 / "krx" / api.replace("/", "_")
    for p in sorted(d.glob("*.json.gz")):
        rec = load_l0(p)
        if rec["meta"].get("rows"):
            return p.name[:8], rec["payload"]
    return None, []


def _hash_order(code: str) -> str:
    return hashlib.sha256(f"sample_v1:{code}".encode()).hexdigest()


def _is_financial(induty: str) -> bool:
    return (induty or "")[:2] in ("64", "65", "66")


def load_sample() -> list[dict]:
    if not SAMPLE_FILE.exists():
        return []
    with SAMPLE_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def select(d: DART, reselect: bool = False) -> list[dict]:
    if SAMPLE_FILE.exists() and not reselect:
        s = load_sample()
        log(f"표본 고정 파일 사용: {len(s)}개사")
        return s

    corp = d.corpcode()
    by_stock = {c["stock_code"]: c for c in corp if c["stock_code"]}
    by_name = {}
    for c in corp:
        by_name.setdefault(c["corp_name"], c)

    day, kospi = _latest_rows("sto/stk_bydd_trd")
    _, kosdaq = _latest_rows("sto/ksq_bydd_trd")
    _, base_k = _latest_rows(BASE_INFO_APIS[0])
    _, base_q = _latest_rows(BASE_INFO_APIS[1])
    if not kospi:
        raise RuntimeError("KRX L0 시세가 없습니다. krx-backfill을 먼저 실행하세요.")
    price = {r["ISU_CD"]: r for r in kospi + kosdaq}
    base = {r["ISU_SRT_CD"]: r for r in base_k + base_q}

    chosen: list[dict] = []
    taken: set[str] = set()

    def add(stock, category, reason, name=None, market=None, corp_code=None):
        if stock in taken:
            return False
        c = by_stock.get(stock) or (by_name.get(name) if name else None)
        if not c and not corp_code:
            log(f"표본 제외(고유번호 없음): {stock} {name}")
            return False
        p = price.get(stock, {})
        chosen.append({"stock_code": stock, "corp_code": corp_code or c["corp_code"],
                       "name": name or (c or {}).get("corp_name") or p.get("ISU_NM", ""),
                       "market": market or p.get("MKT_NM", ""), "category": category, "reason": reason})
        taken.add(stock)
        return True

    for stock, cat, reason in FIXED:
        add(stock, cat, reason)

    # 정정공시 기업 (0단계)
    raw = REPORTS / "phase0" / "phase0_raw.json"
    if raw.exists():
        amend = json.loads(raw.read_text(encoding="utf-8")).get("dart_amendment", {}).get("samples", [])
        for s in amend[:5]:
            add(s["stock_code"], "정정공시", f"2024 사업보고서 버전 {len(s['versions'])}개", name=s.get("corp_name"))

    # 상장폐지: 백필 시작일에는 있었으나 최신일에 없는 보통주
    _, early = _earliest_rows("sto/stk_bydd_trd")
    _, early_q = _earliest_rows("sto/ksq_bydd_trd")
    gone = sorted({r["ISU_CD"] for r in early + early_q} - set(price), key=_hash_order)
    early_map = {r["ISU_CD"]: r for r in early + early_q}
    n_del = 0
    for code in gone:
        if n_del >= QUOTA["상장폐지"]:
            break
        if not code.endswith("0"):
            continue  # 우선주 제외
        r = early_map[code]
        if add(code, "상장폐지", "백필 기간 중 상장폐지(시세 소멸)", name=r.get("ISU_NM"), market=r.get("MKT_NM")):
            n_del += 1

    # 데이터 기반 후보군
    pool = [code for code, b in base.items()
            if b.get("KIND_STKCERT_TP_NM") == "보통주" and b.get("SECUGRP_NM") == "주권" and code in by_stock]
    pool.sort(key=_hash_order)
    got = {k: 0 for k in QUOTA if k != "상장폐지"}
    scanned = 0
    try:
        for code in pool:
            if all(got[k] >= QUOTA[k] for k in got) or scanned >= MAX_POOL:
                break
            if code in taken:
                continue
            scanned += 1
            cc = by_stock[code]["corp_code"]
            comp = d.company(cc)
            if _is_financial(comp.get("induty_code", "")):
                continue
            acc = (comp.get("acc_mt") or "12").zfill(2)
            if acc != "12":
                if got["12월외결산"] < QUOTA["12월외결산"] and add(code, "12월외결산", f"결산월 {acc}"):
                    got["12월외결산"] += 1
                continue
            need_fs = any(got[k] < QUOTA[k] for k in ("적자", "비표준계정", "코스닥소형"))
            if not need_fs:
                continue
            fs_div, js = d.fs(cc, 2024, "11011")
            rows = (js or {}).get("list") or [] if fs_div else []
            if not rows:
                continue
            ni = next((to_num(r.get("thstrm_amount")) for r in rows
                       if r.get("account_id") == "ifrs-full_ProfitLoss" and r.get("sj_div") in ("IS", "CIS")), None)
            nonstd = sum(1 for r in rows if not str(r.get("account_id", "")).startswith(("ifrs-full_", "dart_")))
            mcap = to_num(price.get(code, {}).get("MKTCAP")) or 0
            mkt = price.get(code, {}).get("MKT_NM", "")
            if ni is not None and ni < 0 and got["적자"] < QUOTA["적자"]:
                if add(code, "적자", f"2024 당기순손실 {ni / 1e8:,.0f}억원 ({fs_div})"):
                    got["적자"] += 1
            elif nonstd / max(len(rows), 1) > 0.3 and got["비표준계정"] < QUOTA["비표준계정"]:
                if add(code, "비표준계정", f"비표준 계정 비율 {nonstd / len(rows):.0%}"):
                    got["비표준계정"] += 1
            elif mkt == "KOSDAQ" and 0 < mcap < 1e11 and got["코스닥소형"] < QUOTA["코스닥소형"]:
                if add(code, "코스닥소형", f"시총 {mcap / 1e8:,.0f}억원"):
                    got["코스닥소형"] += 1
    except BudgetExceeded as e:
        log(f"표본 선정 중 예산 소진: {e}")
    log(f"표본 선정: 후보 {scanned}개 조사, 할당 {got}, 총 {len(chosen)}개사 (기준일 {day})")

    SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_FILE.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(chosen)
    return chosen
