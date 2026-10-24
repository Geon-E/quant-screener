"""정정 전 최초본 XBRL → DART 재무 API와 같은 행 형식 (D-17).

목적: 재무 API는 최신 정정본만 돌려준다. 백테스트에서 '그 당시 알 수 있던 숫자'를 쓰려면
     정정 이력이 있는 보고서는 최초 제출본의 XBRL에서 값을 복원해야 한다.
방법: 행 형식을 API와 똑같이 만들어 계정 선택 규칙(normalize/accounts.extract)을 그대로 재사용한다.
  - 재무제표 구분: 표시 링크베이스 역할 코드 D2xxxx 재무상태표 / D3xxxx 손익 / D4xxxx 포괄손익 / D5xxxx 현금흐름,
    끝자리 0 = 연결, 5 = 별도
  - 계정 ID: 표시 링크베이스 로케이터의 조각(예: ifrs-full_Revenue). 회사 확장 계정은 '-표준계정코드 미사용-' + 한글 라벨
  - 기간: 보고서 기말일과 끝나는 컨텍스트. 손익은 3개월(thstrm_amount)과 누적(thstrm_add_amount), 현금흐름은 누적,
    재무상태표는 기말 시점
  - 연결/별도: ConsolidatedAndSeparateFinancialStatementsAxis 구성원. 그 축 외 다른 차원이 붙은 값은 쓰지 않는다
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile
from collections import defaultdict
from xml.etree import ElementTree as ET

NONSTD = "-표준계정코드 미사용-"
XL = "{http://www.w3.org/1999/xlink}"
SJ = {"2": "BS", "3": "IS", "4": "CIS", "5": "CF"}
Q_END = {"11013": (3, 31), "11012": (6, 30), "11014": (9, 30), "11011": (12, 31)}
Q_START = {"11013": (1, 1), "11012": (4, 1), "11014": (7, 1), "11011": (10, 1)}


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _find(names: list[str], suffix: str) -> str | None:
    c = [n for n in names if n.replace("\\", "/").lower().endswith(suffix) or re.search(suffix, n.replace("\\", "/").lower())]
    return c[0] if c else None


def parse_presentation(xml: bytes) -> dict[str, list[tuple[str, str]]]:
    """역할 → [(재무제표 구분, 개념 ID)] 표시 순서. 연결/별도 구분은 역할 코드 끝자리."""
    root = ET.fromstring(xml)
    out = {}
    for link in root.iter():
        if _local(link.tag) != "presentationLink":
            continue
        role = link.get(f"{XL}role", "")
        m = re.search(r"role-D(\d)(\d{4})(\d)$", role)
        if not m or m.group(1) not in SJ:
            continue
        loc, arcs = {}, defaultdict(list)
        for el in link:
            t = _local(el.tag)
            if t == "loc":
                loc[el.get(f"{XL}label")] = el.get(f"{XL}href", "").split("#")[-1]
            elif t == "presentationArc":
                arcs[el.get(f"{XL}from")].append((float(el.get("order") or 0), el.get(f"{XL}to")))
        tos = {to for v in arcs.values() for _, to in v}
        roots = [k for k in loc if k not in tos]
        seq: list[str] = []

        def walk(n):
            seq.append(loc.get(n, n))
            for _, ch in sorted(arcs.get(n, [])):
                walk(ch)
        for r in roots:
            walk(r)
        key = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        # 같은 역할이 여러 presentationLink로 나뉘어 있을 수 있음 → 이어 붙임 (덮어쓰면 '자산총계' 같은 행이 사라짐)
        have = {c for _, c in out.get(key, [])}
        out.setdefault(key, []).extend((SJ[m.group(1)], c) for c in seq if c not in have)
    return out


def parse_labels(xml: bytes | None) -> dict[str, str]:
    if not xml:
        return {}
    root = ET.fromstring(xml)
    loc, lab, arcs = {}, {}, []
    for el in root.iter():
        t = _local(el.tag)
        if t == "loc":
            loc[el.get(f"{XL}label")] = el.get(f"{XL}href", "").split("#")[-1]
        elif t == "label" and (el.get(f"{XL}role", "").endswith("/label")):
            lab[el.get(f"{XL}label")] = (el.text or "").strip()
        elif t == "labelArc":
            arcs.append((el.get(f"{XL}from"), el.get(f"{XL}to")))
    out = {}
    for f, t in arcs:
        if f in loc and t in lab and lab[t]:
            out.setdefault(loc[f], lab[t])
    return out


def parse_instance(xml: bytes) -> tuple[dict, list[tuple[str, str, str]]]:
    """(컨텍스트 id → {start, end, instant, dims}, [(개념 ID, contextRef, 값)])."""
    root = ET.fromstring(xml)
    ns_prefix = {}
    # 네임스페이스 URI → 접두어 (개념 ID를 'ifrs-full_Revenue' 형태로 만들기 위해)
    for k, v in re.findall(rb'xmlns:([A-Za-z0-9_\-]+)="([^"]+)"', xml[:20000]):
        ns_prefix[v.decode()] = k.decode()
    ctx = {}
    facts = []
    for el in root:
        t = _local(el.tag)
        if t == "context":
            c = {"dims": {}}
            for sub in el.iter():
                st = _local(sub.tag)
                if st in ("startDate", "endDate", "instant"):
                    c[st] = (sub.text or "").strip()
                elif st == "explicitMember":
                    c["dims"][sub.get("dimension", "").split(":")[-1]] = (sub.text or "").strip().split(":")[-1]
            ctx[el.get("id")] = c
        elif el.get("contextRef") is not None:
            uri = el.tag[1:].split("}")[0] if el.tag.startswith("{") else ""
            pref = ns_prefix.get(uri, "")
            facts.append((f"{pref}_{_local(el.tag)}", el.get("contextRef"), (el.text or "").strip()))
    return ctx, facts


def to_rows(zip_path, bsns_year: int, reprt: str, fs_div: str) -> list[dict]:
    """XBRL 패키지 → fnlttSinglAcntAll 행 형식 리스트 (해당 보고서 기간·연결/별도)."""
    z = zipfile.ZipFile(zip_path)
    names = z.namelist()
    inst = next(n for n in names if n.endswith(".xbrl"))
    pre = _find(names, "_pre.xml") or _find(names, r"(^|/)pre_[^/]*\.xml$")
    lab = _find(names, "lab-ko.xml") or _find(names, r"lab_[^/]*-ko_[^/]*\.xml$")
    pres = parse_presentation(z.read(pre)) if pre else {}
    labels = parse_labels(z.read(lab) if lab else None)
    ctx, facts = parse_instance(z.read(inst))

    m_end, d_end = Q_END[reprt]
    end = f"{bsns_year}-{m_end:02d}-{d_end:02d}"
    ytd_start = f"{bsns_year}-01-01"
    qs_m, qs_d = Q_START[reprt]
    q_start = f"{bsns_year}-{qs_m:02d}-{qs_d:02d}"
    want = "ConsolidatedMember" if fs_div == "CFS" else "SeparateMember"
    axis = "ConsolidatedAndSeparateFinancialStatementsAxis"
    any_axis = any(axis in c["dims"] for c in ctx.values())

    def ok_ctx(c):
        dims = c["dims"]
        if any_axis:
            return set(dims) == {axis} and dims[axis] == want
        return not dims                      # 축 자체가 없는 신고서(단일 재무제표) — 그 회사의 유일한 재무제표
    # 개념 → {kind: 값}
    val: dict[str, dict[str, float]] = defaultdict(dict)
    for concept, cref, raw in facts:
        c = ctx.get(cref)
        if not c or not ok_ctx(c):
            continue
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if c.get("instant") == end:
            val[concept]["instant"] = v
        elif c.get("endDate") == end:
            if c.get("startDate") == ytd_start:
                val[concept]["ytd"] = v
            if c.get("startDate") == q_start:
                val[concept]["q"] = v

    suffix = "0" if fs_div == "CFS" else "5"
    rows = []
    for key, seq in sorted(pres.items()):
        if key[-1] != suffix:
            continue
        for ord_, (sj, concept) in enumerate(seq):
            v = val.get(concept)
            if not v:
                continue
            if sj == "BS":
                amt, add = v.get("instant"), None
            elif sj == "CF":
                amt, add = v.get("ytd"), None
            else:   # IS/CIS: 3개월 + 누적 (1분기·사업보고서는 누적 하나)
                if reprt in ("11013", "11011"):
                    amt, add = v.get("ytd", v.get("q")), None
                else:
                    amt, add = v.get("q"), v.get("ytd")
            if amt is None and add is None:
                continue
            std = re.match(r"^(ifrs-full|ifrs|dart)_", concept)
            rows.append({"sj_div": sj, "ord": str(ord_), "account_id": concept if std else NONSTD,
                         "account_nm": labels.get(concept) or concept.split("_", 1)[-1],
                         "thstrm_amount": None if amt is None else str(amt),
                         "thstrm_add_amount": None if add is None else str(add), "source": "xbrl"})
    # 표시 링크베이스에 없는 표준 개념 (구형 패키지는 회사 확장분만 표시 링크베이스에 담음 — 2026-10-25 실측)
    #   시점 값 → 재무상태표, 기간 값 → 손익·포괄손익·현금흐름 후보로. 표준 계정 ID는 의미가 하나라 규칙 매칭에 문제 없음
    covered = {(r["sj_div"], r["account_id"]) for r in rows}
    base = len(rows)
    for k, (concept, v) in enumerate(sorted(val.items())):
        if not re.match(r"^(ifrs-full|ifrs|dart)_", concept):
            continue
        targets = ["BS"] if "instant" in v else ["IS", "CIS", "CF"]
        for sj in targets:
            if (sj, concept) in covered:
                continue
            if sj == "BS":
                amt, add = v.get("instant"), None
            elif sj == "CF" or reprt in ("11013", "11011"):
                amt, add = v.get("ytd", v.get("q")), None
            else:
                amt, add = v.get("q"), v.get("ytd")
            if amt is None and add is None:
                continue
            rows.append({"sj_div": sj, "ord": str(base + k), "account_id": concept,
                         "account_nm": labels.get(concept) or concept.split("_", 1)[-1],
                         "thstrm_amount": None if amt is None else str(amt),
                         "thstrm_add_amount": None if add is None else str(add), "source": "xbrl-unpresented"})
    return rows
