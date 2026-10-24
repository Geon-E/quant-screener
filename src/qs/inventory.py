"""계정 인벤토리: 표본 기업들의 실제 DART 계정 ID·계정명 분포를 집계한다.
목적: 계정 표준화 규칙(config/account_rules.yaml)을 추측이 아니라 실측 분포를 보고 작성하기 위함.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict

from .storage import L0, load_l0
from .util import REPORTS, log

# 규칙 작성 시 커버리지를 확인할 관심 항목 (계정명 패턴은 탐색용)
PROBES = {
    "매출액": r"^(매출액|수익\(매출액\)|영업수익|매출)$",
    "매출총이익": r"매출총이익",
    "영업이익": r"^영업이익|영업손실|영업이익\(손실\)",
    "당기순이익": r"(당기|분기|반기)순이익",
    "지배주주순이익": r"지배기업.*(소유|주주)",
    "감가상각비": r"감가상각",
    "무형자산상각비": r"(무형자산)?상각비",
    "이자비용": r"이자비용|이자의 지급|금융비용|금융원가",
    "영업활동현금흐름": r"영업활동.*현금흐름",
    "현금성자산": r"현금및현금성자산",
    "단기금융상품": r"단기금융",
    "차입금": r"차입금",
    "사채": r"사채",
    "리스부채": r"리스부채",
    "유동자산": r"^유동자산",
    "유동부채": r"^유동부채",
}


def run() -> dict:
    import re
    fs_dir = L0 / "dart" / "fs"
    acc = Counter()
    names = defaultdict(Counter)
    corp_has = defaultdict(set)
    corps = set()
    files = 0
    for p in fs_dir.rglob("*.json.gz"):
        js = load_l0(p)["payload"]
        rows = js.get("list") or []
        if not rows:
            continue
        files += 1
        cc = p.parent.name
        corps.add(cc)
        for r in rows:
            key = (r.get("sj_div"), r.get("account_id"))
            acc[key] += 1
            names[key][r.get("account_nm")] += 1
            for probe, pat in PROBES.items():
                if re.search(pat, r.get("account_nm") or ""):
                    corp_has[(probe, r.get("sj_div"), r.get("account_id"))].add(cc)

    out = REPORTS / "phase1"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "account_inventory.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sj_div", "account_id", "rows", "top_names"])
        for (sj, aid), n in acc.most_common():
            w.writerow([sj, aid, n, " | ".join(f"{k}({v})" for k, v in names[(sj, aid)].most_common(4))])
    with (out / "account_probe.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["probe", "sj_div", "account_id", "companies", "coverage"])
        for (probe, sj, aid), cs in sorted(corp_has.items(), key=lambda x: (x[0][0], -len(x[1]))):
            w.writerow([probe, sj, aid, len(cs), f"{len(cs) / max(len(corps), 1):.0%}"])
    res = {"fs_files": files, "companies": len(corps), "distinct_accounts": len(acc)}
    log(f"계정 인벤토리: {res}")
    return res
