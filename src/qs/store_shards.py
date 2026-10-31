"""데이터 보관소(GitHub Release 'data-store') 안전 저장·복원.

2026-10-30 사고: l0.tar.gz 하나에 모든 원천 데이터를 담아 `--clobber`로 올렸다.
과거 재무가 쌓여 파일이 GitHub 릴리스 파일 한도(2 GiB)를 넘자 업로드가 실패했고,
--clobber가 먼저 지운 기존 파일까지 사라졌다.

새 방식
  - 원천(L0)·파생(L1·L2)을 여러 조각(shard)으로 나눈다. 조각마다 1.8 GB를 넘으면 저장 전에 멈춘다.
  - 세대 방식: 새 조각을 '세대 번호'를 붙인 새 이름으로 모두 올린 뒤, 목차(index-<종류>.json)를 마지막에 바꾸고,
    그다음에만 이전 세대 조각을 지운다. 어느 단계에서 실패해도 이전 세대는 그대로 남는다.
  - 복원은 목차에 적힌 조각만 받는다. 목차가 없으면 예전 단일 파일(l0.tar.gz / derived.tar.gz)을 찾는다.

명령
  python -m qs.store_shards save  --kind l0      --src data/l0  --gen <번호>
  python -m qs.store_shards save  --kind derived --src data     --layers l1 l2 --gen <번호>
  python -m qs.store_shards restore --kind l0 --dst data
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

TAG = "data-store"
MAX_BYTES = 1_800_000_000      # GitHub 릴리스 파일 한도 2 GiB보다 여유 있게
DART_BUCKETS = 16


def shard_key(rel: str, kind: str) -> str:
    """파일 상대경로 → 조각 이름. dart는 회사·날짜 폴더 단위로 16조각, krx는 API별, 나머지는 misc."""
    parts = rel.split("/")
    if kind == "l0":
        if parts[0] == "dart" and len(parts) >= 3:
            return f"dart-{zlib.crc32(parts[2].encode()) % DART_BUCKETS:02d}"
        if parts[0] == "krx" and len(parts) >= 2:
            return f"krx-{parts[1]}"
        return "misc"
    if kind == "us":   # 미국 일별 시세: 연도별 조각
        return f"{parts[0]}-{parts[1][:4]}" if len(parts) >= 2 else "misc"
    # derived: l1·l2 각각 하나씩 (작음)
    return parts[0]


def plan(src: Path, kind: str, layers: list[str] | None = None) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    roots = [src / x for x in layers] if layers else [src]
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(src).as_posix()
                groups.setdefault(shard_key(rel, kind), []).append(rel)
    return groups


def pack(src: Path, out: Path, kind: str, gen: str, layers: list[str] | None = None) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    shards = []
    for key, files in sorted(plan(src, kind, layers).items()):
        name = f"{kind}-{key}-g{gen}.tar.gz"
        path = out / name
        with tarfile.open(path, "w:gz") as tf:
            for rel in files:
                tf.add(src / rel, arcname=rel)
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise SystemExit(f"조각 {name} 크기 {size:,}B가 한도 {MAX_BYTES:,}B를 넘음 — 조각 수를 늘려야 함 (아무것도 지우지 않음)")
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        shards.append({"name": name, "files": len(files), "bytes": size, "sha256": h})
    return shards


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])} 실패: {r.stderr.strip()[:500]}")
    return r.stdout


REPO = os.environ.get("GITHUB_REPOSITORY", "Geon-E/quant-screener")


def _release() -> dict:
    return json.loads(gh("api", f"repos/{REPO}/releases/tags/{TAG}"))


def assets() -> dict[str, int]:
    return {a["name"]: a["id"] for a in _release().get("assets", [])}


# 이진 파일 올리기·내려받기는 urllib로 직접 (gh release 하위 명령은 일부 환경에서 GraphQL을 써서 막힘)
def _token() -> str:
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t:
        raise SystemExit("GH_TOKEN 없음 — 워크플로 안에서 실행해야 함")
    return t


def upload(path: Path, name: str) -> None:
    import urllib.request
    rid = _release()["id"]
    old = assets().get(name)
    if old:                                  # 같은 이름은 같은 세대를 다시 올릴 때만 — 덮어쓰기
        gh("api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{old}")
    req = urllib.request.Request(f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets?name={name}",
                                 data=path.read_bytes(), method="POST",
                                 headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/octet-stream",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        if r.status not in (200, 201):
            raise SystemExit(f"{name} 업로드 실패 HTTP {r.status}")


def delete(name: str) -> None:
    aid = assets().get(name)
    if aid:
        gh("api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{aid}", check=False)


class _NoRedirect(__import__("urllib.request").request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def download(name: str, d: Path) -> Path:
    """자산 API → 서명된 저장소 주소로 넘어감. 넘어간 주소에는 인증 머리글을 붙이지 않는다."""
    import shutil
    import urllib.error
    import urllib.request
    aid = assets()[name]
    out = d / name
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/assets/{aid}",
                                 headers={"Authorization": f"Bearer {_token()}", "Accept": "application/octet-stream"})
    try:
        resp = urllib.request.build_opener(_NoRedirect).open(req, timeout=600)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        resp = urllib.request.urlopen(e.headers["Location"], timeout=1800)
    with resp, out.open("wb") as f:
        shutil.copyfileobj(resp, f, length=1 << 20)
    return out


def read_index(kind: str, workdir: Path) -> dict | None:
    if f"index-{kind}.json" not in assets():
        return None
    return json.loads(download(f"index-{kind}.json", workdir).read_text(encoding="utf-8"))


def save(kind: str, src: Path, gen: str, layers: list[str] | None = None) -> dict:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        shards = pack(src, td / "out", kind, gen, layers)
        if not shards:
            print(f"{kind}: 저장할 파일 없음 — 건너뜀")
            return {"shards": 0}
        old = read_index(kind, td)
        # 줄어듦 방지: 복원이 덜 된 채 저장하면 좋은 세대를 덮어쓸 수 있으므로, 파일 수가 이전 세대의 95% 미만이면 멈춘다
        if old and kind == "l0":
            before, after = sum(x["files"] for x in old["shards"]), sum(x["files"] for x in shards)
            if after < before * 0.95:
                raise SystemExit(f"파일 수 감소 {before:,} → {after:,} — 복원 실패 가능성, 저장 중단 (이전 세대 유지)")
        # 1) 새 세대 조각 전부 올리기 (이름이 새로워 기존 파일을 건드리지 않음)
        for s in shards:
            upload(td / "out" / s["name"], s["name"])
        # 2) 올라간 크기 확인
        now = assets()
        missing = [s["name"] for s in shards if s["name"] not in now]
        if missing:
            raise SystemExit(f"업로드 확인 실패 {missing} — 목차를 바꾸지 않음 (이전 세대 유지)")
        # 3) 목차 교체 (작은 파일 — 실패하면 이전 목차·이전 세대가 그대로 남음)
        idx = {"kind": kind, "gen": gen, "shards": shards}
        ip = td / f"index-{kind}.json"
        ip.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
        upload(ip, ip.name)
        # 4) 이전 세대 조각 지우기 (목차가 바뀐 뒤에만)
        keep = {s["name"] for s in shards}
        stale = [n for n in now if n.startswith(f"{kind}-") and n.endswith(".tar.gz") and n not in keep]
        for n in stale:
            delete(n)
        total = sum(s["bytes"] for s in shards)
        print(f"{kind}: 세대 {gen} 저장 — 조각 {len(shards)}개, {total / 1e6:,.0f} MB, 파일 {sum(s['files'] for s in shards):,}개"
              f" (이전 세대 {old.get('gen') if old else '없음'}, 지운 조각 {len(stale)})")
        return {"shards": len(shards), "bytes": total, "deleted": len(stale)}


def restore(kind: str, dst: Path, legacy: str) -> dict:
    dst.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        idx = read_index(kind, td)
        if idx is None:
            if legacy in assets():
                download(legacy, td)
                with tarfile.open(td / legacy) as tf:   # 예전 l0.tar.gz는 'l0/...' 경로로 묶여 있음
                    tf.extractall(dst.parent if kind == "l0" else dst, filter="data")
                print(f"{kind}: 예전 단일 파일 {legacy} 복원")
                return {"legacy": True}
            print(f"{kind}: 보관본 없음 — 처음부터")
            return {"empty": True}
        n = 0
        for s in idx["shards"]:
            p = download(s["name"], td)
            if hashlib.sha256(p.read_bytes()).hexdigest() != s["sha256"]:
                raise SystemExit(f"조각 {s['name']} 해시 불일치 — 복원 중단")
            with tarfile.open(p) as tf:
                tf.extractall(dst, filter="data")
            n += s["files"]
            p.unlink()
        print(f"{kind}: 세대 {idx['gen']} 복원 — 조각 {len(idx['shards'])}개, 파일 {n:,}개")
        return {"gen": idx["gen"], "files": n}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["save", "restore", "plan"])
    ap.add_argument("--kind", choices=["l0", "derived", "us"], required=True)
    ap.add_argument("--src", default="data/l0")
    ap.add_argument("--dst", default="data")
    ap.add_argument("--layers", nargs="*")
    ap.add_argument("--gen", default="0")
    a = ap.parse_args(argv)
    if a.action == "plan":
        for k, v in sorted(plan(Path(a.src), a.kind, a.layers).items()):
            print(k, len(v))
    elif a.action == "save":
        save(a.kind, Path(a.src), a.gen, a.layers)
    else:
        dst = Path(a.dst) / "l0" if a.kind == "l0" and not str(a.dst).endswith("l0") else Path(a.dst)
        if a.kind == "us":
            dst = Path(a.dst) / "l0" / "us"
        # l0 조각은 'dart/...','krx/...' 상대경로 → data/l0 아래로, derived는 'l1/...','l2/...' → data 아래로
        restore(a.kind, dst, {"l0": "l0.tar.gz", "derived": "derived.tar.gz"}.get(a.kind, "-"))


if __name__ == "__main__":
    sys.exit(main())
