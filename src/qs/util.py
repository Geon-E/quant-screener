"""공통 유틸: 시간(KST), 해시, gzip JSON 입출력, 키 제거, 로그."""
from __future__ import annotations

import datetime as dt
import time as _time
import gzip
import hashlib
import json
import os
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(os.environ.get("QS_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CONFIG = ROOT / "config"

_LOG: list[str] = []


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def today_kst() -> dt.date:
    return now_kst().date()


def secrets() -> list[str]:
    return [v for v in (os.environ.get("KRX_API_KEY", "").strip(), os.environ.get("DART_API_KEY", "").strip()) if v]


def scrub(x):
    """API 키 문자열을 제거한다 (문자열·dict·list 재귀)."""
    if isinstance(x, str):
        for k in secrets():
            x = x.replace(k, "***")
        return x
    if isinstance(x, dict):
        return {scrub(k): scrub(v) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    return x


def log(msg: str) -> None:
    line = f"[{now_kst():%H:%M:%S}] {scrub(str(msg))}"
    print(line, flush=True)
    _LOG.append(line)


def log_lines() -> list[str]:
    return list(_LOG)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_json(obj) -> bytes:
    """결정적 직렬화 (키 정렬, 공백 고정) — 해시·결정성 검증용."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def write_gz_json(path: Path, obj) -> str:
    """gzip JSON 저장. mtime=0으로 고정해 같은 내용이면 같은 바이트가 되게 한다. 반환: 내용 해시."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(obj)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as raw:
        # filename=""·mtime=0: gzip 헤더에 파일명·시각이 들어가지 않게 해 같은 내용 = 같은 바이트 (결정성)
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(data)
    tmp.replace(path)
    return sha256_bytes(data)


def read_gz_json(path: Path):
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(text), encoding="utf-8")


def to_num(v):
    """'1,234' · '-' · '' · None → float|None. DART·KRX 숫자 문자열 공통 처리."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A", "nan", "None"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


# ── 실행 시간 예산 (워크플로 제한 안에서 저장 단계까지 끝나도록 수집 단계들이 나눠 쓴다) ──
PROCESS_T0 = _time.monotonic()


def elapsed_min() -> float:
    return (_time.monotonic() - PROCESS_T0) / 60
