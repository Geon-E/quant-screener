"""스크리너 화면 생성: 점수 결과 → 데이터 내장 단일 HTML (reports/screen/screener.html) + screen.json.

화면 원칙 (DESIGN 5·6장)
  - 점수는 '가설', 과거 성과 미검증 — 상단 고정 표시
  - 무효 지표는 빈칸(0·50점으로 채우지 않음), 제외 종목은 사유와 함께 따로 볼 수 있게
  - 사업군 이름·표본 수·신뢰도를 항상 함께 표시
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from .screen import primary_exclusion
from .util import write_text

SHOW = ["E/P", "B/P", "EBIT/EV", "PBR-ROE잔차", "ROE", "GP/A", "CFO/NI", "12-1모멘텀", "영업이익증가율", "이자보상배율"]


def _r(v, n=4):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(v, n)


def compact(r: dict) -> dict:
    m = r.get("metrics") or {}
    ep, bp = m.get("E/P"), m.get("B/P")
    conf = r.get("confidence_P") or {}
    fallback = sorted({f"{k}:{v}" for k, v in conf.items() if v not in ("id", "zero_default", "id_blank", "none")
                       and k in ("ni_owners", "equity_owners", "op_income", "revenue", "debt", "cash")})
    px = primary_exclusion(r.get("exclude") or [])
    pm = (r.get("periods_meta") or {}).get(r.get("period") or "") or {}
    mr = r.get("metric_reasons") or {}
    return {
        "c": r["stock_code"], "n": r.get("name"), "mk": r.get("market"), "g": r.get("group_name"), "gid": r.get("group_id"),
        "gn": r.get("group_n"), "rel": r.get("reliability"), "rk": r.get("rank"), "grk": r.get("group_rank"),
        "sc": r.get("composite"), "b": [(r.get("buckets") or {}).get(k) for k in ("value", "quality", "trend", "safety")],
        "x": r.get("exclude") or [], "w": r.get("warn") or [],
        "mc": _r(r.get("mcap"), 0), "px": r.get("close"), "tv": _r(r.get("tv20"), 0),
        "per": _r(1 / ep, 2) if ep and ep > 0 else None, "pbr": _r(1 / bp, 2) if bp and bp > 0 else None,
        "m": {k: _r(m.get(k)) for k in SHOW}, "p": {k: (r.get("pct") or {}).get(k) for k in SHOW},
        "pd": r.get("period"), "fs": r.get("fs_div"), "fsc": _r(r.get("fscore"), 1), "ksic": r.get("induty_code"),
        "fb": fallback, "ib": r.get("interest_basis"),
        "xp": px[0] if px else None, "xpf": px[1] if px else None,
        "fd": pm.get("rcept_dt_first"), "av": pm.get("available_date"),
        "mr": {k: v for k, v in mr.items() if k in SHOW},
        "prk": r.get("prev_rank"), "pk": r.get("prev_known"),
        "msc": _r(r.get("mkt_composite"), 1), "mrk": r.get("mkt_rank"),
        "dv": ({k: (_r(v, 3) if isinstance(v, float) else v) for k, v in r["dividend"].items()} if r.get("dividend") else None),
        "fg": r.get("fin_group"), "fs2": r.get("fin_score"), "frk": r.get("fin_rank"), "fp": r.get("fin_pct"),
        "fx": r.get("fin_other_ex"),
        "co": r.get("company"),
        "rk2": r.get("rank_v2"), "tg": r.get("truth") or [],
        "tq": ({"ni": _r((r.get("ttm") or {}).get("ni_total"), 0), "cfo": _r((r.get("ttm") or {}).get("cfo"), 0),
                "op": _r((r.get("ttm") or {}).get("op_income"), 0), "acc": _r(r.get("accrual")), "acc10": _r(r.get("accrual_top10"))}
               if r.get("status") == "ok" else None),
    }


def group_medians(rows: list[dict]) -> dict:
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get("sc") is None and r.get("gn") is None:
            continue
        for k, v in r["m"].items():
            if v is not None:
                by[r["gid"]][k].append(v)
    out = {}
    for g, d in by.items():
        out[g] = {}
        for k, vs in d.items():
            vs = sorted(vs)
            n = len(vs)
            out[g][k] = [vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2, n]   # [중앙값, 유효 표본 수]
    return out


def build_view(results: list[dict], summary: dict, out_dir: Path, compact_fn=None, site_name: str = "index.html",
               keep_reports: bool = True) -> None:
    rows = [(compact_fn or compact)(r) for r in results]
    rows.sort(key=lambda x: (x["rk"] is None, x["rk"] or 0, -(x["mc"] or 0)))
    data = {"summary": summary, "rows": rows, "med": group_medians([x for x in rows if not x["x"]])}
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = TEMPLATE.replace("/*__DATA__*/null", js.replace("</", "<\\/"))
    if keep_reports:
        write_text(out_dir / "screen.json", json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        write_text(out_dir / "screener.html", page)
    # 배포용 완전한 문서: 저장소의 site/index.html → Vercel이 GitHub 연동으로 자동 배포 (다른 앱들과 같은 방식, 토큰 불필요)
    write_text(out_dir.parent.parent / "site" / site_name,
               '<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
               '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
               '<meta name="robots" content="noindex">\n</head>\n<body>\n' + page + '\n</body>\n</html>\n')


TEMPLATE = r"""<title>업종 대비 저평가 후보</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="fonts/pretendard/pretendardvariable-dynamic-subset.css">
<style>
/* 화면 2판 (2026-11-20, 6차 협의 Q1 + Codex 레퍼런스 1안): 조사 도구 중심
   구조: 윗줄(제목·기준일·링크) → 찾기(검색·사업군) → 관심 종목(가로 넘김) → 탭 → 목록(PC 표, 휴대폰 카드) → 상세 시트(섹션 바로 가기)
   글꼴: Pretendard 한 가족, 본문 15px, 숫자 tabular-nums. 말투: 합쇼체
   모서리: 큰 틀 16px · 안쪽 칸 10px · 알약 999px. 겹침: 윗줄 20 · 표 머리 10 · 덮개 40 · 시트 50 */
:root{
  --paper:#f3f4f6; --surface:#fefefe; --raised:#f7f8fa; --ink:#191d22; --ink2:#3b424b; --muted:#5d6670; --faint:#7a838d; --line:#e3e6ea; --soft:#eef0f3;
  --accent:#0e7a62; --accent-ink:#f6fbf9; --accent-soft:#e1f2ec;
  --up:#d6342c; --down:#2563c9; --warn:#8f5a00; --warn-soft:#fbf1dc; --warn-line:#efdcae; --bad:#b4282e; --bad-soft:#fbe7e6;
  --star:#d39a00;
  --shadow:0 20px 48px rgba(20,26,33,.18); --lift:0 1px 2px rgba(20,26,33,.05);
  --f-body:"Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;
  --r-lg:16px; --r-md:10px;
  --ease-out:cubic-bezier(0.23,1,0.32,1);
  --z-sticky:10; --z-top:20; --z-scrim:40; --z-sheet:50;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0f1215; --surface:#171b20; --raised:#1d2228; --ink:#e7eaee; --ink2:#c9ced5; --muted:#a2aab4; --faint:#868f99; --line:#2a3038; --soft:#21262d;
  --accent:#4cc3a3; --accent-ink:#0f1215; --accent-soft:#16302a;
  --up:#f0706a; --down:#6c9ef2; --warn:#e3b356; --warn-soft:#2b2412; --warn-line:#4b3e1a; --bad:#f08a86; --bad-soft:#3a1d1e; --star:#e9b73c;
  --shadow:0 20px 48px rgba(0,0,0,.55); --lift:none; color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#0f1215; --surface:#171b20; --raised:#1d2228; --ink:#e7eaee; --ink2:#c9ced5; --muted:#a2aab4; --faint:#868f99; --line:#2a3038; --soft:#21262d;
  --accent:#4cc3a3; --accent-ink:#0f1215; --accent-soft:#16302a;
  --up:#f0706a; --down:#6c9ef2; --warn:#e3b356; --warn-soft:#2b2412; --warn-line:#4b3e1a; --bad:#f08a86; --bad-soft:#3a1d1e; --star:#e9b73c;
  --shadow:0 20px 48px rgba(0,0,0,.55); --lift:none; color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--f-body);font-size:15px;line-height:1.6;font-variant-numeric:tabular-nums;word-break:keep-all;overflow-wrap:break-word;-webkit-font-smoothing:antialiased}
[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{font:inherit;color:inherit;-webkit-tap-highlight-color:transparent}
a{color:var(--accent);text-underline-offset:3px}
.press{transition:transform 120ms var(--ease-out),background-color 120ms var(--ease-out),border-color 120ms var(--ease-out),color 120ms var(--ease-out)}
.press:active{transform:scale(.97)}
.wrap>*{min-width:0}
.wrap{max-width:1120px;margin:0 auto;padding:0 max(16px,env(safe-area-inset-right,0px)) 64px max(16px,env(safe-area-inset-left,0px));display:grid;gap:20px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* 윗줄 */
.topbar{position:sticky;top:0;z-index:var(--z-top);background:color-mix(in srgb,var(--paper) 92%,transparent);backdrop-filter:saturate(1.4) blur(10px);-webkit-backdrop-filter:saturate(1.4) blur(10px);margin:0 calc(-1*max(16px,env(safe-area-inset-left,0px)));padding:10px max(16px,env(safe-area-inset-left,0px))}
.topbar .in{max-width:1120px;margin:0 auto;display:flex;align-items:center;gap:8px 14px;flex-wrap:wrap}
h1{font-size:19px;font-weight:700;line-height:1.3;margin:0;letter-spacing:-.02em}
.topbar nav{display:flex;gap:4px;margin-left:auto}
.topbar nav a{font-size:14px;font-weight:500;color:var(--ink2);text-decoration:none;padding:6px 10px;border-radius:var(--r-md);min-height:36px;display:inline-flex;align-items:center}
.topbar nav a:hover{background:var(--soft);color:var(--ink)}
.head{display:grid;gap:8px;padding-top:6px}
.lede{color:var(--muted);font-size:14.5px;margin:0;max-width:62ch}
.state{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;font-size:13.5px;color:var(--muted)}
.state b{color:var(--ink);font-weight:600}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12.5px}
.pill.ok{background:var(--accent-soft);color:var(--accent)}
.pill.wait{background:var(--warn-soft);color:var(--warn)}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor}
.caution{font-size:13.5px;color:var(--ink2);line-height:1.6}
.caution strong{color:var(--warn);font-weight:600}
.caution details{display:inline}
.caution summary{display:inline;cursor:pointer;color:var(--accent);text-decoration:underline;text-underline-offset:3px;list-style:none}
.caution summary::-webkit-details-marker{display:none}
.caution details[open]{display:block;margin-top:8px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);padding:12px 14px}
.caution details[open] summary{display:none}
.caution .more{display:block;margin-top:6px;color:var(--ink2)}
.caution .more:first-of-type{margin-top:0}

/* 찾기 */
.finder{display:grid;gap:12px;grid-template-columns:minmax(0,1fr)}
.finder>*,.hsec>*,#pane-home>*,#pane-top>*{min-width:0}
.searchbox{position:relative}
.searchbox svg{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--faint);pointer-events:none}
.finder input[type=search]{font:inherit;font-size:16.5px;color:var(--ink);background:var(--surface);border:1.5px solid var(--line);border-radius:var(--r-lg);padding:14px 16px 14px 46px;min-height:56px;width:100%;box-shadow:var(--lift);transition:border-color 120ms var(--ease-out)}
.finder input[type=search]::placeholder{color:var(--faint)}
.finder input[type=search]:hover{border-color:var(--faint)}
.finder input[type=search]:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.finder input[type=search]:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.chipwrap{position:relative}
.chipwrap::after{content:"";position:absolute;top:0;right:-1px;bottom:0;width:40px;background:linear-gradient(90deg,transparent,var(--paper));pointer-events:none}
.gchips{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;padding-right:40px}
.gchips::-webkit-scrollbar{display:none}
.gchips button{flex:none;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:6px 14px;min-height:38px;font-size:14px;font-weight:500;color:var(--ink2);cursor:pointer}
.gchips button:hover{border-color:var(--faint);color:var(--ink)}
.gchips button[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:var(--surface)}
.gchips small{font-size:12.5px;color:inherit;opacity:.65;margin-left:4px;font-weight:400}

