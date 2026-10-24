"""KIND 관리종목 현재 목록 — 하루 1회 스냅샷 (4차 협의 Q1: D 구조의 '현재 목록 누적').

근거: config/sources.yaml (robots.txt 없음, 사용자 확인 2026-10-25). 저빈도·개인 연구용.
과거 이력이 아니라 '그날의 목록'만 쌓는다 — 과거 날짜에 소급 적용하지 않는다.
"""
from __future__ import annotations

import io

import requests
import yaml

from ..http import UA
from ..storage import L0, exists, load_l0, save_l0
from ..util import CONFIG, log, today_kst

URL = "https://kind.krx.co.kr/investwarn/adminissue.do?method=searchAdminIssueSub&currentPageSize=5000&forward=adminissue_down"


def allowed() -> bool:
    with (CONFIG / "sources.yaml").open(encoding="utf-8") as f:
        k = yaml.safe_load(f).get("kind", {})
    return k.get("robots_txt") == "absent" and "/investwarn/adminissue.do" in k.get("allowed_paths", [])


def snapshot() -> dict:
    path = L0 / "kind" / "admin" / f"{today_kst():%Y%m%d}.json.gz"
    if exists(path):
        rec = load_l0(path)
        return {"date": path.name[:8], "rows": len(rec["payload"]), "cached": True}
    if not allowed():
        return {"skipped": "config/sources.yaml에서 허용되지 않음"}
    try:
        r = requests.get(URL, headers=UA, timeout=60)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}"}
    text = r.content.decode("euc-kr", "replace")
    if "firewall" in text.lower() or "<table" not in text.lower():
        log("KIND: 방화벽 차단 또는 표 없음 — 오늘은 수집하지 않음(재시도 안 함)")
        return {"blocked": True, "http": r.status_code}
    import pandas as pd
    df = pd.read_html(io.StringIO(text), header=0)[0]
    rows = []
    for rec in df.astype(str).to_dict("records"):
        code = next((v for k, v in rec.items() if "코드" in str(k)), "")
        rows.append({"code": str(code).split(".")[0].zfill(6), "name": next((v for k, v in rec.items() if "종목" in str(k) or "회사" in str(k)), ""),
                     "designated": next((v for k, v in rec.items() if "지정일" in str(k)), ""),
                     "reason": next((v for k, v in rec.items() if "사유" in str(k)), "")})
    save_l0(path, source={"provider": "KIND", "page": "adminissue", "rows": len(rows), "http": r.status_code}, payload=rows)
    return {"date": path.name[:8], "rows": len(rows)}


def latest() -> tuple[str | None, set[str]]:
    d = L0 / "kind" / "admin"
    files = sorted(d.glob("*.json.gz")) if d.exists() else []
    if not files:
        return None, set()
    return files[-1].name[:8], {r["code"] for r in load_l0(files[-1])["payload"]}
