"""저장 계층 (DESIGN 2.1).

L0 원본은 '추가만' 한다: 같은 경로가 이미 있으면 다시 받지 않는다 (재개 가능성).
원격 보관(GitHub Release 자산)과의 동기화는 워크플로가 tar로 처리하고,
여기서는 로컬 경로 규칙·매니페스트·보안 검사만 담당한다 → 저장소 위치를 바꿔도 이 인터페이스는 유지.
"""
from __future__ import annotations

import json
from pathlib import Path

from .util import DATA, canonical_json, now_kst, read_gz_json, secrets, sha256_bytes, write_gz_json

L0 = DATA / "l0"
L1 = DATA / "l1"
L2 = DATA / "l2"


# ── L0 경로 규칙 ──
def krx_path(api: str, basDd: str) -> Path:
    return L0 / "krx" / api.replace("/", "_") / f"{basDd}.json.gz"


def dart_path(kind: str, *parts: str) -> Path:
    return L0 / "dart" / kind / Path(*parts)


def exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def save_l0(path: Path, *, source: dict, payload, collected_at: str | None = None) -> str:
    """L0 레코드 = {meta, payload}. meta에 수집 시각과 원본 해시를 남긴다."""
    body_hash = sha256_bytes(canonical_json(payload))
    rec = {"meta": {**source, "collected_at": collected_at or now_kst().isoformat(), "payload_sha256": body_hash},
           "payload": payload}
    write_gz_json(path, rec)
    return body_hash


def load_l0(path: Path):
    return read_gz_json(path)


# ── 매니페스트 ──
def build_manifest(root: Path) -> dict:
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith(".tmp") and p.name != "MANIFEST.json":
            files[str(p.relative_to(root))] = {"bytes": p.stat().st_size, "sha256": sha256_bytes(p.read_bytes())}
    return {"root": str(root.name), "generated_at": now_kst().isoformat(), "count": len(files),
            "total_bytes": sum(v["bytes"] for v in files.values()), "files": files}


def write_manifest(root: Path) -> Path:
    m = build_manifest(root)
    out = root / "MANIFEST.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=0), encoding="utf-8")
    return out


def verify_manifest(root: Path) -> list[str]:
    """매니페스트와 실제 파일 대조 → 불일치 목록."""
    mf = root / "MANIFEST.json"
    if not mf.exists():
        return ["MANIFEST.json 없음"]
    m = json.loads(mf.read_text(encoding="utf-8"))
    bad = []
    for rel, info in m["files"].items():
        p = root / rel
        if not p.exists():
            bad.append(f"누락: {rel}")
        elif sha256_bytes(p.read_bytes()) != info["sha256"]:
            bad.append(f"해시 불일치: {rel}")
    return bad


# ── 보안 검사 (완료 기준 8) ──
def scan_for_secrets(*roots: Path) -> list[str]:
    """결과물·로그에 API 키 문자열이 있으면 경로를 반환 (gzip은 풀어서 검사)."""
    import gzip
    keys = [k.encode() for k in secrets()]
    if not keys:
        return []
    hits = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            b = p.read_bytes()
            if p.suffix == ".gz":
                try:
                    b = gzip.decompress(b)
                except OSError:
                    pass
            if any(k in b for k in keys):
                hits.append(str(p))
    return hits