/* 관심 종목: 가로 넘김 */
.watchbox{display:grid;gap:10px}
.sec-h{display:flex;align-items:baseline;gap:8px;margin:0}
.sec-h h2{font-size:17px;font-weight:700;margin:0;letter-spacing:-.01em}
.sec-h .cnt{color:var(--faint);font-size:14px}
.wcards{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(220px,260px);gap:10px;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}
.wcards::-webkit-scrollbar{display:none}
.wcard{display:grid;gap:8px;text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;cursor:pointer;box-shadow:var(--lift)}
.wcard:hover{border-color:var(--faint)}
.wcard .top{display:flex;justify-content:space-between;align-items:center;gap:8px;min-width:0}
.wcard .top b{font-size:16px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wcard .g{font-size:13px;color:var(--muted);margin-top:-6px}
.wcard .kv3{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
.wcard .kv3 span{font-size:12.5px;color:var(--muted);display:grid}
.wcard .kv3 b{font-size:15px;color:var(--ink);font-weight:600}
.wcard .neg{color:var(--warn);font-size:12.5px;font-weight:600}
.empty-note{color:var(--muted);font-size:14px}

/* 탭 */
.tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--line)}
.tabs::-webkit-scrollbar{display:none}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;margin-bottom:-1px;padding:10px 12px;min-height:46px;color:var(--muted);cursor:pointer;font-size:15px;font-weight:500;white-space:nowrap;flex:none}
.tabs button:hover{color:var(--ink)}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--ink);font-weight:700}
.tabs .cnt{font-size:13px;color:var(--faint);margin-left:4px;font-weight:500}

/* 목록 조작 */
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.controls select{font:inherit;font-size:14px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);padding:8px 12px;min-height:42px;min-width:0}
.controls select:hover{border-color:var(--faint)}
.controls input[type=checkbox]{accent-color:var(--accent);width:17px;height:17px;margin:0}
.controls label{display:flex;gap:7px;align-items:center;color:var(--ink2);font-size:14px;cursor:pointer;min-height:42px}
.ghost{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);padding:8px 14px;min-height:42px;color:var(--ink2);cursor:pointer;font-size:14px;font-weight:500}
.ghost:hover{border-color:var(--faint);color:var(--ink)}
.count{margin-left:auto;color:var(--muted);font-size:14px}

/* 업종 안 위치 알약 */
.qp{display:inline-block;font-size:12.5px;font-weight:600;padding:3px 10px;border-radius:999px;white-space:nowrap;line-height:1.4}
.qp.q1{background:var(--accent);color:var(--accent-ink)} .qp.q2{background:var(--accent-soft);color:var(--accent)}
.qp.q3{background:var(--soft);color:var(--ink2)} .qp.q4{background:var(--warn-soft);color:var(--warn)} .qp.q5{background:var(--bad-soft);color:var(--bad)}
.qp.qx{background:none;color:var(--faint);font-weight:500;padding:3px 0}

