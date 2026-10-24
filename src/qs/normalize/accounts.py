"""계정 표준화: DART 재무 응답 1건(보고서 1개) → 표준 항목 값 + 출처·신뢰도 (config/account_rules.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml

from ..util import CONFIG, to_num
from .ttm import ytd_from_row

NONSTD = "-표준계정코드 미사용-"


def canon(aid: str | None) -> str:
    return re.sub(r"^ifrs(-full)?_", "ifrs:", aid or "")


@lru_cache(maxsize=1)
def rules() -> dict:
    with (CONFIG / "account_rules.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)["items"]


@dataclass
class Picked:
    value: float | None
    confidence: str          # id / pattern / ordinal / separate_only / zero_default / none
    sources: list            # [(account_id, account_nm)]


def _val(row: dict, kind: str) -> float | None:
    return ytd_from_row(row) if kind == "flow" else to_num(row.get("thstrm_amount"))


def _excluded(nm: str, rule: dict) -> bool:
    return any(re.search(p, nm or "") for p in rule.get("exclude_names", []))


def extract(rows: list[dict], fs_div: str) -> dict[str, Picked]:
    """rows: fnlttSinglAcntAll의 list (한 보고서). fs_div: CFS/OFS."""
    rs = sorted(rows, key=lambda r: (r.get("sj_div") or "", int(r.get("ord") or 0)))
    out: dict[str, Picked] = {}
    R = rules()

    def pick(name: str) -> Picked:
        rule = R[name]
        kind, sj = rule["kind"], set(rule["sj"])
        ids = set(rule.get("ids", []))
        cand = [r for r in rs if r.get("sj_div") in sj]
        mode = rule.get("mode", "first")
        conv = (lambda v: abs(v) if (v is not None and rule.get("abs")) else v)

        # 1) 계정 ID
        hits = [r for r in cand if canon(r.get("account_id")) in ids and not _excluded(r.get("account_nm"), rule)]
        if hits:
            if mode == "sum":
                seen, total, src = set(), 0.0, []
                for r in hits:
                    aid = canon(r.get("account_id"))
                    if aid in seen:
                        continue
                    v = _val(r, kind)
                    if v is None:
                        continue
                    seen.add(aid)
                    total += conv(v)
                    src.append((r.get("account_id"), r.get("account_nm")))
                if src:
                    # 비표준 행 중 이름이 맞는 것도 더한다 (ID와 비표준이 섞인 회사)
                    for r in cand:
                        if r.get("account_id") == NONSTD and any(re.search(p, r.get("account_nm") or "") for p in rule.get("names", [])) \
                                and not _excluded(r.get("account_nm"), rule):
                            v = _val(r, kind)
                            if v is not None:
                                total += conv(v)
                                src.append((r.get("account_id"), r.get("account_nm")))
                    return Picked(total, "id", src)
            else:
                for r in hits:
                    v = _val(r, kind)
                    if v is not None:
                        return Picked(conv(v), "id", [(r.get("account_id"), r.get("account_nm"))])

        # 2) 서수 규칙 (지배주주 순이익)
        if rule.get("ordinal_after"):
            base = R[rule["ordinal_after"]]
            idx = None
            for i, r in enumerate(cand):
                if canon(r.get("account_id")) in set(base.get("ids", [])) or \
                        (r.get("account_id") == NONSTD and any(re.search(p, r.get("account_nm") or "") for p in base.get("names", []))):
                    idx = i
                    break
            if idx is not None:
                for r in cand[idx + 1:]:
                    nm = r.get("account_nm") or ""
                    if any(s in nm or s in (r.get("account_id") or "") for s in rule.get("ordinal_stop", [])):
                        break
                    if any(re.search(p, nm) for p in rule.get("ordinal_names", [])) and "비지배" not in nm:
                        v = _val(r, kind)
                        if v is not None:
                            return Picked(v, "ordinal", [(r.get("account_id"), nm)])

        # 3) 비표준 계정명 패턴
        if rule.get("names"):
            pats = rule["names"]
            hits = [r for r in cand if r.get("account_id") == NONSTD and any(re.search(p, r.get("account_nm") or "") for p in pats)
                    and not _excluded(r.get("account_nm"), rule)]
            if hits:
                if mode == "sum":
                    vals = [(_val(r, kind), r) for r in hits]
                    vals = [(v, r) for v, r in vals if v is not None]
                    if vals:
                        return Picked(sum(conv(v) for v, _ in vals), "pattern",
                                      [(r.get("account_id"), r.get("account_nm")) for _, r in vals])
                else:
                    for r in hits:
                        v = _val(r, kind)
                        if v is not None:
                            return Picked(conv(v), "pattern", [(r.get("account_id"), r.get("account_nm"))])

        # 4) 별도재무제표만 있는 회사: 전체 = 지배주주
        if rule.get("separate_only_fallback") and fs_div == "OFS":
            base = out.get(rule["separate_only_fallback"]) or pick(rule["separate_only_fallback"])
            if base.value is not None:
                return Picked(base.value, "separate_only", base.sources)

        # 5) 연결인데 비지배지분 행이 전혀 없음 → 전체 = 지배주주
        if rule.get("no_nci_fallback") and fs_div == "CFS":
            nci = [r for r in cand if canon(r.get("account_id")) in set(rule.get("nci_ids", []))
                   or any(re.search(p, r.get("account_nm") or "") for p in rule.get("nci_names", []))]
            if not nci:
                base = out.get(rule["no_nci_fallback"]) or pick(rule["no_nci_fallback"])
                if base.value is not None:
                    return Picked(base.value, "no_nci", base.sources)

        if rule.get("missing_as_zero"):
            return Picked(0.0, "zero_default", [])
        return Picked(None, "none", [])

    for name in R:
        out[name] = pick(name)
    return out