/* 목록: PC 표 */
.list{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden;box-shadow:var(--lift)}
.grid{display:grid;grid-template-columns:36px minmax(200px,1.6fr) 104px 64px 64px 70px 80px 92px 92px;align-items:center;column-gap:12px;padding:0 18px}
.hrow{background:var(--raised);font-size:13px;color:var(--muted);font-weight:600;min-height:44px;line-height:1.3;border-bottom:1px solid var(--line)}
.hrow .u{font-weight:400;color:var(--faint)}
.hrow button{background:none;border:0;padding:6px 0;cursor:pointer;color:inherit;font-weight:inherit;text-align:inherit}
.hrow button:hover{color:var(--ink)}
.hrow button[aria-sort]{color:var(--ink)}
.hrow button[aria-sort="ascending"]::after{content:" ▲";font-size:9px}
.hrow button[aria-sort="descending"]::after{content:" ▼";font-size:9px}
.r{text-align:right} .c{text-align:center}
.row{min-height:64px;border-top:1px solid var(--soft);cursor:pointer;background:none;width:100%;border-left:0;border-right:0;border-bottom:0;text-align:left;font-size:15px;transition:background-color 120ms var(--ease-out)}
.row:first-child{border-top:0}
.row:hover{background:var(--raised)}
.row:focus-visible{outline-offset:-2px}
.row.sel{background:var(--accent-soft)}
.nm{min-width:0}
.nm .n{font-weight:600;font-size:15.5px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm .cd{display:block;color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm .cd small{color:var(--faint);font-size:13px}
.nm .cd small.low{color:var(--warn)}
.star{background:none;border:0;padding:0;width:36px;height:36px;display:grid;place-items:center;border-radius:var(--r-md);cursor:pointer;font-size:18px;line-height:1;color:var(--line);transition:transform 120ms var(--ease-out),color 120ms var(--ease-out)}
.star:hover{color:var(--faint)}
.star:active{transform:scale(.88)}
.star.on{color:var(--star)}
.up{color:var(--up)} .down{color:var(--down)} .dash{color:var(--faint)}
.chip{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle;font-size:12.5px;padding:2px 9px;border-radius:999px;background:var(--warn-soft);color:var(--warn);font-weight:500}
.chip.x,.chip.tg{background:var(--bad-soft);color:var(--bad)}
.g-nums{display:none}
.more{justify-self:center;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:10px 24px;min-height:46px;color:var(--ink);font-weight:600;cursor:pointer;font-size:14.5px}
.more:hover{border-color:var(--faint)}
.emptybox{padding:40px 20px;color:var(--muted);text-align:center;font-size:14.5px}
.emptybox b{display:block;color:var(--ink);margin-bottom:6px;font-size:16px}
.vnote{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:13.5px;color:var(--muted);margin:2px 0 10px}

/* 사업군·배당·금융 표 */
table.gt{border-collapse:collapse;width:100%;font-size:14.5px}
.gt th,.gt td{padding:12px 14px;border-bottom:1px solid var(--soft);text-align:right;white-space:nowrap}
.gt th{background:var(--raised);color:var(--muted);font-weight:600;font-size:13px;border-bottom:1px solid var(--line)}
.gt th .u{font-weight:400;color:var(--faint)}
.gt .l{text-align:left}
.gt tbody tr{transition:background-color 120ms var(--ease-out)}
.gt tbody tr:hover{background:var(--raised)}
.gt .toplink{background:none;border:0;padding:0;cursor:pointer;font-weight:600;color:var(--ink);text-align:left}
.gt .toplink:hover{color:var(--accent)}
.gt .low{color:var(--warn)}
.gwrap{overflow-x:auto}
.gwrap table{min-width:760px}
.pane-note{color:var(--muted);font-size:14px;margin:0 0 12px;max-width:80ch}
.etfbox{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:16px 18px}
.etfbox h2{font-size:17px;font-weight:700;margin:0 0 10px}
.etfbox table{min-width:560px}
.gt th[data-dk]{cursor:pointer}
.gt th[aria-sort="descending"]::after{content:" ▼";font-size:9px}
.gt th[aria-sort="ascending"]::after{content:" ▲";font-size:9px}
.trend{display:inline-flex;align-items:flex-end;gap:2px;height:20px;vertical-align:middle;border-bottom:1px solid var(--line);padding:0 1px}
.trend i{display:block;width:8px;background:var(--accent);border-radius:2px 2px 0 0}
.trend i.z{background:none;height:0!important}
.gt .nmcell{font-weight:600}
.gt .nmcell small{display:block;color:var(--muted);font-weight:400;font-size:12.5px}

/* 상세 시트 */
.scrim{position:fixed;inset:0;background:rgba(12,16,20,.36);z-index:var(--z-scrim);animation:fade 200ms var(--ease-out)}
@keyframes fade{from{opacity:0}}
.sheet{position:fixed;z-index:var(--z-sheet);background:var(--surface);box-shadow:var(--shadow);display:flex;flex-direction:column;
  top:0;right:0;bottom:0;width:min(620px,100vw);border-left:1px solid var(--line);transform:translateX(0);transition:transform 240ms var(--ease-out)}
.sheet.closed{transform:translateX(104%)}
.sh-head{display:flex;align-items:flex-start;gap:10px;padding:18px 20px 12px;flex:none}
.sh-head .t{flex:1;min-width:0}
.sh-head h3{font-size:23px;font-weight:700;margin:0;line-height:1.3;letter-spacing:-.02em}
.sh-head .cd{color:var(--muted);font-size:14px;margin-top:2px}
.sh-head .big{display:grid;justify-items:end;gap:2px}
.sh-head .big small{font-size:12px;color:var(--faint)}
.sh-head .big .qp{font-size:14px;padding:5px 12px}
.sh-act{display:flex;gap:6px;flex:none}
.close,.sh-star{background:var(--soft);border:0;border-radius:999px;width:40px;height:40px;cursor:pointer;font-size:19px;line-height:1;color:var(--ink2);display:grid;place-items:center}
.close:hover,.sh-star:hover{background:var(--line)}
.sh-star.on{color:var(--star)}
.sh-nav{flex:none;display:flex;gap:6px;padding:0 20px 12px;border-bottom:1px solid var(--line);overflow-x:auto;scrollbar-width:none}
.sh-nav::-webkit-scrollbar{display:none}
.sh-nav button{flex:none;background:none;border:1px solid var(--line);border-radius:999px;padding:5px 13px;min-height:36px;font-size:13.5px;font-weight:500;color:var(--ink2);cursor:pointer}
.sh-nav button:hover{border-color:var(--faint)}
.sh-nav button[aria-current="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
.sh-body{overflow-y:auto;padding:16px 20px 36px;display:grid;gap:28px;overscroll-behavior:contain;scroll-behavior:smooth}
.sh-sec{display:grid;gap:12px;scroll-margin-top:8px}
.sh-sec>h4{font-size:17px;font-weight:700;color:var(--ink);margin:0;letter-spacing:-.01em}
.sh-body h5{font-size:14px;font-weight:600;color:var(--muted);margin:0 0 8px}
.kgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.kgrid div{background:var(--raised);border-radius:var(--r-md);padding:12px}
.kgrid span{display:block;font-size:13px;color:var(--muted)}
.kgrid b{display:block;font-size:20px;font-weight:700;line-height:1.3;margin-top:2px}
.kgrid small{display:block;font-size:12.5px;color:var(--faint);margin-top:2px}
.why{display:grid;gap:4px;font-size:15px;line-height:1.65;color:var(--ink2)}
.why b{color:var(--ink)}
.why .pos{color:var(--accent);font-weight:600} .why .neg{color:var(--warn);font-weight:600}
.why .note{color:var(--muted);font-size:13.5px}
.bk{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.bk div{background:var(--raised);border-radius:var(--r-md);padding:10px 12px}
.bk span{display:block;font-size:13px;color:var(--muted)}
.bk b{font-size:18px}
.bk .bar{height:6px;border-radius:3px;background:var(--line);margin-top:6px;position:relative;overflow:hidden}
.bk .bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:3px}
.strip{display:block;width:132px;height:22px}
.strip .base{stroke:var(--line)} .strip .tick{stroke:var(--faint);opacity:.55} .strip .me{fill:var(--accent);stroke:var(--surface);stroke-width:2}
.strip .me.muted{fill:var(--faint)}
.strip .end{font-size:12px;fill:var(--muted);font-family:var(--f-body);font-variant-numeric:tabular-nums}
table.mt{border-collapse:collapse;width:100%;font-size:14px}
.mt th,.mt td{padding:9px 6px;border-bottom:1px solid var(--soft);text-align:right}
.mt th{color:var(--muted);font-weight:600;font-size:13px}
.mt .l{text-align:left}
.mt small{color:var(--faint);font-size:12px;margin-left:4px}
.pc{display:inline-block;width:56px;height:6px;border-radius:3px;background:var(--line);vertical-align:middle;margin-left:8px;position:relative}
.pc i{position:absolute;inset:0 auto 0 0;border-radius:3px;background:var(--accent)}
.truth{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.truth li{display:flex;gap:12px;align-items:flex-start;padding:12px;border-radius:var(--r-md);background:var(--raised)}
.truth li .ic{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:700}
.truth li.ok .ic{background:var(--accent);color:var(--accent-ink)} .truth li.bad .ic{background:var(--bad);color:var(--surface)} .truth li.na .ic{background:var(--line);color:var(--muted)}
.truth li b{display:block;font-weight:600;font-size:14.5px} .truth li small{display:block;color:var(--muted);font-size:13px;margin-top:2px}
h5 .pos{color:var(--accent)} h5 .neg{color:var(--bad)}
.note{color:var(--muted);font-size:13.5px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:14px;margin:0}
.kv dt{color:var(--muted)} .kv dd{margin:0}
.lst{margin:0;padding-left:18px;display:grid;gap:4px;font-size:14px}
.about{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;font-size:14px;color:var(--muted)}
.about .ind{color:var(--ink);font-weight:600}
.about i{font-style:normal;color:var(--faint)}
.desc{margin:0;font-size:14.5px;line-height:1.7;color:var(--ink2);max-width:62ch}
.desc small{display:block;margin-top:4px;font-size:12.5px;color:var(--faint)}
.sh-body details summary{cursor:pointer;color:var(--muted);font-size:14px}
footer{color:var(--muted);font-size:13px;display:grid;gap:4px;max-width:90ch;padding-top:8px;border-top:1px solid var(--line)}

/* 메뉴: PC 왼쪽, 휴대폰 아래 */
.shell{display:grid;grid-template-columns:232px minmax(0,1fr);max-width:1400px;margin:0 auto}
.shell>.wrap{max-width:1120px;width:100%;margin:0}
.side{position:sticky;top:0;height:100vh;height:100dvh;padding:20px 14px;display:flex;flex-direction:column;gap:2px;border-right:1px solid var(--line)}
.side .brand{font-size:18px;font-weight:700;letter-spacing:-.02em;padding:4px 12px 18px;line-height:1.3}
.side .brand small{display:block;font-size:13px;font-weight:500;color:var(--muted)}
.side button{display:flex;align-items:center;gap:12px;width:100%;padding:10px 12px;min-height:44px;border-radius:var(--r-md);background:none;border:0;font-size:15px;font-weight:500;color:var(--ink2);cursor:pointer;text-align:left}
.side button:hover{background:var(--soft);color:var(--ink)}
.side button[aria-current="page"]{background:var(--surface);color:var(--ink);font-weight:700;box-shadow:var(--lift)}
.side button[aria-current="page"] svg{color:var(--accent)}
.side svg,.bottom svg{width:20px;height:20px;flex:none}
.side .cnt{margin-left:auto;font-size:12.5px;color:var(--faint);font-weight:500}
.side-foot{margin-top:auto;display:grid;gap:2px;padding-top:12px;border-top:1px solid var(--line)}
.side-foot a{font-size:14px;color:var(--muted);text-decoration:none;padding:8px 12px;border-radius:var(--r-md)}
.side-foot a:hover{background:var(--soft);color:var(--ink)}
.bottom{display:none}
.topbar nav{display:none}

/* 홈 */
#pane-home{display:grid;gap:28px}
#pane-home[hidden]{display:none}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;box-shadow:var(--lift)}
.stat span{display:block;font-size:13.5px;color:var(--muted)}
.stat b{display:block;font-size:24px;font-weight:700;letter-spacing:-.01em;line-height:1.35}
.stat small{display:block;font-size:12.5px;color:var(--faint)}
.hsec{display:grid;gap:12px}
.sec-h .go{margin-left:auto;background:none;border:0;color:var(--accent);font-size:14px;font-weight:600;cursor:pointer;padding:6px 0}
.mini5{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--lift);overflow:hidden}
.mini5 li+li{border-top:1px solid var(--soft)}
.mini5 button{display:grid;grid-template-columns:28px minmax(0,1fr) auto auto;gap:12px;align-items:center;width:100%;background:none;border:0;padding:12px 16px;text-align:left;cursor:pointer;min-height:58px}
.mini5 button:hover{background:var(--raised)}
.mini5 .no{font-size:17px;font-weight:700;color:var(--accent);text-align:center}
.mini5 .t b{display:block;font-size:15.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mini5 .t small{display:block;font-size:13px;color:var(--muted)}
.mini5 .s{font-size:16px;font-weight:700}
.chgcols{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.chgbox{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;box-shadow:var(--lift)}
.chgbox h3{font-size:14px;font-weight:600;color:var(--muted);margin:0 0 8px}
.chgbox ul{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.chgbox li button{background:none;border:0;padding:6px 0;cursor:pointer;font-size:15px;font-weight:500;display:flex;justify-content:space-between;width:100%;text-align:left}
.chgbox li button:hover{color:var(--accent)}
.chg{font-size:13px;font-weight:600}
.chg.up{color:var(--up)} .chg.down{color:var(--down)} .chg.new{color:var(--accent)} .chg.flat{color:var(--faint)}
.gcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.gcard{display:grid;gap:4px;text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;cursor:pointer;box-shadow:var(--lift)}
.gcard:hover{border-color:var(--faint)}
.gcard b{font-size:15.5px}
.gcard small{font-size:13px;color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.tile{display:flex;align-items:center;gap:12px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;min-height:64px;cursor:pointer;font-size:15px;font-weight:600;color:var(--ink);text-decoration:none;box-shadow:var(--lift);text-align:left}
.tile:hover{border-color:var(--faint)}
.tile svg{width:22px;height:22px;color:var(--accent);flex:none}
.tile small{display:block;font-size:12.5px;font-weight:400;color:var(--muted)}

/* TOP 20 */
#pane-top{display:grid;gap:16px}
#pane-top[hidden]{display:none}
.topnote{background:var(--warn-soft);border:1px solid var(--warn-line);border-radius:var(--r-lg);padding:14px 16px;font-size:14px;line-height:1.65;color:var(--ink2)}
.topnote b{color:var(--warn)}
.seg{display:inline-flex;background:var(--soft);border-radius:999px;padding:4px;gap:2px;justify-self:start}
.seg button{background:none;border:0;border-radius:999px;padding:8px 16px;min-height:40px;font-size:14px;font-weight:600;color:var(--muted);cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--surface);color:var(--ink);box-shadow:var(--lift)}
.toplist{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.tcard{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:4px 12px;align-items:center;width:100%;text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;cursor:pointer;box-shadow:var(--lift)}
.tcard:hover{border-color:var(--faint)}
.tcard .no{grid-row:1/3;font-size:24px;font-weight:800;color:var(--ink);text-align:center;line-height:1}
.tcard .no small{display:block;margin-top:4px}
.tcard.p1 .no,.tcard.p2 .no,.tcard.p3 .no{color:var(--accent)}
.tcard .t b{display:block;font-size:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tcard .t small{display:block;font-size:13px;color:var(--muted)}
.tcard .s{font-size:20px;font-weight:700;text-align:right;line-height:1.2}
.tcard .s small{display:block;font-size:12px;font-weight:500;color:var(--faint)}
.tcard .k{grid-column:2/4;display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px;color:var(--muted)}
.tcard .k b{color:var(--ink);font-weight:600}
.tcard .k .pos{color:var(--accent);font-weight:600}

/* 휴대폰: 표 → 카드 */
@media (max-width:900px){
  .shell{grid-template-columns:1fr}
  .side{display:none}
  .topbar nav{display:flex}
  .bottom{display:grid;grid-template-columns:repeat(5,1fr);position:fixed;left:0;right:0;bottom:0;z-index:var(--z-top);background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom,0px))}
  .bottom button{display:grid;justify-items:center;gap:2px;background:none;border:0;padding:6px 0;min-height:52px;font-size:11.5px;font-weight:500;color:var(--faint);cursor:pointer}
  .bottom button[aria-current="page"]{color:var(--ink);font-weight:700}
  .bottom button[aria-current="page"] svg{color:var(--accent)}
  .wrap{padding-bottom:96px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .tiles{grid-template-columns:repeat(2,1fr)}
  .toplist{grid-template-columns:1fr}
}
@media (max-width:560px){ .chgcols{grid-template-columns:1fr} }
@media (max-width:760px){
  body{font-size:15px}
  h1{font-size:18px}
  .topbar nav a{padding:6px 8px;font-size:13.5px}
  .wrap{gap:18px}
  .chipwrap{margin:0 -16px}
  .gchips{padding:0 40px 0 16px}
  .chipwrap::after{right:0}
  .wcards{margin:0 -16px;padding:0 16px 2px;grid-auto-columns:minmax(210px,72%)}
  .tabs{margin:0 -16px;padding:0 8px}
  .controls select{flex:1 1 40%;min-height:44px}
  .count{margin-left:0;flex-basis:100%;order:3}
  .ghost{min-height:44px}
  .list{background:none;border:0;box-shadow:none;overflow:visible;display:grid;gap:10px}
  .hrow{display:none}
  #rows{display:grid;gap:10px}
  .grid{grid-template-columns:auto 1fr auto;grid-template-areas:"st nm q" "nums nums nums";column-gap:8px;row-gap:10px;padding:14px 14px 14px 8px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--lift)}
  .row{border-top:1px solid var(--line)}
  .row:first-child{border-top:1px solid var(--line)}
  .grid>*{min-width:0}
  .g-st{grid-area:st;align-self:start} .g-nm{grid-area:nm} .g-q{grid-area:q;align-self:start}
  .nm .n{font-size:16.5px}
  .g-per,.g-pbr,.g-roe,.g-mom,.g-mc,.g-flag{display:none}
  .g-nums{grid-area:nums;display:grid!important;grid-template-columns:repeat(4,1fr);gap:6px;padding-left:8px}
  .g-nums>span{display:grid;font-size:12.5px;color:var(--muted);background:var(--raised);border-radius:8px;padding:6px 8px}
  .g-nums b{color:var(--ink);font-weight:600;font-size:14.5px}
  .g-nums>.fl{grid-column:1/-1;background:none;padding:0;display:block}
  #csv{display:none}
  .head{padding-top:0}
  .sheet{top:auto;left:0;right:0;width:auto;height:92dvh;border-left:0;border-top:1px solid var(--line);border-radius:20px 20px 0 0;transform:translateY(0)}
  .sheet.closed{transform:translateY(104%)}
  .sh-head{position:relative;padding:22px 16px 10px}
  .sh-head::before{content:"";position:absolute;left:50%;top:8px;width:40px;height:4px;border-radius:2px;background:var(--line);transform:translateX(-50%)}
  .sh-head h3{font-size:21px}
  .sh-nav{padding:0 16px 10px}
  .sh-body{padding:14px 16px 36px}
  .kgrid{grid-template-columns:repeat(2,1fr)}
  .bk{grid-template-columns:repeat(2,1fr)}
  .mt .pc{display:none} .mt td small{display:block;margin-left:0}
}
.lite .col-po,.lite .col-sc{display:none}
.searching #glance{display:none}
@media (prefers-reduced-motion:reduce){.sheet{transition:opacity 150ms linear}.sheet.closed{transform:none;opacity:0}.scrim{animation:none}.press:active,.star:active{transform:none}.sh-body{scroll-behavior:auto}}
</style>

<div class="shell">
<nav class="side" aria-label="메뉴"><div class="brand">업종 비교<small>종목 조사</small></div><button class="press" data-view="home" id="tab-home"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h5v-6h4v6h5V9.5"/></svg><span>홈</span></button><button class="press" data-view="find" id="tab-find"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><span>종목 찾기</span></button><button class="press" data-view="top" id="tab-top"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4Z"/><path d="M17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/></svg><span>TOP 20</span></button><button class="press" data-view="grp" id="tab-grp"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg><span>사업군</span></button><button class="press" data-view="div" id="tab-div"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M14.8 9.2c-.5-1-1.6-1.6-2.8-1.6-1.6 0-2.8.9-2.8 2.2 0 3 5.8 1.6 5.8 4.6 0 1.3-1.3 2.2-3 2.2-1.3 0-2.5-.6-3-1.7M12 6v1.6M12 16.6V18"/></svg><span>배당</span></button><button class="press" data-view="fin" id="tab-fin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10h18L12 4 3 10ZM5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18"/></svg><span>금융업</span></button><button class="press" data-view="watch" id="tab-watch"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8L3.5 9.7l5.9-.9L12 3.5Z"/></svg><span>관심 종목</span><span class=cnt id=wcnt></span></button><div class="side-foot"><a href="backtest.html" id="bt-link2">과거 성과 검증</a><a href="us.html" class="mkt-link">미국 화면</a></div></nav>
<div class="wrap">
  <div class="topbar"><div class="in">
    <h1 id="page-title">홈</h1>
    <nav><a href="backtest.html" id="bt-link">과거 성과 검증</a><a href="us.html" id="other-mkt">미국</a></nav>
  </div></div>

  <header class="head">
    <p class="lede">종목을 찾아서, 같은 업종 회사들과 숫자를 나란히 놓고 봅니다.</p>
    <div class="state" id="state"></div>
  </header>

  <section class="etfbox" id="etfbox" hidden><h2>관심 ETF</h2><div class="gwrap"><table class="gt"><thead><tr><th class="l">ETF</th><th class="l">구분</th><th>현재가 <span class="u">($)</span></th><th>1개월 <span class="u">(%)</span></th><th>1년 <span class="u">(%)</span></th><th>52주 고점 대비 <span class="u">(%)</span></th><th>분배금 수익률 <span class="u">(%)</span></th></tr></thead><tbody id="etftb"></tbody></table></div>
    <p class="pane-note" style="margin-top:8px">1개월·1년·고점 대비는 가격 기준(분배금 제외)입니다. 분배금 수익률은 최근 12개월 정기 분배금 합 ÷ 현재가입니다.</p></section>

  <div class="finder" id="finder">
    <label class="sr" for="q">종목 찾기</label>
    <div class="searchbox">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input type="search" id="q" placeholder="종목명 또는 코드 (예: 삼성전자, 005930)" autocomplete="off">
    </div>
    <div class="chipwrap"><div class="gchips" id="gchips" role="group" aria-label="사업군 바로 가기"></div></div>
  </div>


  <div id="pane-home" hidden>
    <div class="stats" id="stats"></div>
    <section class="watchbox" id="glance" aria-labelledby="w-h">
    <div class="sec-h"><h2 id="w-h">관심 종목</h2><span class="cnt" id="wcnt2"></span></div>
    <div class="wcards" id="wsum"></div>
  </section>
    <section class="hsec" id="h-top"></section>
    <section class="hsec" id="h-chg"></section>
    <section class="hsec" id="h-grp"></section>
    <section class="hsec"><div class="sec-h"><h2>바로 가기</h2></div><div class="tiles" id="h-tiles"></div></section>
  </div>

  <div id="pane-top" hidden>
    <div class="topnote" id="topnote"></div>
    <div class="seg" role="group" aria-label="TOP 20 기준"><button class="press" data-tb="v2" aria-pressed="true">회계 점검 통과 (v2)</button><button class="press" data-tb="g" aria-pressed="false">전체 (v1)</button></div>
    <ol class="toplist" id="toplist"></ol>
  </div>

  <div id="pane-list">
    <div class="caution vnote"><span><strong>업종 안 위치</strong>는 같은 사업군에서 가치·실적·흐름 점수가 어디쯤인지 다섯 구간으로 보여 줍니다. 검증 전 가설이며 매수 추천이 아닙니다.</span>
      <details><summary>검증 결과 보기</summary><span class="more" id="vres">과거 성과로 아직 검증되지 않았습니다.</span><span class="more" id="method"></span></details></div>
    <div class="controls">
      <select id="mk" aria-label="시장"><option value="">전체 시장</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
      <select id="grp" aria-label="사업군"><option value="">전체 사업군</option></select>
      <select id="basis" aria-label="순위 기준" hidden><option value="v2">v2</option><option value="g">v1</option><option value="m">시장 전체</option></select>
      <span class="count" id="count"></span>
      <button class="ghost press" id="csv">CSV 받기</button>
    </div>
    <div class="list" role="table" aria-label="종목 목록" style="margin-top:12px">
      <div class="grid hrow" role="row" id="hdr"></div>
      <div id="rows" role="rowgroup"></div>
    </div>
    <div style="display:grid;margin-top:14px"><button class="more press" id="more" hidden>더 보기</button></div>
  </div>

  <div id="pane-div" hidden>
    <p class="pane-note">최근 사업보고서의 주당 현금배당금(보통주)과 지금 주가로 계산한 배당수익률입니다. 3년 추이는 같은 보고서에 적힌 전전기·전기·당기 배당입니다. 관리종목·거래 부족·소형주·자본잠식은 뺐습니다.</p>
    <div class="controls">
      <select id="dmk" aria-label="시장"><option value="">전체 시장</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
      <label><input type="checkbox" id="dstreak"> 3년 연속 배당만</label>
      <label><input type="checkbox" id="dup"> 배당 늘린 종목만</label>
      <label><input type="checkbox" id="dsafe"> 배당성향 100% 이하만</label>
      <span class="count" id="dcount"></span>
    </div>
    <div class="list gwrap" style="margin-top:12px"><table class="gt" id="dtab"><thead><tr>
      <th class="l">종목</th><th class="l" id="dg-h">사업군</th><th data-dk="yield_now">배당수익률 <span class="u">(%)</span></th><th data-dk="dps">주당배당금 <span class="u" id="dps-u">(원)</span></th><th class="c" id="dtr-h">3년 추이</th><th data-dk="payout" class="col-po">배당성향 <span class="u">(%)</span></th><th data-dk="sc" class="col-sc">종합점수</th><th class="l">비고</th>
    </tr></thead><tbody id="dtb"></tbody></table></div>
  </div>

  <div id="pane-fin" hidden>
    <p class="pane-note">금융업은 재무 구조가 달라 본 비교에서 빼고 여기서 따로 비교합니다. 금융 점수 = 금융업 안에서 장부가 대비 싼 정도(B/P)·ROE·현재 배당수익률의 백분위 평균입니다. 가설이며 성과 검증 전입니다.</p>
    <div class="controls">
      <select id="fgrp" aria-label="구분"><option value="">전체 구분</option></select>
      <span class="count" id="fcount"></span>
    </div>
    <div class="list gwrap" style="margin-top:12px"><table class="gt"><thead><tr>
      <th class="c">순위</th><th class="l">종목</th><th class="l">구분</th><th>금융 점수</th><th>PBR <span class="u">(배)</span></th><th>ROE <span class="u">(%)</span></th><th>배당수익률 <span class="u">(%)</span></th><th>시총 <span class="u">(억원)</span></th><th class="l">비고</th>
    </tr></thead><tbody id="ftb"></tbody></table></div>
  </div>

  <div id="pane-grp" hidden>
    <p class="pane-note">사업군 이름을 누르면 그 사업군 종목만 목록으로 봅니다.</p>
    <div class="list gwrap"><table class="gt"><thead><tr><th class="l">사업군</th><th>비교 대상</th><th>점수 산출</th><th>신뢰도</th><th>PER 중앙값 <span class="u">(배)</span></th><th>PBR 중앙값 <span class="u">(배)</span></th><th>ROE 중앙값 <span class="u">(%)</span></th><th class="l">시총 1위</th></tr></thead><tbody id="gtb"></tbody></table></div>
  </div>

  <footer id="foot">
    <div>자료: KRX 정보데이터시스템 OPEN API(시세), OpenDART(재무). 재무는 최근 4개 분기 합(TTM), 연결재무제표 우선.</div>
    <div>사업군: KSIC 업종코드를 묶은 고정 표(v1). 비교 대상이 10개 미만인 사업군은 업종 안 위치를 매기지 않습니다.</div>
  </footer>
</div>

</div>
<nav class="bottom" aria-label="메뉴"><button data-view="home"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h5v-6h4v6h5V9.5"/></svg><span>홈</span></button><button data-view="find"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><span>찾기</span></button><button data-view="top"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4Z"/><path d="M17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/></svg><span>TOP 20</span></button><button data-view="grp"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg><span>사업군</span></button><button data-view="watch"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8L3.5 9.7l5.9-.9L12 3.5Z"/></svg><span>관심</span></button></nav>
<div class="scrim" id="scrim" hidden></div>
<aside class="sheet closed" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sh-name" hidden>
  <div class="sh-head">
    <div class="t"><h3 id="sh-name"></h3><div class="cd" id="sh-cd"></div></div>
    <div class="big" id="sh-big"></div>
    <div class="sh-act"><button class="sh-star press" id="sh-star" aria-label="관심 종목 추가">★</button><button class="close press" id="sh-close" aria-label="닫기">×</button></div>
  </div>
  <nav class="sh-nav" id="sh-nav" aria-label="상세 섹션"></nav>
  <div class="sh-body" id="sh-body"></div>
</aside>

<script>
const DATA = /*__DATA__*/null;
const S = DATA.summary, ROWS = DATA.rows, MED = DATA.med;
const BY = Object.fromEntries(ROWS.map(r=>[r.c,r]));
const LABEL = {"E/P":"이익수익률(E/P)","B/P":"장부가수익률(B/P)","EBIT/EV":"EBIT/EV","PBR-ROE잔차":"같은 ROE 대비 PBR(모형)","ROE":"ROE","GP/A":"GP/A","CFO/NI":"이익의 질(CFO/NI)","12-1모멘텀":"12-1 모멘텀","영업이익증가율":"영업이익 증가율","이자보상배율":"이자보상배율"};
const BUCKET = {"E/P":"가치","B/P":"가치","EBIT/EV":"가치","PBR-ROE잔차":"가치","ROE":"퀄리티","GP/A":"퀄리티","CFO/NI":"퀄리티","12-1모멘텀":"추세","영업이익증가율":"추세","이자보상배율":"안정성"};
const PCTM = new Set(["E/P","B/P","EBIT/EV","ROE","GP/A","12-1모멘텀","영업이익증가율"]);
const CHANGE = new Set(["12-1모멘텀","영업이익증가율"]);   // 상승 빨강·하락 파랑은 변화율에만
const BK = ["가치","퀄리티","추세","안정성"];
const COLS = [
  {k:"st",t:"",cls:"g-st",ns:1},{k:"n",t:"종목",cls:"g-nm"},{k:"q",t:"업종 안 위치",cls:"c g-q"},
  {k:"per",t:"PER",u:"배",cls:"r g-per"},{k:"pbr",t:"PBR",u:"배",cls:"r g-pbr"},
  {k:"roe",t:"ROE",u:"%",cls:"r g-roe"},{k:"mom",t:"1년 흐름",u:"%",cls:"r g-mom"},{k:"mc",t:"시총",u:"억원",cls:"r g-mc"},{k:"flag",t:"비고",cls:"c g-flag",ns:1}
];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const D = '<span class="dash">–</span>';
const num = (v,d=1) => v==null ? D : v.toLocaleString("ko-KR",{maximumFractionDigits:d,minimumFractionDigits:d});
const pct = (v,d=1,color=false,unit=true) => v==null ? D : `<span class="${color?(v>0?'up':v<0?'down':''):''}">${color&&v>0?'+':''}${(v*100).toFixed(d)}${unit?'%':''}</span>`;
const pctc = (v,d=1,color=false) => pct(v,d,color,false);   // 표 칸: 단위는 머리에
const US = (S.market==="US");
const LITE = US && !!S.lite;   // 미국 ETF·배당만 (재무 없이)
const mcap = v => v==null ? D : US ? (v>=1e9 ? `$${(v/1e9).toLocaleString("en-US",{maximumFractionDigits:1,minimumFractionDigits:1})}B` : `$${Math.round(v/1e6).toLocaleString()}M`)
  : v>=1e12 ? `${(v/1e12).toLocaleString("ko-KR",{maximumFractionDigits:1,minimumFractionDigits:1})}조` : `${Math.round(v/1e8).toLocaleString()}억`;
const mcc = v => v==null ? D : US ? (v/1e9).toLocaleString("en-US",{maximumFractionDigits:1,minimumFractionDigits:1}) : Math.round(v/1e8).toLocaleString("ko-KR");
const won = v => v==null ? '–' : US ? `$${Number(v).toLocaleString("en-US",{maximumFractionDigits:2})}` : Number(v).toLocaleString()+"원";
const tvf = v => v==null ? '–' : US ? `$${(v/1e6).toLocaleString("en-US",{maximumFractionDigits:1})}M` : Math.round(v/1e8).toLocaleString()+"억";
const medv = x => Array.isArray(x) ? x[0] : (x ?? null);
const medn = x => Array.isArray(x) ? x[1] : null;
const dfmt = d => d ? (d.includes("-") ? d : `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`) : null;
const mkname = m => m==="KOSPI"?"코스피":m==="KOSDAQ"?"코스닥":(m||"");
function fmtMetric(k,v){
  if(v==null) return D;
  if(k==="PBR-ROE잔차"){const d=Math.exp(-v)-1; return `<span class="${d<0?'down':'up'}">${d>0?'+':''}${(d*100).toFixed(0)}%</span>`;}
  if(PCTM.has(k)) return pct(v,1,CHANGE.has(k));
  if(k==="이자보상배율") return v>=100?"100배+":num(v,1)+"배";
  return num(v,2)+"배";
}
// 같은 사업군 종합점수 (순위 대상 기준): 업종 내 위치 띠
const PEERS = {};
ROWS.forEach(r=>{ if(r.rk && r.gid){ (PEERS[r.gid]=PEERS[r.gid]||[]).push(r.sc); } });
const median=a=>{a=a.filter(v=>v!=null).sort((x,y)=>x-y); if(!a.length) return null; const m=a.length>>1; return a.length%2?a[m]:(a[m-1]+a[m])/2;};
const GM={}; { const by={}; ROWS.filter(r=>!r.x.length&&r.gid).forEach(r=>(by[r.gid]=by[r.gid]||[]).push(r));
  Object.entries(by).forEach(([g,rs])=>{GM[g]={per:median(rs.map(r=>r.per)),pbr:median(rs.map(r=>r.pbr)),roe:median(rs.map(r=>r.m.ROE)),n:rs.length};}); }
// 업종 안 위치 = 같은 사업군 순위 대상 안에서 다섯 구간 (6차 협의 Q1: 순위 대신 거친 구간, 판정된 신호 아님)
const QL=["","상위 20%","상위 40%","중간","하위 40%","하위 20%"];
function quint(r){ const n=(PEERS[r.gid]||[]).length; if(!r.rk||!r.grk||!n) return null; return Math.min(5,Math.max(1,Math.ceil(5*r.grk/n))); }
function qpill(r){ const q=quint(r); return q?`<span class="qp q${q}">${QL[q]}</span>`:`<span class="qp qx">${r.x.length?'비교 제외':'–'}</span>`; }
function strip(r, W=132, H=22){
  // 가로축 = 같은 사업군 순위 대상 종목의 최저~최고 종합점수 (이 종목 포함)
  const peers=PEERS[r.gid]||[], pad=6, all=peers.concat(r.sc==null?[]:[r.sc]);
  const lo=all.length?Math.min(...all):0, hi=all.length?Math.max(...all):100, span=Math.max(hi-lo,1e-9);
  const xs=v=>pad+(W-2*pad)*(v-lo)/span;
  const y=H/2, sty=W>200?` style="width:100%;height:${H}px"`:'';
  if(r.sc==null||!peers.length) return `<svg class="strip" viewBox="0 0 ${W} ${H}"${sty} aria-hidden="true"><line class="base" x1="${pad}" y1="${y}" x2="${W-pad}" y2="${y}"/></svg>`;
  const ticks=peers.map(v=>`<line class="tick" x1="${xs(v).toFixed(1)}" y1="${y-5}" x2="${xs(v).toFixed(1)}" y2="${y+5}"/>`).join("");
  const below=peers.filter(v=>v<r.sc).length;
  const big=W>200, ends=big?`<text class="end" x="${pad}" y="${H+14}">최저 ${lo.toFixed(1)}</text><text class="end" x="${W-pad}" y="${H+14}" text-anchor="end">최고 ${hi.toFixed(1)}</text>`:'';
  return `<svg class="strip" viewBox="0 0 ${W} ${big?H+18:H}"${big?` style="width:100%;height:${H+18}px;overflow:visible"`:''} role="img" aria-label="${esc(r.g||'')} 사업군 순위 대상 ${peers.length}개 중 이 종목보다 점수 낮은 종목 ${below}개"><line class="base" x1="${pad}" y1="${y}" x2="${W-pad}" y2="${y}"/>${ticks}<circle class="me ${r.rk?'':'muted'}" cx="${xs(r.sc).toFixed(1)}" cy="${y}" r="5"/>${ends}</svg>`;
}
function chg(r){
  if(!S.prev_asof || !r.rk) return "";
  if(r.pk===false || r.prk==null) return `<span class="chg new">신규</span>`;
  const d=r.prk-r.rk;
  return d>0?`<span class="chg up">▲${d}</span>`:d<0?`<span class="chg down">▼${-d}</span>`:`<span class="chg flat">–</span>`;
}
let sortK="mc", sortDir=-1, limit=100, view="all", openCode=null, lastFocus=null;
const WKEY = US ? "qs-watch-us" : "qs-watch";
let WATCH=new Set(); try{WATCH=new Set(JSON.parse(localStorage.getItem(WKEY)||"[]"));}catch(e){}
function saveWatch(){try{localStorage.setItem(WKEY,JSON.stringify([...WATCH]));}catch(e){}}
let BASIS="g";
const HAS_V2 = ROWS.some(r=>r.rk2);
const RK = r => BASIS==="m" ? r.mrk : BASIS==="v2" ? r.rk2 : r.rk;
const GATED = r => BASIS==="v2" && !r.x.length && (r.tg||[]).length>0;   // v2에서만 순위 밖
const OUT = r => r.x.length>0 || GATED(r);
const SC = r => BASIS==="m" ? r.msc : r.sc;
const val = (r,k) => k==="q" ? quint(r) : k==="rk" ? RK(r) : k==="sc" ? SC(r) : k==="roe"?r.m.ROE : k==="mom"?r.m["12-1모멘텀"] : k==="g"?(r.g||"") : k==="n"?(r.n||"") : r[k];

function navHide(v){ document.querySelectorAll(`[data-view="${v}"]`).forEach(e=>e.hidden=true); }
function marketSetup(){
  // 자료가 아직 없는 기능은 감춘다 (다음 갱신에서 자동으로 나타남)
  if(!ROWS.some(r=>r.dv&&r.dv.dps>0)) navHide("div");
  if(!ROWS.some(r=>r.fg)) navHide("fin");
  if(!ROWS.some(r=>r.mrk)) document.getElementById("basis").hidden=true;
  if(HAS_V2 && !US){ BASIS="v2"; document.getElementById("basis").value="v2"; }
  else { const o=document.querySelector('#basis option[value="v2"]'); if(o) o.remove(); }
  if(LITE){
    document.body.classList.add("lite");
    document.title="미국 ETF·배당";
    document.querySelectorAll(".side,.bottom,#finder").forEach(e=>e.hidden=true);
    document.querySelector(".shell").style.gridTemplateColumns="1fr";
    ["dstreak","dsafe"].forEach(id=>document.getElementById(id).closest("label").hidden=true);
    document.getElementById("dg-h").textContent="배당 주기";
    document.getElementById("dtr-h").textContent="2년 추이";
    document.querySelector(".lede").textContent="관심 ETF와 미국 배당주를 최근 12개월 배당 기준으로 봅니다. 미국 재무 자료(SEC)를 받을 수 없어 저평가 순위는 아직 없습니다.";
    const cau=document.querySelector(".caution"); cau.classList.remove("vnote"); document.querySelector(".head").append(cau);
    cau.innerHTML="<strong>배당수익률은 지난 12개월 기준입니다.</strong> 앞으로의 배당을 보장하지 않으며, 특별배당은 빼고 계산합니다. <details><summary>계산 방법</summary><span class='more' id='method'></span></details>";
  }
  if(!US){ if(!S.has_us) document.getElementById("other-mkt").hidden=true; return; }
  if(!LITE) document.title="미국 업종 대비 저평가 후보";
  document.querySelector("h1").textContent=LITE?"미국 ETF·배당":"미국 업종 대비 저평가 후보";
  const o=document.getElementById("other-mkt"); o.href="index.html"; o.textContent="국내 보기";
  document.getElementById("bt-link").hidden=true; document.getElementById("bt-link2").hidden=true;
  document.querySelectorAll(".mkt-link").forEach(a=>{a.href="index.html"; a.textContent="국내 화면";});
  const mk=document.getElementById("mk"); mk.innerHTML='<option value="">전체 거래소</option><option value="NYSE">NYSE</option><option value="Nasdaq">Nasdaq</option>';
  document.getElementById("foot").innerHTML=LITE?'<div>자료: Massive(시세·배당 기록, 무료 등급 장마감 기준). NYSE·Nasdaq 보통주 중 20일 평균 거래대금 200만 달러 이상, 주가 1달러 이상.</div><div>미국 저평가 순위는 재무 자료(SEC)를 GitHub 서버에서 받을 수 없어 보류 중입니다.</div>':'<div>자료: SEC EDGAR(재무·업종 SIC, 무료 공식), Massive(시세, 무료 등급 장마감 기준). 재무는 최근 4개 분기 합(TTM), SEC 달력 분기 기준.</div><div>사업군: SIC 업종코드를 묶은 고정 표(v1). 금융·리츠·스팩은 본 순위에서 제외. 다중 클래스 주식은 대표 티커 가격 × 전체 주식수.</div>';
  navHide("fin");
  const E=(S.etfs||[]).filter(e=>e.close!=null);
  if(E.length){ document.getElementById("etfbox").hidden=false;
    document.getElementById("etftb").innerHTML=E.map(e=>`<tr><td class="l nmcell">${esc(e.ticker)}<small>${esc(e.name)}</small></td><td class="l">${esc(e.kind)}</td><td>${Number(e.close).toLocaleString("en-US",{maximumFractionDigits:2,minimumFractionDigits:2})}</td><td>${pctc(e.r1m,1,true)}</td><td>${pctc(e.r1y,1,true)}</td><td>${pctc(e.from_high,1,true)}</td><td>${e.dy==null?D:e.dy.toFixed(2)}${e.dfreq?`<small style="display:block;color:var(--faint)">${esc(e.dfreq)} 분배</small>`:''}</td></tr>`).join(""); }
  document.getElementById("dps-u").textContent="($)";
  document.querySelector("#pane-div .pane-note").textContent=LITE?"최근 12개월 정기 배당금 합을 지금 주가로 나눈 배당수익률입니다. 2년 추이는 전년 12개월과 최근 12개월 배당 합 비교입니다.":"SEC 공시의 주당 배당 선언액 최근 4분기 합과 지금 주가로 계산한 배당수익률입니다. 시가총액·거래대금 기준 미달, 자본잠식은 뺐습니다.";
}
function head(){
  const V=S.validation;   // 검증 구간 결과 (D-31, 5차 협의 Q4: 불합격을 첫 화면에 숫자로 공개)
  if(V&&!US){ const p=v=>(v>0?'+':'')+(v*100).toFixed(1)+'%';
    document.getElementById("vres").innerHTML=`검증 결과 <b>기준 미통과</b> (${esc(V.from)}~${esc(V.to)}, 분기마다 상위 20종목 교체: 연 ${p(V.cagr)} · 전 종목 평균 ${p(V.ew)} · 코스피 ${p(V.kospi)}). 점수 순서에는 정보가 있었지만 상위 종목을 사는 방식은 이기지 못했습니다. 순위는 참고용 지표로만 보세요. <a href="backtest.html">자세히</a>`
    if(HAS_V2) document.getElementById("vres").innerHTML+=`<span class="v2note"><b>v2</b>는 v1에 '진짜인가' 거름 4개(현금 없는 이익, 일회성 이익 등)를 더한 순위입니다. 과거 자료로 검증된 것이 아니며, 2026-11-13부터 v1과 나란히 실시간 기록해 1년 뒤 판정합니다.</span>`; }
  const d=S.asof_price, ds=d?dfmt(d):"";
  const st=S.status||{}, w=(st.warnings||[]).filter(x=>!x.startsWith("재무 수집 진행 중"));
  const h=st.history;
  const pill = !S.collection_complete
    ? `<span class="pill wait"><i></i>잠정 순위 · 재무 수집 ${S.universe_collected.toLocaleString()}/${S.universe_total.toLocaleString()}개사</span>`
    : w.length ? `<span class="pill wait"><i></i>확인 필요 ${w.length}건</span>` : `<span class="pill ok"><i></i>오늘 갱신됨</span>`;
  if(LITE){
    document.getElementById("state").innerHTML = `<span class="pill ok"><i></i>ETF·배당만</span><span>시세 기준 <b>${ds||'–'}</b></span><span>배당주 <b>${S.evaluated.toLocaleString()}</b>개</span>`+
      (w.length?`<span>${w.map(esc).join(" / ")}</span>`:"");
    document.getElementById("method").textContent="배당수익률 = 최근 12개월 정기 배당 합(특별배당 제외) ÷ 최근 종가. 배당 주기는 지난 1년 배당 기록에서 가장 많은 주기입니다.";
    return;
  }
  document.getElementById("state").innerHTML = pill +
    `<span>시세 기준 <b>${ds}</b></span><span>전체 <b>${S.evaluated.toLocaleString()}</b>개사 · 업종 비교 가능 ${S.ranked.toLocaleString()}개사</span>` +
    (h&&h.targets&&!h.complete?`<span>과거 재무 수집 ${Math.round(100*h.processed/h.targets)}% (백테스트 준비)</span>`:"") +
    (w.length?`<span>${w.map(esc).join(" / ")}</span>`:"");
  const ka=st.kind_admin;
  document.getElementById("method").textContent = "같은 사업군 안에서 지표마다 백분위를 매기고, 가치 40%·퀄리티 30%·추세 20%·안정성 10%로 합칩니다. " +
    (ka?`관리종목은 코스닥 KRX 소속부와 코스피 KIND 목록(${dfmt(ka.date)}) 기준으로 제외합니다. `:"") + "자료가 없는 지표는 빈칸으로 두고 점수를 채워 넣지 않습니다.";
  const gs=[...new Set(ROWS.filter(r=>r.g&&r.gid!=="EX").map(r=>r.g))].sort((a,b)=>a.localeCompare(b,"ko"));
  const sel=document.getElementById("grp"); gs.forEach(g=>{const o=document.createElement("option");o.textContent=g;sel.append(o);});
}
function glance(){
  const ws=[...WATCH].map(c=>BY[c]).filter(Boolean);
  document.getElementById("wcnt").textContent = ws.length?ws.length:"";
  document.getElementById("wcnt2").textContent = ws.length?ws.length+"개":"";
  document.getElementById("glance").hidden = LITE || !ws.length;
  document.getElementById("wsum").innerHTML = ws.map(r=>{const nw=r.w.length+(r.tg||[]).length;
    return `<button class="wcard press" data-open="${r.c}"><span class="top"><b>${esc(r.n)}</b>${qpill(r)}</span><span class="g">${esc(r.g||r.fg||'')}</span>
    <span class="kv3"><span>PER<b>${r.per==null?'–':num(r.per,1)}</b></span><span>PBR<b>${r.pbr==null?'–':num(r.pbr,2)}</b></span><span>ROE<b>${r.m.ROE==null?'–':(r.m.ROE*100).toFixed(1)+'%'}</b></span></span>${nw?`<span class="neg">확인할 점 ${nw}개</span>`:''}</button>`;}).join("");
}
function gchips(){
  const cnt={}; ROWS.forEach(r=>{ if(r.g&&r.gid!=="EX"&&r.rk) cnt[r.g]=(cnt[r.g]||0)+1; });
  const top=Object.entries(cnt).sort((a,b)=>b[1]-a[1]).slice(0,12);
  const cur=document.getElementById("grp").value;
  document.getElementById("gchips").innerHTML = top.map(([g,n])=>`<button class="press" data-g="${esc(g)}" aria-pressed="${cur===g}">${esc(g)}<small>${n}</small></button>`).join("");
}
function header(){
  document.getElementById("hdr").innerHTML = COLS.map(c=>{ if(c.k==="st") return `<div class="g-st" role="columnheader"><span class="sr">관심</span></div>`;const u=c.u?` <span class="u">(${c.k==="mc"&&US?"$B":c.u})</span>`:''; return `<div class="${c.cls}" role="columnheader">${c.ns?c.t:`<button data-k="${c.k}" ${sortK===c.k?`aria-sort="${sortDir>0?'ascending':'descending'}"`:''}>${c.t}${u}</button>`}</div>`;}).join("");
}
function filtered(){
  const q=document.getElementById("q").value.trim().toLowerCase(), mk=document.getElementById("mk").value,
        g=document.getElementById("grp").value;
  let rs=ROWS.filter(r=>(view==="watch"? WATCH.has(r.c) : true) && (!mk||r.mk===mk) && (!g||r.g===g) &&
    (!q || (r.n||"").toLowerCase().includes(q) || r.c.includes(q)));
  rs.sort((a,b)=>{const x=val(a,sortK), y=val(b,sortK);
    if(x==null&&y==null) return 0; if(x==null) return 1; if(y==null) return -1;
    return (typeof x==="string" ? x.localeCompare(y,"ko") : x-y)*sortDir;});
  return rs;
}
function flag(r){
  const tg=r.tg||[];
  if(!r.x.length && tg.length) return `<span class="chip ${BASIS==="v2"?"x":"tg"}" title="${esc(tg.join(' · '))}">회계 점검 ${tg.length}</span>`;
  return r.x.length ? `<span class="chip x" title="${esc(r.x.join(' · '))}">${esc(r.xp||r.x[0])}${r.x.length>1?` +${r.x.length-1}`:''}</span>`
       : r.w.length ? `<span class="chip" title="${esc(r.w.join(' · '))}">경고 ${r.w.length}</span>` : '';
}
function row(r){
  const fl=flag(r);
  return `<div class="grid row ${openCode===r.c?'sel':''}" role="row" tabindex="0" data-c="${r.c}">
    <div class="g-st" role="cell"><button class="star ${WATCH.has(r.c)?'on':''}" data-w="${r.c}" aria-label="관심 종목 ${WATCH.has(r.c)?'해제':'추가'}" aria-pressed="${WATCH.has(r.c)}">★</button></div>
    <div class="g-nm nm" role="cell"><span class="n">${esc(r.n)}</span><span class="cd">${esc(r.g||r.fg||'미배정')}${r.gn?`<small class="${r.rel==='낮음'?'low':''}"> ${r.gn}</small>`:''} · ${r.c} · ${mkname(r.mk)}</span></div>
    <div class="g-q c" role="cell">${qpill(r)}</div>
    <div class="g-per r" role="cell">${r.per==null?D:num(r.per,1)}</div><div class="g-pbr r" role="cell">${r.pbr==null?D:num(r.pbr,2)}</div>
    <div class="g-roe r" role="cell">${pctc(r.m.ROE)}</div><div class="g-mom r" role="cell">${pctc(r.m["12-1모멘텀"],0,true)}</div>
    <div class="g-mc r" role="cell">${mcc(r.mc)}</div><div class="g-flag c" role="cell">${fl}</div>
    <div class="g-nums" role="cell"><span>PER<b>${r.per==null?'–':num(r.per,1)}</b></span><span>PBR<b>${r.pbr==null?'–':num(r.pbr,2)}</b></span><span>ROE<b>${r.m.ROE==null?'–':(r.m.ROE*100).toFixed(1)+'%'}</b></span><span>시총<b>${mcap(r.mc)}</b></span>${fl?`<span class="fl">${fl}</span>`:''}</div>
  </div>`;
}
function render(){
  header();
  const rs=filtered(), shown=rs.slice(0,limit);
  const box=document.getElementById("rows");
  if(!rs.length){
    box.innerHTML = view==="watch" && !WATCH.size
      ? '<div class="emptybox"><b>아직 관심 종목이 없습니다</b>전체 종목에서 종목 이름 옆 ★를 누르면 여기에 모입니다.</div>'
      : '<div class="emptybox"><b>조건에 맞는 종목이 없습니다</b>검색어나 시장·사업군 선택을 바꿔 보세요.</div>';
  } else box.innerHTML = shown.map(row).join("");
  document.getElementById("count").textContent = `${rs.length.toLocaleString()}개 종목${sortK==="mc"&&sortDir<0?" · 시총 큰 순":""}`;
  const more=document.getElementById("more"); more.hidden = rs.length<=limit; more.textContent=`더 보기 (${(rs.length-limit).toLocaleString()}개 남음)`; gchips();
}
function why(r){
  const ks=Object.keys(LABEL).filter(k=>r.p[k]!=null);
  const strong=ks.filter(k=>r.p[k]>=80).sort((a,b)=>r.p[b]-r.p[a]).slice(0,3);
  const weak=ks.filter(k=>r.p[k]<=30).sort((a,b)=>r.p[a]-r.p[b]).slice(0,2);
  const miss=Object.keys(LABEL).filter(k=>r.p[k]==null).map(k=>LABEL[k]);
  const bi=r.b.map((v,i)=>[v,i]).filter(x=>x[0]!=null).sort((a,b)=>b[0]-a[0]);
  const nv=Object.keys(LABEL).filter(k=>r.m[k]!=null).length;
  const L=[];
  const q=quint(r);
  L.push(q ? `${esc(r.g)} 사업군에서 점수를 낼 수 있는 ${(PEERS[r.gid]||[]).length}개사 중 <b>${QL[q]}</b> 구간입니다. <span class="note">(검증 전 가설, 매수 신호 아님)</span>`
        : r.x.length ? `<b>업종 비교 제외</b>: ${esc(r.xpf||r.x[0])}${r.x.length>1?` 외 ${r.x.length-1}건`:''}` : '<b>업종 비교 자료 없음</b>');
  if(bi.length) L.push(`가장 높은 묶음은 <b>${BK[bi[0][1]]}</b>(${bi[0][0].toFixed(0)}점)${bi.length>1?`, 가장 낮은 묶음은 <b>${BK[bi[bi.length-1][1]]}</b>(${bi[bi.length-1][0].toFixed(0)}점)`:''}입니다.`);
  if(strong.length) L.push(`<span class="pos">강점</span> ${strong.map(k=>`${LABEL[k]} 사업군 상위 ${Math.max(1,Math.round(100-r.p[k]))}%`).join(", ")}`);
  if(weak.length) L.push(`<span class="neg">약점</span> ${weak.map(k=>`${LABEL[k]} 사업군 하위 ${Math.max(1,Math.round(r.p[k]))}%`).join(", ")}`);
  const notes=[`지표 ${nv}/10개 계산`];
  if(miss.length) notes.push(`자료 없음: ${miss.join(", ")}`);
  if(r.rel==="낮음") notes.push(`비교 대상이 ${r.gn}개뿐이라 백분위가 거칩니다`);
  L.push(`<span class="note">${esc(notes.join(" · "))}</span>`);
  return `<div class="why">${L.map(l=>`<div>${l}</div>`).join("")}</div>`;
}
function truthBox(r){
  const q=r.tq; if(!q) return '';
  const eok=v=>v==null?'–':(Math.abs(v)>=1e12?(v/1e12).toFixed(2)+'조':Math.round(v/1e8).toLocaleString()+'억')+'원';
  const C=[
    ["영업으로 현금이 들어오나", !(q.ni>0 && q.cfo!=null && q.cfo<0), `순이익 ${eok(q.ni)} · 영업현금흐름 ${eok(q.cfo)}`, q.ni==null||q.cfo==null],
    ["이익이 영업에서 나오나", !(q.op>0 && q.ni!=null && q.ni>q.op*1.5), `순이익 ${eok(q.ni)} · 영업이익 ${eok(q.op)} (1.5배 넘으면 일회성 의심)`, q.ni==null||q.op==null],
    ["영업적자인데 순이익 흑자 아닌가", !(q.op!=null && q.op<=0 && (r.tg||[]).some(x=>x.startsWith("영업적자"))), `영업이익 ${eok(q.op)}`, q.op==null],
    ["현금 없는 이익이 과하지 않나", !(r.tg||[]).some(x=>x.startsWith("발생액")), `발생액 비율 ${q.acc==null?'–':(q.acc*100).toFixed(1)+'%'}${q.acc10!=null?` (사업군 상위 10% 기준 ${(q.acc10*100).toFixed(1)}%)`:''}`, q.acc==null||q.acc10==null]];
  const n=(r.tg||[]).length;
  return `<div><h5>회계 '진짜인가' 점검 · ${n?`<span class="neg">${n}개 걸림</span>`:'<span class="pos">모두 통과</span>'}</h5>
    <ul class="truth">${C.map(([t,ok,d,na])=>`<li class="${na?'na':ok?'ok':'bad'}"><span class="ic" aria-hidden="true">${na?'–':ok?'✓':'✕'}</span><span><b>${t}</b><small>${esc(d)}${na?' · 자료 부족(거르지 않음)':''}</small></span></li>`).join("")}</ul>
    <p class="note" style="margin:8px 0 0">이익이 실제 현금·영업에서 나오는지 보는 점검입니다. 과거 자료로 검증되지 않았고 실시간 기록으로 판정합니다.</p></div>`;
}
function openSheet(c){
  const r=BY[c]; if(!r) return;
  openCode=c; lastFocus=document.activeElement;
  document.getElementById("sh-name").textContent=r.n;
  document.getElementById("sh-cd").innerHTML=`${r.c} · ${mkname(r.mk)} · ${esc(r.g||r.fg||'미배정')}${r.gn?` (비교 대상 ${r.gn}개)`:''}`;
  document.getElementById("sh-big").innerHTML = `${qpill(r)}<small>업종 안 위치</small>`;
  const st=document.getElementById("sh-star"); st.classList.toggle("on",WATCH.has(c)); st.setAttribute("aria-label",`관심 종목 ${WATCH.has(c)?'해제':'추가'}`); st.setAttribute("aria-pressed",WATCH.has(c));
  const co=r.co, hm=co&&co.hm&&/^https?:\/\/[^\s"'<>]+$/i.test(co.hm)?co.hm:null;
  const about=co?[co.ind?`<span class="ind">${esc(co.ind)}${co.ind_approx?' <small>(상위 분류)</small>':''}</span>`:'',
    co.est?`<span>${esc(co.est)}년 설립</span>`:'', hm?`<a href="${esc(hm)}" target="_blank" rel="noopener noreferrer">홈페이지 ↗</a>`:''].filter(Boolean).join('<i aria-hidden="true">·</i>'):'';
  const med=MED[r.gid]||{}, gm=GM[r.gid]||{};
  const kt=(lab,v,m)=>`<div><span>${lab}</span><b>${v}</b><small>${m==null?'업종 중앙값 –':'업종 중앙값 '+m}</small></div>`;
  const kg=`<div class="kgrid">${kt("PER",r.per==null?'–':num(r.per,1)+'배',gm.per==null?null:num(gm.per,1)+'배')}${kt("PBR",r.pbr==null?'–':num(r.pbr,2)+'배',gm.pbr==null?null:num(gm.pbr,2)+'배')}${kt("ROE",r.m.ROE==null?'–':(r.m.ROE*100).toFixed(1)+'%',gm.roe==null?null:(gm.roe*100).toFixed(1)+'%')}<div><span>시가총액</span><b>${mcap(r.mc)}</b><small>현재가 ${r.px?won(r.px):'–'}</small></div></div>`;
  const mrows=Object.keys(LABEL).map(k=>`<tr><td class="l">${LABEL[k]}<small>${BUCKET[k]}</small></td>
    <td${r.m[k]==null&&(r.mr||{})[k]?` title="${esc(r.mr[k])}"`:''}>${fmtMetric(k,r.m[k])}</td>
    <td>${k==="PBR-ROE잔차"?D:fmtMetric(k,medv(med[k]))+(medn(med[k])?`<small>n=${medn(med[k])}</small>`:'')}</td>
    <td>${r.p[k]==null?D:`${r.p[k].toFixed(0)}<span class="pc"><i style="width:${r.p[k]}%"></i></span>`}</td></tr>`).join("");
  const fb = r.fb.length ? r.fb.map(x=>x.replace("ni_owners","지배주주 순이익").replace("equity_owners","지배주주 자본").replace("separate_only","별도재무제표만 제출 → 전체=지배").replace("no_nci","비지배지분 없음 → 전체=지배")).join(", ") : "표준 계정으로 확인";
  const list=a=>a.length?`<ul class="lst">${a.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:'<span class="dash">없음</span>';
  const nchk=r.w.length+(r.tg||[]).length+r.x.length;
  const secs=[["sum","요약"],["met","지표 비교"],["chk",`점검${nchk?` ${nchk}`:''}`],["info","회사·배당"]];
  document.getElementById("sh-nav").innerHTML=secs.map(([id,t],i)=>`<button class="press" data-sec="${id}" aria-current="${i===0}">${t}</button>`).join("");
  document.getElementById("sh-body").innerHTML = `
    <section class="sh-sec" id="sec-sum"><h4 class="sr">요약</h4>${about?`<div class="about">${about}</div>`:''}${kg}${why(r)}
      <div><h5>묶음 점수 (사업군 안 백분위 평균${r.sc!=null?` · 종합 ${r.sc.toFixed(1)}`:''})</h5><div class="bk">${BK.map((b,i)=>`<div><span>${b}</span><b>${r.b[i]==null?'–':r.b[i].toFixed(0)}</b><div class="bar"><i style="width:${r.b[i]??0}%"></i></div></div>`).join("")}</div></div>
      <div><h5>사업군 점수 분포 (${(PEERS[r.gid]||[]).length}개, 점이 이 종목)</h5>${strip(r,560,28)}</div></section>
    <section class="sh-sec" id="sec-met"><h4>지표 비교</h4><table class="mt"><thead><tr><th class="l">지표</th><th>이 종목</th><th>업종 중앙값</th><th>백분위</th></tr></thead><tbody>${mrows}</tbody></table></section>
    <section class="sh-sec" id="sec-chk"><h4>점검</h4>
      ${r.x.length?`<div><h5>업종 비교 제외 사유</h5>${list(r.x)}</div>`:''}
      ${truthBox(r)}
      <div><h5>경고</h5>${list(r.w)}</div></section>
    <section class="sh-sec" id="sec-info"><h4>회사·배당</h4>
      ${co&&co.desc?`<p class="desc">${esc(co.desc)}${co.desc_src?`<small>출처: ${esc(co.desc_src)} 사업의 개요</small>`:''}</p>`:''}
      ${r.dv?`<div><h5>배당 (${r.dv.year} 사업연도 보고서)</h5><dl class="kv"><dt>주당배당금</dt><dd>${r.dv.dps==null?'–':Math.round(r.dv.dps).toLocaleString()+'원'} ${r.dv.trend?`(${r.dv.trend})`:''}</dd>
        <dt>배당수익률(현재가)</dt><dd>${r.dv.yield_now==null?'–':r.dv.yield_now.toFixed(2)+'%'}</dd><dt>배당성향</dt><dd>${r.dv.payout==null?'–':r.dv.payout.toFixed(0)+'%'}</dd>
        <dt>3년 추이</dt><dd>${[r.dv.dps2,r.dv.dps1,r.dv.dps].map(x=>x==null?'–':Math.round(x).toLocaleString()).join(' → ')}원</dd></dl></div>`:''}
      ${r.fg?`<div><h5>금융업 비교 (${esc(r.fg)})</h5><dl class="kv"><dt>금융 점수</dt><dd>${r.fs2==null?'–':r.fs2.toFixed(1)}${r.frk?` (금융업 ${r.frk}위)`:''}</dd>${r.fp?`<dt>백분위</dt><dd>B/P ${r.fp["B/P"]??'–'} · ROE ${r.fp.ROE??'–'} · 배당 ${r.fp["배당"]??'–'}</dd>`:''}</dl></div>`:''}
      <div><h5>데이터</h5><dl class="kv"><dt>재무 기준</dt><dd>${r.pd||'–'} ${r.fs==="CFS"?"연결":r.fs==="OFS"?"별도":""}</dd>
        <dt>공시일</dt><dd>${r.fd?dfmt(r.fd):'미확인'}${r.av?` (점수 반영 ${dfmt(r.av)})`:''}</dd>
        <dt>20일 평균 거래대금</dt><dd>${tvf(r.tv)}</dd><dt>F-스코어</dt><dd>${r.fsc??'–'}</dd></dl>
        <details style="margin-top:10px"><summary>세부 데이터</summary><dl class="kv" style="margin-top:8px"><dt>업종코드</dt><dd>${r.ksic||'–'}</dd><dt>계정 선택</dt><dd>${esc(fb)}</dd>${r.ib?`<dt>이자 기준</dt><dd>${esc(r.ib)}</dd>`:''}</dl></details></div></section>`;
  const sh=document.getElementById("sheet"), sc=document.getElementById("scrim");
  sh.hidden=false; sc.hidden=false; requestAnimationFrame(()=>sh.classList.remove("closed"));
  document.getElementById("sh-body").scrollTop=0;
  document.getElementById("sh-close").focus({preventScroll:true});
  document.querySelectorAll(".row.sel").forEach(e=>e.classList.remove("sel"));
  const rowEl=document.querySelector(`.row[data-c="${c}"]`); if(rowEl) rowEl.classList.add("sel");
}
function closeSheet(){
  const sh=document.getElementById("sheet");
  sh.classList.add("closed"); document.getElementById("scrim").hidden=true; openCode=null;
  setTimeout(()=>{ if(sh.classList.contains("closed")) sh.hidden=true; }, 240);
  document.querySelectorAll(".row.sel").forEach(e=>e.classList.remove("sel"));
  if(lastFocus&&lastFocus.focus) lastFocus.focus({preventScroll:true});
}
function groupView(){
  const by={}, cmp={}; ROWS.filter(r=>!r.x.length&&r.g).forEach(r=>(by[r.g]=by[r.g]||[]).push(r));
  ROWS.filter(r=>r.g&&r.gn).forEach(r=>{cmp[r.g]=r.gn;});
  const rows=Object.entries(by).map(([g,rs])=>{rs.sort((a,b)=>(b.mc||0)-(a.mc||0)); return {g,n:rs.filter(r=>r.rk).length,rel:rs[0].rel,per:median(rs.map(r=>r.per)),pbr:median(rs.map(r=>r.pbr)),roe:median(rs.map(r=>r.m.ROE)),top:rs[0]};}).sort((a,b)=>(cmp[b.g]||0)-(cmp[a.g]||0));
  document.getElementById("gtb").innerHTML=rows.map(x=>`<tr><td class="l"><button class="toplink" data-gsel="${esc(x.g)}">${esc(x.g)}</button></td><td>${cmp[x.g]??'–'}</td><td>${x.n}</td><td class="${x.rel==='낮음'?'low':''}">${x.rel||''}</td><td>${x.per==null?D:num(x.per,1)}</td><td>${x.pbr==null?D:num(x.pbr,2)}</td><td>${pctc(x.roe)}</td><td class="l"><button class="toplink" data-open="${x.top.c}">${esc(x.top.n)}</button></td></tr>`).join("")||'<tr><td colspan="8" class="l dash">비교할 사업군이 없습니다.</td></tr>';
}
const PANES = {home:"pane-home", top:"pane-top", find:"pane-list", all:"pane-list", watch:"pane-list", div:"pane-div", fin:"pane-fin", grp:"pane-grp"};
const TITLES = {home:"홈", top:"TOP 20", find:"종목 찾기", all:"종목 찾기", watch:"관심 종목", div:"배당", fin:"금융업", grp:"사업군"};
function tab(which, keepScroll){
  if(which==="all") which="find";
  if(!PANES[which]) which="home";
  if(which==="find"||which==="watch") view = which==="watch" ? "watch" : "all";
  document.querySelectorAll("[data-view]").forEach(b=>b.setAttribute("aria-current", b.dataset.view===which ? "page" : "false"));
  new Set(Object.values(PANES)).forEach(id=>document.getElementById(id).hidden = id!==PANES[which]);
  document.getElementById("finder").hidden = LITE || !(which==="home"||which==="find");
  document.querySelector(".lede").hidden = which!=="home" && !LITE;
  if(!LITE){ document.getElementById("page-title").textContent = TITLES[which]; document.title = `${TITLES[which]} · 업종 비교 종목 조사`; }
  if(!LITE && location.hash.slice(1)!==which) history.replaceState(null,"","#"+which);
  if(which==="grp") groupView(); else if(which==="div") divView(); else if(which==="fin") finView();
  else if(which==="home") home(); else if(which==="top") topView(); else { limit=100; render(); }
  if(!keepScroll) window.scrollTo(0,0);
}
// ── 홈 ──
function v2ok(){ return ROWS.some(r=>r.rk2); }
function home(){
  const ndiv=ROWS.filter(r=>r.dv&&r.dv.dps>0).length, nchk=ROWS.filter(r=>r.rk2).length;
  const stat=(l,v,sm)=>`<div class="stat"><span>${l}</span><b>${v}</b>${sm?`<small>${sm}</small>`:''}</div>`;
  document.getElementById("stats").innerHTML = stat("전체 종목",S.evaluated.toLocaleString(),"코스피·코스닥")+stat("업종 비교 가능",S.ranked.toLocaleString(),"비교 대상 10개 이상 사업군")+
    stat("회계 점검 통과",nchk?nchk.toLocaleString():'–',"현금·영업에서 나온 이익")+stat("배당주",ndiv.toLocaleString(),"최근 사업보고서 기준");
  const tk = v2ok() ? "rk2" : "rk";
  const t5=ROWS.filter(r=>r[tk]).sort((a,b)=>a[tk]-b[tk]).slice(0,5);
  document.getElementById("h-top").innerHTML = `<div class="sec-h"><h2>점수 TOP 5</h2><span class="cnt">가설 · ${tk==="rk2"?"회계 점검 통과":"전체"}</span><button class="go" data-go="top">TOP 20 보기 ›</button></div>
    <ol class="mini5">${t5.map(r=>`<li><button data-open="${r.c}"><span class="no">${r[tk]}</span><span class="t"><b>${esc(r.n)}</b><small>${esc(r.g||'')}</small></span>${qpill(r)}<span class="s">${num(r.sc,1)}</span></button></li>`).join("")}</ol>`;
  const box=document.getElementById("h-chg");
  if(!S.prev_asof){ box.hidden=true; }
  else { box.hidden=false;
    const ranked=ROWS.filter(r=>r.rk).sort((a,b)=>a.rk-b.rk), top=ranked.filter(r=>r.rk<=30);
    const neu=top.filter(r=>r.pk===false||r.prk==null||r.prk>30), out=ROWS.filter(r=>r.prk!=null&&r.prk<=30&&(!r.rk||r.rk>30));
    const li=(r,tag)=>`<li><button data-open="${r.c}"><span>${esc(r.n)}</span>${tag}</button></li>`;
    box.innerHTML = `<div class="sec-h"><h2>지난 갱신 대비 변화</h2><span class="cnt">${dfmt(S.prev_asof)} → ${dfmt(S.asof_price)}, 점수 30위 기준</span></div>
      <div class="chgcols"><div class="chgbox"><h3>새로 들어옴 ${neu.length}</h3><ul>${neu.slice(0,6).map(r=>li(r,`<span class="chg new">${r.rk}위</span>`)).join("")||'<li class="empty-note">없음</li>'}</ul></div>
      <div class="chgbox"><h3>빠짐 ${out.length}</h3><ul>${out.slice(0,6).map(r=>li(r,`<span class="chg down">${r.rk?r.rk+'위':'제외'}</span>`)).join("")||'<li class="empty-note">없음</li>'}</ul></div></div>`; }
  const cnt={}; ROWS.forEach(r=>{ if(r.g&&r.gid&&r.gid!=="EX"&&r.rk) (cnt[r.g]=cnt[r.g]||{n:0,gid:r.gid}).n++; });
  const gs=Object.entries(cnt).sort((a,b)=>b[1].n-a[1].n).slice(0,8);
  document.getElementById("h-grp").innerHTML = `<div class="sec-h"><h2>업종 둘러보기</h2><button class="go" data-go="grp">전체 사업군 ›</button></div>
    <div class="gcards">${gs.map(([g,x])=>{const m=GM[x.gid]||{}; return `<button class="gcard press" data-gsel="${esc(g)}"><b>${esc(g)}</b><small>${x.n}개사 · PER 중앙값 ${m.per==null?'–':num(m.per,1)+'배'}</small></button>`;}).join("")}</div>`;
  const tile=(go,k,t,sm)=>`<button class="tile press" data-go="${go}">${NAVIC[k]||''}<span>${t}<small>${sm}</small></span></button>`;
  document.getElementById("h-tiles").innerHTML = tile("div","div","배당","배당수익률·3년 추이")+tile("fin","fin","금융업","은행·보험·증권 따로 비교")+
    (US?'':`<a class="tile press" href="backtest.html">${NAVIC.top}<span>과거 성과 검증<small>점수가 실제로 통했나</small></span></a>`)+(S.has_us&&!US?`<a class="tile press" href="us.html">${NAVIC.find}<span>미국<small>ETF·배당</small></span></a>`:'');
  glance(); gchips();
}
const NAVIC={}; document.querySelectorAll(".side button[data-view]").forEach(b=>{NAVIC[b.dataset.view]=b.querySelector("svg").outerHTML;});
// ── TOP 20 ──
let TB = "v2";
function topView(){
  if(!v2ok()){ TB="g"; document.querySelector(".seg").hidden=true; }
  document.querySelectorAll(".seg button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.tb===TB));
  const V=S.validation, p=v=>(v>0?'+':'')+(v*100).toFixed(1)+'%';
  document.getElementById("topnote").innerHTML = `<b>매수 목록이 아닙니다.</b> 같은 사업군 안 백분위를 합친 점수의 상위 20개입니다.`+
    (V?` 과거 검증(${esc(V.from)}~${esc(V.to)})에서 분기마다 상위 20종목을 샀다면 연 ${p(V.cagr)}로, 전 종목 평균 ${p(V.ew)}·코스피 ${p(V.kospi)}보다 낮았습니다. 2026-11-13부터 실시간 기록 중이며 1년 뒤 판정합니다. <a href="backtest.html">자세히</a>`:'');
  const k = TB==="v2" ? "rk2" : "rk";
  const rs=ROWS.filter(r=>r[k]).sort((a,b)=>a[k]-b[k]).slice(0,20);
  document.getElementById("toplist").innerHTML = rs.map(r=>{
    const bi=r.b.map((v,i)=>[v,i]).filter(x=>x[0]!=null).sort((a,b)=>b[0]-a[0]);
    const best=bi.length?`<span class="pos">${BK[bi[0][1]]} 강함</span>`:'';
    return `<li><button class="tcard press p${r[k]}" data-open="${r.c}"><span class="no">${r[k]}${TB==="g"?`<small>${chg(r)}</small>`:''}</span>
      <span class="t"><b>${esc(r.n)}</b><small>${esc(r.g||'')} · ${mkname(r.mk)}</small></span><span class="s">${num(r.sc,1)}<small>점</small></span>
      <span class="k">${best}<span>PER <b>${r.per==null?'–':num(r.per,1)}</b></span><span>PBR <b>${r.pbr==null?'–':num(r.pbr,2)}</b></span><span>ROE <b>${r.m.ROE==null?'–':(r.m.ROE*100).toFixed(1)+'%'}</b></span><span>시총 <b>${mcap(r.mc)}</b></span>${r.w.length?`<span class="chip">경고 ${r.w.length}</span>`:''}</span></button></li>`;}).join("");
}
// ── 배당 탭 ──
const HARD = ["관리종목","투자주의","거래정지","시가총액","거래대금","자본잠식","감사","시세 없음","재무 없음","상장 1년 미만"];
let dSort="yield_now", dDir=-1;
function trend(d){
  const v=[d.dps2,d.dps1,d.dps]; const mx=Math.max(...v.filter(x=>x!=null),0);
  if(!mx) return D;
  if(LITE) return `<span class="trend" role="img" aria-label="2년 배당 추이" title="전년 12개월 ${v[1]==null?'–':won(v[1])} → 최근 12개월 ${v[2]==null?'–':won(v[2])}">${[v[1],v[2]].map(x=>x==null||x===0?'<i class="z"></i>':`<i style="height:${Math.max(3,Math.round(20*x/mx))}px"></i>`).join("")}</span>`;
  return `<span class="trend" role="img" aria-label="3년 배당 추이" title="전전기 ${v[0]==null?'–':won(v[0])} → 전기 ${v[1]==null?'–':won(v[1])} → 당기 ${v[2]==null?'–':won(v[2])}">${v.map(x=>x==null?'<i class="z"></i>':x===0?'<i class="z"></i>':`<i style="height:${Math.max(3,Math.round(20*x/mx))}px"></i>`).join("")}</span>`;
}
function divView(){
  const mk=document.getElementById("dmk").value, st=document.getElementById("dstreak").checked, up=document.getElementById("dup").checked, sf=document.getElementById("dsafe").checked;
  let rs=ROWS.filter(r=>r.dv&&r.dv.dps>0&&!r.x.some(x=>HARD.some(h=>x.includes(h)))&&(!mk||r.mk===mk)
    &&(!st||r.dv.streak3===3)&&(!up||r.dv.trend==="증가")&&(!sf||(r.dv.payout!=null&&r.dv.payout<=100)));
  const v=r=>dSort==="sc"?r.sc:r.dv[dSort];
  rs.sort((a,b)=>{const x=v(a),y=v(b); if(x==null&&y==null) return 0; if(x==null) return 1; if(y==null) return -1; return (x-y)*dDir;});
  document.getElementById("dcount").textContent=`${rs.length.toLocaleString()}개 종목`;
  document.querySelectorAll("#dtab th[data-dk]").forEach(th=>{ if(th.dataset.dk===dSort) th.setAttribute("aria-sort",dDir>0?"ascending":"descending"); else th.removeAttribute("aria-sort"); });
  document.getElementById("dtb").innerHTML = rs.slice(0,300).map(r=>{const d=r.dv; const w=[...d.warn]; if(r.x.some(x=>x.startsWith("적자"))) w.push("적자");
    return `<tr><td class="l nmcell">${LITE?`<span>${esc(r.n)}</span>`:`<button class="toplink" data-open="${r.c}">${esc(r.n)}</button>`}<small>${r.c} · ${mkname(r.mk)}</small></td><td class="l">${esc(LITE?(d.freq?d.freq+' 배당':''):(r.g||r.fg||''))}</td>
    <td><b>${d.yield_now==null?D:d.yield_now.toFixed(2)}</b></td><td>${d.dps==null?D:(US?Number(d.dps).toLocaleString("en-US",{maximumFractionDigits:2,minimumFractionDigits:2}):Math.round(d.dps).toLocaleString("ko-KR"))}<small style="display:block;color:var(--faint)">${US?'최근 12개월 합':d.year+' 사업연도'}</small></td>
    <td class="c">${trend(d)}</td><td class="col-po ${d.payout>100?'low':''}">${d.payout==null?D:d.payout.toFixed(0)}</td><td class="col-sc">${r.sc==null?'–':num(r.sc,1)+(r.rk?`<small style="display:block;color:var(--faint)">${r.rk}위</small>`:'')}</td>
    <td class="l">${w.length?`<span class="chip">${esc(w.join(' · '))}</span>`:''}</td></tr>`;}).join("") || '<tr><td colspan="8" class="l dash">조건에 맞는 종목이 없습니다. 위의 선택을 풀어 보세요.</td></tr>';
}
// ── 금융업 탭 ──
function finView(){
  const g=document.getElementById("fgrp");
  if(g.options.length===1){ [...new Set(ROWS.filter(r=>r.fg).map(r=>r.fg))].sort((a,b)=>a.localeCompare(b,"ko")).forEach(x=>{const o=document.createElement("option");o.textContent=x;g.append(o);}); }
  const sel=g.value;
  const rs=ROWS.filter(r=>r.fg&&(!sel||r.fg===sel)).sort((a,b)=>(a.frk??1e9)-(b.frk??1e9)||(b.mc||0)-(a.mc||0));
  document.getElementById("fcount").textContent=`${rs.filter(r=>r.frk).length}개 순위 / ${rs.length}개 종목`;
  document.getElementById("ftb").innerHTML = rs.map(r=>`<tr><td class="c">${r.frk??'<span class="xr">제외</span>'}</td>
    <td class="l nmcell"><button class="toplink" data-open="${r.c}">${esc(r.n)}</button><small>${r.c} · ${mkname(r.mk)}</small></td><td class="l">${esc(r.fg)}</td>
    <td><b>${r.fs2==null?'–':num(r.fs2,1)}</b></td><td>${r.pbr==null?D:num(r.pbr,2)}</td><td>${pctc(r.m.ROE)}</td>
    <td>${r.dv&&r.dv.yield_now!=null?r.dv.yield_now.toFixed(2):D}</td><td>${mcc(r.mc)}</td>
    <td class="l">${(r.fx||[]).length?`<span class="chip x">${esc(r.fx[0])}</span>`:''}</td></tr>`).join("") || '<tr><td colspan="9" class="l dash">금융업 종목이 없습니다.</td></tr>';
}
// 이벤트
document.getElementById("hdr").addEventListener("click",e=>{const b=e.target.closest("button[data-k]"); if(!b) return;
  const k=b.dataset.k; if(sortK===k) sortDir=-sortDir; else {sortK=k; sortDir=(k==="q"||k==="rk"||k==="n"||k==="g"||k==="per"||k==="pbr")?1:-1;} render();});
document.getElementById("rows").addEventListener("click",e=>{
  const st=e.target.closest(".star"); if(st){e.stopPropagation(); const c=st.dataset.w; WATCH.has(c)?WATCH.delete(c):WATCH.add(c); saveWatch(); render(); glance(); return;}
  const rw=e.target.closest(".row"); if(rw) openSheet(rw.dataset.c);});
document.getElementById("rows").addEventListener("keydown",e=>{ if(e.key!=="Enter"&&e.key!==" ") return; const rw=e.target.closest(".row"); if(!rw||e.target.closest(".star")) return; e.preventDefault(); openSheet(rw.dataset.c);});
document.addEventListener("click",e=>{const o=e.target.closest("[data-open]"); if(o){e.preventDefault(); openSheet(o.dataset.open);}});
document.getElementById("sh-close").addEventListener("click",closeSheet);
document.getElementById("sh-star").addEventListener("click",()=>{ if(!openCode) return; const c=openCode; WATCH.has(c)?WATCH.delete(c):WATCH.add(c); saveWatch();
  const st=document.getElementById("sh-star"); st.classList.toggle("on",WATCH.has(c)); st.setAttribute("aria-pressed",WATCH.has(c)); st.setAttribute("aria-label",`관심 종목 ${WATCH.has(c)?'해제':'추가'}`); glance(); render(); });
document.getElementById("sh-nav").addEventListener("click",e=>{const b=e.target.closest("button[data-sec]"); if(!b) return;
  document.querySelectorAll("#sh-nav button").forEach(x=>x.setAttribute("aria-current",x===b));
  const el=document.getElementById("sec-"+b.dataset.sec), body=document.getElementById("sh-body");
  body.scrollTo({top:el.getBoundingClientRect().top-body.getBoundingClientRect().top+body.scrollTop-12, behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth"});});
document.addEventListener("click",e=>{const g=e.target.closest("[data-gsel]"); if(!g) return; document.getElementById("grp").value=g.dataset.gsel; tab("find");});
document.getElementById("scrim").addEventListener("click",closeSheet);
document.addEventListener("keydown",e=>{ if(e.key==="Escape" && openCode) closeSheet(); });
document.getElementById("basis").addEventListener("input",e=>{BASIS=e.target.value; render(); glance();});
["q","mk","grp"].forEach(id=>document.getElementById(id).addEventListener("input",()=>{limit=100;
  document.body.classList.toggle("searching", document.getElementById("q").value.trim()!=="");
  if(document.getElementById("pane-list").hidden || view!=="all") tab("find", true); else render();}));
document.getElementById("gchips").addEventListener("click",e=>{const b=e.target.closest("button[data-g]"); if(!b) return;
  const sel=document.getElementById("grp"); sel.value = sel.value===b.dataset.g ? "" : b.dataset.g; limit=100;
  if(document.getElementById("pane-list").hidden || view!=="all") tab("find", true); else render();});
document.getElementById("more").addEventListener("click",()=>{limit+=200; render();});
document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>tab(b.dataset.view)));
document.addEventListener("click",e=>{const g=e.target.closest("[data-go]"); if(g) tab(g.dataset.go);});
document.querySelector(".seg").addEventListener("click",e=>{const b=e.target.closest("button[data-tb]"); if(!b) return; TB=b.dataset.tb; topView();});
window.addEventListener("hashchange",()=>{ if(!LITE) tab(location.hash.slice(1)||"home"); });
["dmk","dstreak","dup","dsafe"].forEach(id=>document.getElementById(id).addEventListener("input",divView));
document.getElementById("fgrp").addEventListener("input",finView);
document.querySelector("#dtab thead").addEventListener("click",e=>{const th=e.target.closest("th[data-dk]"); if(!th) return; const k=th.dataset.dk; if(dSort===k) dDir=-dDir; else {dSort=k; dDir=-1;} divView();});
document.getElementById("csv").addEventListener("click",()=>{
  const headr=["순위","전날순위","종목코드","종목명","시장","사업군","비교n","종합","가치","퀄리티","추세","안정성","PER","PBR","ROE","12-1모멘텀","시총(억)","주 제외사유","제외사유 전체","경고","재무기준","공시일"];
  const lines=[headr].concat(filtered().map(r=>[r.rk??"",r.prk??"",r.c,r.n,r.mk,r.g||"",r.gn??"",r.sc??"",...r.b.map(v=>v??""),r.per??"",r.pbr??"",r.m.ROE??"",r.m["12-1모멘텀"]??"",r.mc?Math.round(r.mc/1e8):"",r.xp||"",r.x.join(" / "),r.w.join(" / "),r.pd||"",r.fd||""]));
  const csv="﻿"+lines.map(l=>l.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
  try{const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"})); a.download=`screener_${S.asof_price}.csv`; a.click();}catch(e){}
});
marketSetup(); head(); glance();
if(LITE) tab("div"); else tab(location.hash.slice(1)||"home", true);
</script>
"""
