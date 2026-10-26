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


def build_view(results: list[dict], summary: dict, out_dir: Path) -> None:
    rows = [compact(r) for r in results]
    rows.sort(key=lambda x: (x["rk"] is None, x["rk"] or 0, -(x["mc"] or 0)))
    data = {"summary": summary, "rows": rows, "med": group_medians([x for x in rows if not x["x"]])}
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_text(out_dir / "screen.json", json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    page = TEMPLATE.replace("/*__DATA__*/null", js.replace("</", "<\\/"))
    write_text(out_dir / "screener.html", page)
    # 배포용 완전한 문서: 저장소의 site/index.html → Vercel이 GitHub 연동으로 자동 배포 (다른 앱들과 같은 방식, 토큰 불필요)
    write_text(out_dir.parent.parent / "site" / "index.html",
               '<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
               '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
               '<meta name="robots" content="noindex">\n</head>\n<body>\n' + page + '\n</body>\n</html>\n')


TEMPLATE = r"""<title>업종 대비 저평가 후보</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Gowun+Batang:wght@700&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">
<style>
/* 구조: 머리(제목·상태) → 오늘 한눈에 → 탭(전체 순위·관심 종목·사업군) → 목록(PC는 줄, 휴대폰은 카드) → 상세 시트(PC 오른쪽, 휴대폰 아래에서)
   시그니처: '업종 내 위치' 띠 — 같은 사업군 종목들의 종합점수 눈금 위에 이 종목의 자리 */
:root{
  --paper:#f2f5f4; --surface:#ffffff; --ink:#16201c; --muted:#5b6964; --faint:#8a9893; --line:#d9e1de; --soft:#e9eeec;
  --accent:#0d6b5b; --accent-ink:#ffffff; --accent-soft:#dcede8;
  --up:#c8322b; --down:#2559b8; --warn:#9c6300; --warn-soft:#f8eed8; --bad:#a3262b; --bad-soft:#f7e3e2;
  --shadow:0 18px 40px rgba(18,32,28,.16);
  --f-body:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;
  --f-title:"Gowun Batang","Noto Serif KR",serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0e1412; --surface:#151d1a; --ink:#e2ebe7; --muted:#9aaaa4; --faint:#6f7f79; --line:#26322e; --soft:#1c2622;
  --accent:#5cc2aa; --accent-ink:#0e1412; --accent-soft:#183129;
  --up:#ef6b62; --down:#6f9cf0; --warn:#e2b04f; --warn-soft:#2f2710; --bad:#f08a86; --bad-soft:#381c1d;
  --shadow:0 18px 40px rgba(0,0,0,.5); color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#0e1412; --surface:#151d1a; --ink:#e2ebe7; --muted:#9aaaa4; --faint:#6f7f79; --line:#26322e; --soft:#1c2622;
  --accent:#5cc2aa; --accent-ink:#0e1412; --accent-soft:#183129;
  --up:#ef6b62; --down:#6f9cf0; --warn:#e2b04f; --warn-soft:#2f2710; --bad:#f08a86; --bad-soft:#381c1d;
  --shadow:0 18px 40px rgba(0,0,0,.5); color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--f-body);font-size:14px;line-height:1.5;font-variant-numeric:tabular-nums;word-break:keep-all}
[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{font:inherit;color:inherit}
.wrap{max-width:1180px;margin:0 auto;padding:22px max(16px,env(safe-area-inset-right,0px)) 56px max(16px,env(safe-area-inset-left,0px));display:grid;gap:16px}

/* 머리 */
.head{display:grid;gap:6px}
.title-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px}
h1{font-family:var(--f-title);font-size:27px;line-height:1.2;margin:0;letter-spacing:-.01em}
.head a{color:var(--accent);font-size:13px}
.lede{color:var(--muted);font-size:13.5px;margin:0}
.state{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;font-size:12.5px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px}
.pill.ok{background:var(--accent-soft);color:var(--accent)}
.pill.wait{background:var(--warn-soft);color:var(--warn)}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor}
.state b{color:var(--ink);font-weight:600}
.caution{font-size:12.5px;color:var(--muted);border-left:3px solid var(--warn);padding:2px 0 2px 10px}
.caution strong{color:var(--warn)}
.caution details{display:inline}
.caution summary{display:inline;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.caution summary::-webkit-details-marker{display:none}
.caution .more{display:block;margin-top:4px}

/* 오늘 한눈에 */
.glance{display:grid;grid-template-columns:1.25fr 1fr 1fr;background:var(--surface);border:1px solid var(--line);border-radius:12px}
.glance section{padding:14px 16px;min-width:0}
.glance section+section{border-left:1px solid var(--line)}
.glance h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 8px}
.top5{list-style:none;margin:0;padding:0;display:grid;gap:4px}
.top5 li{display:grid;grid-template-columns:18px 1fr auto auto;gap:8px;align-items:baseline}
.top5 .no{color:var(--faint);font-size:12px}
.top5 button,.mini button{background:none;border:0;padding:0;text-align:left;cursor:pointer;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top5 button:hover,.mini button:hover{color:var(--accent)}
.top5 .s{font-weight:700;color:var(--accent)}
.mini{display:grid;gap:5px;font-size:13px}
.mini .r{display:flex;justify-content:space-between;gap:8px}
.mini .lab{color:var(--muted)}
.empty-note{color:var(--faint);font-size:12.5px}

/* 탭·필터 */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line)}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;padding:9px 14px;color:var(--muted);cursor:pointer;font-weight:500}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.tabs .cnt{font-size:11.5px;color:var(--faint);margin-left:4px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.controls input[type=search],.controls select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 11px;min-width:0}
.controls input[type=search]{flex:1 1 220px}
.controls label{display:flex;gap:6px;align-items:center;color:var(--muted);font-size:13px;cursor:pointer}
.ghost{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:7px 12px;color:var(--accent);cursor:pointer;font-size:13px}
.count{margin-left:auto;color:var(--muted);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12px;color:var(--muted)}
.legend svg{vertical-align:middle;margin-right:4px}

/* 목록: PC는 표처럼 줄, 휴대폰은 카드 */
.list{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.grid{display:grid;grid-template-columns:58px minmax(170px,1fr) 150px 62px 132px 58px 58px 64px 68px 84px 104px;align-items:center;column-gap:10px;padding:0 14px}
.hrow{background:var(--soft);font-size:12px;color:var(--muted);font-weight:600;min-height:38px;position:sticky;top:0;z-index:1}
.hrow button{background:none;border:0;padding:0;cursor:pointer;color:inherit;font-weight:inherit;text-align:inherit}
.hrow button[aria-sort]{color:var(--ink)}
.hrow button[aria-sort="ascending"]::after{content:" ▲";font-size:9px}
.hrow button[aria-sort="descending"]::after{content:" ▼";font-size:9px}
.r{text-align:right} .c{text-align:center}
.row{min-height:56px;border-top:1px solid var(--soft);cursor:pointer;background:none;width:100%;border-left:0;border-right:0;border-bottom:0;text-align:left;font-size:13.5px}
.row:hover{background:var(--soft)}
.row.sel{background:var(--accent-soft)}
.rk{display:flex;flex-direction:column;align-items:center;line-height:1.2}
.rk b{font-size:14px;font-weight:600}
.chg{font-size:11px;font-weight:600}
.chg.up{color:var(--up)} .chg.down{color:var(--down)} .chg.new{color:var(--accent)} .chg.flat{color:var(--faint)}
.xr{color:var(--bad);font-size:12px;font-weight:600}
.nm{display:flex;align-items:center;gap:6px;min-width:0}
.nm .t{min-width:0}
.nm .n{font-weight:600;font-size:14px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm .cd{display:block;color:var(--muted);font-size:11.5px}
.star{background:none;border:0;padding:2px;cursor:pointer;font-size:16px;line-height:1;color:var(--line);flex:none}
.star.on{color:#d39a00}
.grp{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grp small{color:var(--faint);margin-left:4px}
.grp small.low{color:var(--warn)}
.sc{font-weight:700;font-size:15px;color:var(--accent);text-align:right}
.sc.muted{color:var(--faint);font-weight:500}
.up{color:var(--up)} .down{color:var(--down)} .dash{color:var(--faint)}
.chip{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle;font-size:11.5px;padding:1px 8px;border-radius:999px;background:var(--warn-soft);color:var(--warn);font-weight:500}
.chip.x{background:var(--bad-soft);color:var(--bad)}
.strip{display:block;width:132px;height:22px}
.strip .base{stroke:var(--line)} .strip .tick{stroke:var(--faint);opacity:.55} .strip .me{fill:var(--accent);stroke:var(--surface);stroke-width:2}
.strip .me.muted{fill:var(--faint)}
.more{justify-self:center;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:9px 20px;color:var(--accent);cursor:pointer}
.emptybox{padding:28px 18px;color:var(--muted);text-align:center}
.emptybox b{display:block;color:var(--ink);margin-bottom:4px}

/* 사업군 표 */
table.gt{border-collapse:collapse;width:100%;font-size:13.5px}
.gt th,.gt td{padding:9px 12px;border-bottom:1px solid var(--soft);text-align:right;white-space:nowrap}
.gt th{background:var(--soft);color:var(--muted);font-weight:600;font-size:12px}
.gt .l{text-align:left}
.gt tbody tr:hover{background:var(--soft)}
.gt .toplink{background:none;border:0;padding:0;cursor:pointer;text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
.gt .low{color:var(--warn)}
.gwrap{overflow-x:auto}

/* 상세 시트: PC는 오른쪽 서랍, 휴대폰은 아래에서 올라옴 */
.scrim{position:fixed;inset:0;background:rgba(10,20,17,.28);z-index:20}
.sheet{position:fixed;z-index:21;background:var(--surface);box-shadow:var(--shadow);display:flex;flex-direction:column;
  top:0;right:0;bottom:0;width:min(600px,100vw);border-left:1px solid var(--line);transform:translateX(0);transition:transform .22s ease}
.sheet.closed{transform:translateX(104%)}
.sh-head{display:flex;align-items:flex-start;gap:10px;padding:16px 18px 12px;border-bottom:1px solid var(--line)}
.sh-head .t{flex:1;min-width:0}
.sh-head h3{font-family:var(--f-title);font-size:22px;margin:0;line-height:1.25}
.sh-head .cd{color:var(--muted);font-size:12.5px}
.sh-head .big{font-size:26px;font-weight:700;color:var(--accent);line-height:1}
.sh-head .big.muted{color:var(--faint)}
.sh-head .big small{display:block;font-size:11.5px;font-weight:500;color:var(--muted);text-align:right;margin-top:4px}
.close{background:none;border:1px solid var(--line);border-radius:8px;width:34px;height:34px;cursor:pointer;font-size:18px;line-height:1;color:var(--muted);flex:none}
.sh-body{overflow-y:auto;padding:14px 18px 28px;display:grid;gap:18px;overscroll-behavior:contain}
.sh-body h4{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 8px}
.why{display:grid;gap:3px;font-size:14px;line-height:1.6}
.why .pos{color:var(--accent);font-weight:600} .why .neg{color:var(--warn);font-weight:600}
.why .note{color:var(--muted);font-size:12.5px}
.bk{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.bk div{background:var(--soft);border-radius:8px;padding:8px 10px}
.bk span{display:block;font-size:12px;color:var(--muted)}
.bk b{font-size:18px}
.bk .bar{height:4px;border-radius:2px;background:var(--line);margin-top:6px;position:relative;overflow:hidden}
.bk .bar i{position:absolute;inset:0 auto 0 0;background:var(--accent)}
table.mt{border-collapse:collapse;width:100%;font-size:13px}
.mt th,.mt td{padding:7px 6px;border-bottom:1px solid var(--soft);text-align:right}
.mt th{color:var(--muted);font-weight:600;font-size:12px}
.mt .l{text-align:left}
.mt small{color:var(--faint);font-size:10.5px;margin-left:3px}
.pc{display:inline-block;width:52px;height:5px;border-radius:3px;background:var(--soft);vertical-align:middle;margin-left:6px;position:relative}
.pc i{position:absolute;inset:0 auto 0 0;border-radius:3px;background:var(--accent)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px;margin:0}
.kv dt{color:var(--muted)} .kv dd{margin:0}
.lst{margin:0;padding-left:18px;display:grid;gap:3px;font-size:13px}
.sh-body details summary{cursor:pointer;color:var(--muted);font-size:12.5px}
footer{color:var(--muted);font-size:12px;display:grid;gap:4px}

@media (max-width:860px){
  .glance{grid-template-columns:1fr}
  .glance section+section{border-left:0;border-top:1px solid var(--line)}
}
/* 휴대폰: 줄 → 카드 */
@media (max-width:720px){
  h1{font-size:23px}
  .wrap{gap:14px;padding-top:16px}
  .hrow{display:none}
  .legend span+span{display:none}
  .controls select{flex:1 1 40%}
  .controls input[type=search]{flex-basis:100%}
  .count{margin-left:0}
  .grid{grid-template-columns:30px 1fr auto;grid-template-areas:"rk nm sc" "rk meta strip" "rk nums nums";column-gap:10px;row-gap:3px;padding:11px 12px}
  .grid>*{min-width:0}
  .g-rk{grid-area:rk;align-self:start;padding-top:2px}
  .g-nm{grid-area:nm} .g-sc{grid-area:sc}
  .g-grp{grid-area:meta;font-size:12.5px}
  .g-strip{grid-area:strip;justify-self:end}
  .g-strip .strip{width:110px}
  .g-per,.g-pbr,.g-roe,.g-mom,.g-mc,.g-flag{display:none}
  .g-nums{grid-area:nums;display:flex!important;flex-wrap:wrap;gap:2px 12px;font-size:12.5px;color:var(--muted)}
  .g-nums b{color:var(--ink);font-weight:500}
  .sheet{top:auto;left:0;right:0;width:auto;height:92vh;border-left:0;border-top:1px solid var(--line);border-radius:16px 16px 0 0;transform:translateY(0)}
  .sheet.closed{transform:translateY(104%)}
  .sh-head::before{content:"";position:absolute;left:50%;top:6px;width:38px;height:4px;border-radius:2px;background:var(--line);transform:translateX(-50%)}
  .sh-head{position:relative;padding-top:18px}
  .bk{grid-template-columns:repeat(4,1fr);gap:6px} .bk div{padding:6px 8px} .bk b{font-size:16px}
  .mt .pc{display:none} .mt td small{display:block;margin-left:0}
  .g-nums{gap:2px 10px;font-size:12px}
}
.g-nums{display:none}
.m-flag{display:none}
@media (max-width:720px){.m-flag{display:inline}}
@media (prefers-reduced-motion:reduce){.sheet{transition:none}}
</style>

<div class="wrap">
  <header class="head">
    <div class="title-row">
      <h1>업종 대비 저평가 후보</h1>
      <a href="backtest.html">과거 성과 검증 보기</a>
    </div>
    <p class="lede">같은 업종 회사들과 비교해 싸고, 실적이 괜찮고, 흐름이 좋은 종목을 찾는 후보 목록입니다. 매수 목록이 아닙니다.</p>
    <div class="state" id="state"></div>
    <div class="caution"><strong>점수는 가설입니다.</strong> 과거 성과로 아직 검증되지 않았습니다.
      <details><summary>점수 계산 방법</summary><span class="more" id="method"></span></details></div>
  </header>

  <div class="glance" id="glance">
    <section><h2>오늘의 상위 후보</h2><ol class="top5" id="top5"></ol></section>
    <section><h2 id="chg-title">어제 대비 변화</h2><div class="mini" id="chg"></div></section>
    <section><h2>관심 종목</h2><div class="mini" id="wsum"></div></section>
  </div>

  <div class="tabs" role="tablist">
    <button role="tab" id="tab-all" aria-selected="true">전체 순위</button>
    <button role="tab" id="tab-watch" aria-selected="false">관심 종목<span class="cnt" id="wcnt"></span></button>
    <button role="tab" id="tab-grp" aria-selected="false">사업군</button>
  </div>

  <div id="pane-list">
    <div class="controls">
      <input type="search" id="q" placeholder="종목명 또는 코드" aria-label="종목 검색">
      <select id="mk" aria-label="시장"><option value="">전체 시장</option><option value="KOSPI">코스피</option><option value="KOSDAQ">코스닥</option></select>
      <select id="grp" aria-label="사업군"><option value="">전체 사업군</option></select>
      <label><input type="checkbox" id="showx"> 순위 제외 종목 보기</label>
      <button class="ghost" id="csv">CSV 받기</button>
      <span class="count" id="count"></span>
    </div>
    <div class="legend" style="margin:10px 0 8px">
      <span><svg width="46" height="14" aria-hidden="true"><line x1="2" y1="7" x2="44" y2="7" stroke="currentColor" opacity=".3"/><line x1="10" y1="3" x2="10" y2="11" stroke="currentColor" opacity=".5"/><line x1="20" y1="3" x2="20" y2="11" stroke="currentColor" opacity=".5"/><line x1="27" y1="3" x2="27" y2="11" stroke="currentColor" opacity=".5"/><circle cx="37" cy="7" r="4.5" fill="var(--accent)"/></svg>업종 내 위치: 눈금은 같은 사업군 종목들의 종합점수(왼쪽 최저·오른쪽 최고), 점이 이 종목</span>
      <span>사업군 옆 숫자는 비교 대상 수</span>
      <span>PER·PBR은 지배주주 기준</span>
    </div>
    <div class="list" role="table" aria-label="종목 순위">
      <div class="grid hrow" role="row" id="hdr"></div>
      <div id="rows" role="rowgroup"></div>
    </div>
    <div style="display:grid;margin-top:12px"><button class="more" id="more" hidden>더 보기</button></div>
  </div>

  <div id="pane-grp" hidden>
    <div class="list gwrap"><table class="gt"><thead><tr><th class="l">사업군</th><th>비교 대상</th><th>순위 대상</th><th>신뢰도</th><th>PER 중앙값</th><th>PBR 중앙값</th><th>ROE 중앙값</th><th class="l">사업군 1위</th><th>점수</th></tr></thead><tbody id="gtb"></tbody></table></div>
  </div>

  <footer>
    <div>자료: KRX 정보데이터시스템 OPEN API(시세), OpenDART(재무). 재무는 최근 4개 분기 합(TTM), 연결재무제표 우선.</div>
    <div>사업군: KSIC 업종코드를 묶은 고정 표(v1). 비교 대상이 10개 미만인 사업군은 순위에서 제외합니다.</div>
  </footer>
</div>

<div class="scrim" id="scrim" hidden></div>
<aside class="sheet closed" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sh-name" hidden>
  <div class="sh-head">
    <div class="t"><h3 id="sh-name"></h3><div class="cd" id="sh-cd"></div></div>
    <div class="big" id="sh-big"></div>
    <button class="close" id="sh-close" aria-label="닫기">×</button>
  </div>
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
  {k:"rk",t:"순위",cls:"c g-rk"},{k:"n",t:"종목",cls:"g-nm"},{k:"g",t:"사업군",cls:"g-grp"},{k:"sc",t:"종합",cls:"r g-sc"},
  {k:"strip",t:"업종 내 위치",cls:"g-strip",ns:1},{k:"per",t:"PER",cls:"r g-per"},{k:"pbr",t:"PBR",cls:"r g-pbr"},
  {k:"roe",t:"ROE",cls:"r g-roe"},{k:"mom",t:"모멘텀",cls:"r g-mom"},{k:"mc",t:"시총",cls:"r g-mc"},{k:"flag",t:"비고",cls:"c g-flag",ns:1}
];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const D = '<span class="dash">–</span>';
const num = (v,d=1) => v==null ? D : v.toLocaleString("ko-KR",{maximumFractionDigits:d,minimumFractionDigits:d});
const pct = (v,d=1,color=false) => v==null ? D : `<span class="${color?(v>0?'up':v<0?'down':''):''}">${(v*100).toFixed(d)}%</span>`;
const mcap = v => v==null ? D : v>=1e12 ? `${(v/1e12).toLocaleString("ko-KR",{maximumFractionDigits:1,minimumFractionDigits:1})}조` : `${Math.round(v/1e8).toLocaleString()}억`;
const medv = x => Array.isArray(x) ? x[0] : (x ?? null);
const medn = x => Array.isArray(x) ? x[1] : null;
const dfmt = d => d ? (d.includes("-") ? d : `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}`) : null;
const mkname = m => m==="KOSPI"?"코스피":"코스닥";
function fmtMetric(k,v){
  if(v==null) return D;
  if(k==="PBR-ROE잔차"){const d=Math.exp(-v)-1; return `<span class="${d<0?'down':'up'}">${(d*100).toFixed(0)}%</span>`;}
  if(PCTM.has(k)) return pct(v,1,CHANGE.has(k));
  if(k==="이자보상배율") return v>=100?"100배+":num(v,1)+"배";
  return num(v,2)+"배";
}
// 같은 사업군 종합점수 (순위 대상 기준) — 업종 내 위치 띠
const PEERS = {};
ROWS.forEach(r=>{ if(r.rk && r.gid){ (PEERS[r.gid]=PEERS[r.gid]||[]).push(r.sc); } });
function strip(r, W=132, H=22){
  // 가로축 = 같은 사업군 순위 대상 종목의 최저~최고 종합점수 (이 종목 포함)
  const peers=PEERS[r.gid]||[], pad=6, all=peers.concat(r.sc==null?[]:[r.sc]);
  const lo=all.length?Math.min(...all):0, hi=all.length?Math.max(...all):100, span=Math.max(hi-lo,1e-9);
  const xs=v=>pad+(W-2*pad)*(v-lo)/span;
  const y=H/2, sty=W>200?` style="width:100%;height:${H}px"`:'';
  if(r.sc==null||!peers.length) return `<svg class="strip" viewBox="0 0 ${W} ${H}"${sty} aria-hidden="true"><line class="base" x1="${pad}" y1="${y}" x2="${W-pad}" y2="${y}"/></svg>`;
  const ticks=peers.map(v=>`<line class="tick" x1="${xs(v).toFixed(1)}" y1="${y-5}" x2="${xs(v).toFixed(1)}" y2="${y+5}"/>`).join("");
  const below=peers.filter(v=>v<r.sc).length;
  return `<svg class="strip" viewBox="0 0 ${W} ${H}"${sty} role="img" aria-label="${esc(r.g||'')} 사업군 순위 대상 ${peers.length}개 중 이 종목보다 점수 낮은 종목 ${below}개"><line class="base" x1="${pad}" y1="${y}" x2="${W-pad}" y2="${y}"/>${ticks}<circle class="me ${r.rk?'':'muted'}" cx="${xs(r.sc).toFixed(1)}" cy="${y}" r="5"/></svg>`;
}
function chg(r){
  if(!S.prev_asof || !r.rk) return "";
  if(r.pk===false || r.prk==null) return `<span class="chg new">신규</span>`;
  const d=r.prk-r.rk;
  return d>0?`<span class="chg up">▲${d}</span>`:d<0?`<span class="chg down">▼${-d}</span>`:`<span class="chg flat">–</span>`;
}
let sortK="rk", sortDir=1, limit=150, view="all", openCode=null, lastFocus=null;
let WATCH=new Set(); try{WATCH=new Set(JSON.parse(localStorage.getItem("qs-watch")||"[]"));}catch(e){}
function saveWatch(){try{localStorage.setItem("qs-watch",JSON.stringify([...WATCH]));}catch(e){}}
const val = (r,k) => k==="roe"?r.m.ROE : k==="mom"?r.m["12-1모멘텀"] : k==="g"?(r.g||"") : k==="n"?(r.n||"") : r[k];

function head(){
  const d=S.asof_price, ds=d?dfmt(d):"";
  const st=S.status||{}, w=(st.warnings||[]).filter(x=>!x.startsWith("재무 수집 진행 중"));
  const h=st.history;
  const pill = !S.collection_complete
    ? `<span class="pill wait"><i></i>잠정 순위 · 재무 수집 ${S.universe_collected.toLocaleString()}/${S.universe_total.toLocaleString()}개사</span>`
    : w.length ? `<span class="pill wait"><i></i>확인 필요 ${w.length}건</span>` : `<span class="pill ok"><i></i>오늘 갱신됨</span>`;
  document.getElementById("state").innerHTML = pill +
    `<span>시세 기준 <b>${ds}</b></span><span>순위 <b>${S.ranked.toLocaleString()}</b>개 / 평가 ${S.evaluated.toLocaleString()}개사</span>` +
    (h&&h.targets&&!h.complete?`<span>과거 재무 수집 ${Math.round(100*h.processed/h.targets)}% (백테스트 준비)</span>`:"") +
    (w.length?`<span>${w.map(esc).join(" / ")}</span>`:"");
  const ka=st.kind_admin;
  document.getElementById("method").textContent = "같은 사업군 안에서 지표마다 백분위를 매기고, 가치 40%·퀄리티 30%·추세 20%·안정성 10%로 합칩니다. " +
    (ka?`관리종목은 코스닥 KRX 소속부와 코스피 KIND 목록(${dfmt(ka.date)}) 기준으로 제외합니다. `:"") + "자료가 없는 지표는 빈칸으로 두고 점수를 채워 넣지 않습니다.";
  const gs=[...new Set(ROWS.filter(r=>r.g&&r.gid!=="EX").map(r=>r.g))].sort((a,b)=>a.localeCompare(b,"ko"));
  const sel=document.getElementById("grp"); gs.forEach(g=>{const o=document.createElement("option");o.textContent=g;sel.append(o);});
}
function glance(){
  const ranked=ROWS.filter(r=>r.rk).sort((a,b)=>a.rk-b.rk);
  document.getElementById("top5").innerHTML = ranked.slice(0,5).map(r=>`<li><span class="no">${r.rk}</span><button data-open="${r.c}">${esc(r.n)}</button><span class="s">${r.sc.toFixed(1)}</span><span>${chg(r)}</span></li>`).join("");
  const box=document.getElementById("chg");
  if(!S.prev_asof){ document.getElementById("chg-title").textContent="순위 변화";
    box.innerHTML='<span class="empty-note">비교할 전날 기록이 쌓이면 여기에 새로 들어온 종목과 빠진 종목이 보입니다.</span>'; }
  else {
    document.getElementById("chg-title").textContent=`${dfmt(S.prev_asof)} 대비 변화`;
    const top=ranked.filter(r=>r.rk<=30), neu=top.filter(r=>r.pk===false||r.prk==null||r.prk>30);
    const out=ROWS.filter(r=>r.prk!=null&&r.prk<=30&&(!r.rk||r.rk>30));
    const up=ranked.filter(r=>r.prk!=null).sort((a,b)=>(b.prk-b.rk)-(a.prk-a.rk))[0];
    box.innerHTML = `<div class="r"><span class="lab">30위 안 새로 진입</span><span>${neu.length?neu.slice(0,3).map(r=>`<button data-open="${r.c}">${esc(r.n)}</button>`).join(", ")+(neu.length>3?` 외 ${neu.length-3}`:""):"없음"}</span></div>`+
      `<div class="r"><span class="lab">30위 밖으로</span><span>${out.length?out.slice(0,3).map(r=>`<button data-open="${r.c}">${esc(r.n)}</button>`).join(", ")+(out.length>3?` 외 ${out.length-3}`:""):"없음"}</span></div>`+
      (up&&up.prk-up.rk>0?`<div class="r"><span class="lab">가장 많이 오름</span><span><button data-open="${up.c}">${esc(up.n)}</button> <span class="chg up">▲${up.prk-up.rk}</span></span></div>`:"");
  }
  const ws=[...WATCH].map(c=>BY[c]).filter(Boolean);
  document.getElementById("wcnt").textContent = ws.length?ws.length:"";
  const wb=document.getElementById("wsum");
  if(!ws.length){ wb.innerHTML='<span class="empty-note">종목 옆 ★를 누르면 여기 모입니다. 순위가 바뀌면 바로 보입니다.</span>'; return; }
  const best=ws.filter(r=>r.rk).sort((a,b)=>a.rk-b.rk)[0];
  const mv=S.prev_asof?ws.filter(r=>r.rk&&r.prk!=null&&r.prk!==r.rk).sort((a,b)=>Math.abs(b.prk-b.rk)-Math.abs(a.prk-a.rk))[0]:null;
  const xs=ws.filter(r=>r.x.length).length;
  wb.innerHTML = `<div class="r"><span class="lab">모아 둔 종목</span><span>${ws.length}개${xs?` (순위 제외 ${xs})`:""}</span></div>`+
    (best?`<div class="r"><span class="lab">가장 높은 순위</span><span><button data-open="${best.c}">${esc(best.n)}</button> ${best.rk}위</span></div>`:"")+
    (mv?`<div class="r"><span class="lab">가장 크게 움직임</span><span><button data-open="${mv.c}">${esc(mv.n)}</button> ${chg(mv)}</span></div>`:"");
}
function header(){
  document.getElementById("hdr").innerHTML = COLS.map(c=>`<div class="${c.cls}" role="columnheader">${c.ns?c.t:`<button data-k="${c.k}" ${sortK===c.k?`aria-sort="${sortDir>0?'ascending':'descending'}"`:''}>${c.t}</button>`}</div>`).join("");
}
function filtered(){
  const q=document.getElementById("q").value.trim().toLowerCase(), mk=document.getElementById("mk").value,
        g=document.getElementById("grp").value, sx=document.getElementById("showx").checked;
  let rs=ROWS.filter(r=>(view==="watch"? WATCH.has(r.c) : (sx? r.x.length>0 : r.x.length===0)) && (!mk||r.mk===mk) && (!g||r.g===g) &&
    (!q || (r.n||"").toLowerCase().includes(q) || r.c.includes(q)));
  rs.sort((a,b)=>{const x=val(a,sortK), y=val(b,sortK);
    if(x==null&&y==null) return 0; if(x==null) return 1; if(y==null) return -1;
    return (typeof x==="string" ? x.localeCompare(y,"ko") : x-y)*sortDir;});
  return rs;
}
function flag(r){
  return r.x.length ? `<span class="chip x" title="${esc(r.x.join(' · '))}">${esc(r.xp||r.x[0])}${r.x.length>1?` +${r.x.length-1}`:''}</span>`
       : r.w.length ? `<span class="chip" title="${esc(r.w.join(' · '))}">경고 ${r.w.length}</span>` : '';
}
function row(r){
  return `<div class="grid row ${openCode===r.c?'sel':''}" role="row" tabindex="0" data-c="${r.c}">
    <div class="g-rk rk" role="cell">${r.rk?`<b>${r.rk}</b>${chg(r)}`:(r.x.length?'<span class="xr">제외</span>':D)}</div>
    <div class="g-nm nm" role="cell"><button class="star ${WATCH.has(r.c)?'on':''}" data-w="${r.c}" aria-label="관심 종목 ${WATCH.has(r.c)?'해제':'추가'}" aria-pressed="${WATCH.has(r.c)}">★</button><span class="t"><span class="n">${esc(r.n)}</span><span class="cd">${r.c} · ${mkname(r.mk)}</span></span></div>
    <div class="g-grp grp" role="cell" title="${esc(r.g||'')}${r.gn?` · 비교 대상 ${r.gn}개`:''}">${esc(r.g||'미배정')}${r.gn?`<small class="${r.rel==='낮음'?'low':''}">${r.gn}</small>`:''}${flag(r)?`<span class="m-flag"> ${flag(r)}</span>`:''}</div>
    <div class="g-sc sc ${r.rk?'':'muted'}" role="cell">${num(r.sc,1)}</div>
    <div class="g-strip" role="cell">${strip(r)}</div>
    <div class="g-per r" role="cell">${r.per==null?D:num(r.per,1)}</div><div class="g-pbr r" role="cell">${r.pbr==null?D:num(r.pbr,2)}</div>
    <div class="g-roe r" role="cell">${pct(r.m.ROE)}</div><div class="g-mom r" role="cell">${pct(r.m["12-1모멘텀"],0,true)}</div>
    <div class="g-mc r" role="cell">${mcap(r.mc)}</div><div class="g-flag c" role="cell">${flag(r)}</div>
    <div class="g-nums" role="cell"><span>PER <b>${r.per==null?'–':num(r.per,1)}</b></span><span>PBR <b>${r.pbr==null?'–':num(r.pbr,2)}</b></span><span>ROE <b>${r.m.ROE==null?'–':(r.m.ROE*100).toFixed(1)+'%'}</b></span><span>시총 <b>${mcap(r.mc)}</b></span></div>
  </div>`;
}
function render(){
  header();
  const rs=filtered(), shown=rs.slice(0,limit);
  const box=document.getElementById("rows");
  if(!rs.length){
    box.innerHTML = view==="watch" && !WATCH.size
      ? '<div class="emptybox"><b>아직 관심 종목이 없습니다</b>전체 순위에서 종목 이름 옆 ★를 누르면 여기에 모입니다.</div>'
      : '<div class="emptybox"><b>조건에 맞는 종목이 없습니다</b>검색어나 시장·사업군 선택을 바꿔 보세요.</div>';
  } else box.innerHTML = shown.map(row).join("");
  document.getElementById("count").textContent = `${rs.length.toLocaleString()}개 종목`;
  const more=document.getElementById("more"); more.hidden = rs.length<=limit; more.textContent=`더 보기 (${(rs.length-limit).toLocaleString()}개 남음)`;
}
function why(r){
  const ks=Object.keys(LABEL).filter(k=>r.p[k]!=null);
  const strong=ks.filter(k=>r.p[k]>=80).sort((a,b)=>r.p[b]-r.p[a]).slice(0,3);
  const weak=ks.filter(k=>r.p[k]<=30).sort((a,b)=>r.p[a]-r.p[b]).slice(0,2);
  const miss=Object.keys(LABEL).filter(k=>r.p[k]==null).map(k=>LABEL[k]);
  const bi=r.b.map((v,i)=>[v,i]).filter(x=>x[0]!=null).sort((a,b)=>b[0]-a[0]);
  const nv=Object.keys(LABEL).filter(k=>r.m[k]!=null).length;
  const L=[];
  L.push(r.rk ? `<b>전체 ${r.rk}위</b>, ${esc(r.g)} 사업군 ${r.gn}개 중 ${r.grk}위입니다.`
        : r.x.length ? `<b>순위 제외</b>: ${esc(r.xpf||r.x[0])}${r.x.length>1?` 외 ${r.x.length-1}건`:''}` : '<b>점수 없음</b>');
  if(bi.length) L.push(`가장 높은 묶음은 <b>${BK[bi[0][1]]}</b>(${bi[0][0].toFixed(0)}점)${bi.length>1?`, 가장 낮은 묶음은 <b>${BK[bi[bi.length-1][1]]}</b>(${bi[bi.length-1][0].toFixed(0)}점)`:''}입니다.`);
  if(strong.length) L.push(`<span class="pos">강점</span> ${strong.map(k=>`${LABEL[k]} 사업군 상위 ${Math.max(1,Math.round(100-r.p[k]))}%`).join(", ")}`);
  if(weak.length) L.push(`<span class="neg">약점</span> ${weak.map(k=>`${LABEL[k]} 사업군 하위 ${Math.max(1,Math.round(r.p[k]))}%`).join(", ")}`);
  const notes=[`지표 ${nv}/10개 계산`];
  if(miss.length) notes.push(`자료 없음: ${miss.join(", ")}`);
  if(r.rel==="낮음") notes.push(`비교 대상이 ${r.gn}개뿐이라 백분위가 거칩니다`);
  L.push(`<span class="note">${esc(notes.join(" · "))}</span>`);
  return `<div class="why">${L.map(l=>`<div>${l}</div>`).join("")}</div>`;
}
function openSheet(c){
  const r=BY[c]; if(!r) return;
  openCode=c; lastFocus=document.activeElement;
  document.getElementById("sh-name").textContent=r.n;
  document.getElementById("sh-cd").innerHTML=`${r.c} · ${mkname(r.mk)} · ${esc(r.g||'미배정')}${r.gn?` (비교 대상 ${r.gn}개)`:''}`;
  document.getElementById("sh-big").className="big"+(r.rk?"":" muted");
  document.getElementById("sh-big").innerHTML = r.sc==null?'–':`${r.sc.toFixed(1)}<small>${r.rk?`전체 ${r.rk}위 ${chg(r)}`:'순위 제외'}</small>`;
  const med=MED[r.gid]||{};
  const mrows=Object.keys(LABEL).map(k=>`<tr><td class="l">${LABEL[k]}<small>${BUCKET[k]}</small></td>
    <td${r.m[k]==null&&(r.mr||{})[k]?` title="${esc(r.mr[k])}"`:''}>${fmtMetric(k,r.m[k])}</td>
    <td>${k==="PBR-ROE잔차"?D:fmtMetric(k,medv(med[k]))+(medn(med[k])?`<small>n=${medn(med[k])}</small>`:'')}</td>
    <td>${r.p[k]==null?D:`${r.p[k].toFixed(0)}<span class="pc"><i style="width:${r.p[k]}%"></i></span>`}</td></tr>`).join("");
  const fb = r.fb.length ? r.fb.map(x=>x.replace("ni_owners","지배주주 순이익").replace("equity_owners","지배주주 자본").replace("separate_only","별도재무제표만 제출 → 전체=지배").replace("no_nci","비지배지분 없음 → 전체=지배")).join(", ") : "표준 계정으로 확인";
  const list=a=>a.length?`<ul class="lst">${a.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:'<span class="dash">없음</span>';
  document.getElementById("sh-body").innerHTML = `${why(r)}
    <div><h4>묶음 점수 (사업군 안 백분위 평균)</h4><div class="bk">${BK.map((b,i)=>`<div><span>${b}</span><b>${r.b[i]==null?'–':r.b[i].toFixed(0)}</b><div class="bar"><i style="width:${r.b[i]??0}%"></i></div></div>`).join("")}</div></div>
    <div><h4>업종 내 위치 (같은 사업군 순위 대상 ${(PEERS[r.gid]||[]).length}개, 왼쪽이 최저점·오른쪽이 최고점)</h4>${strip(r,560,28)}</div>
    <div><h4>지표 — 이 종목 / 사업군 중앙값 / 백분위</h4><table class="mt"><thead><tr><th class="l">지표</th><th>이 종목</th><th>중앙값</th><th>백분위</th></tr></thead><tbody>${mrows}</tbody></table></div>
    ${r.x.length?`<div><h4>순위 제외 사유</h4>${list(r.x)}</div>`:''}
    <div><h4>경고</h4>${list(r.w)}</div>
    <div><h4>데이터</h4><dl class="kv"><dt>재무 기준</dt><dd>${r.pd||'–'} ${r.fs==="CFS"?"연결":r.fs==="OFS"?"별도":""}</dd>
      <dt>공시일</dt><dd>${r.fd?dfmt(r.fd):'미확인'}${r.av?` (점수 반영 ${dfmt(r.av)})`:''}</dd>
      <dt>현재가</dt><dd>${r.px?Number(r.px).toLocaleString()+"원":"–"}</dd><dt>시가총액</dt><dd>${mcap(r.mc)}</dd>
      <dt>20일 평균 거래대금</dt><dd>${r.tv==null?'–':Math.round(r.tv/1e8).toLocaleString()+"억"}</dd><dt>F-스코어</dt><dd>${r.fsc??'–'}</dd></dl>
      <details style="margin-top:8px"><summary>세부 데이터</summary><dl class="kv" style="margin-top:6px"><dt>업종코드</dt><dd>${r.ksic||'–'}</dd><dt>계정 선택</dt><dd>${esc(fb)}</dd>${r.ib?`<dt>이자 기준</dt><dd>${esc(r.ib)}</dd>`:''}</dl></details></div>`;
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
  const med=a=>{a=a.filter(v=>v!=null).sort((x,y)=>x-y); if(!a.length) return null; const m=a.length>>1; return a.length%2?a[m]:(a[m-1]+a[m])/2;};
  const rows=Object.entries(by).map(([g,rs])=>{rs.sort((a,b)=>(a.rk??1e9)-(b.rk??1e9)); return {g,n:rs.length,rel:rs[0].rel,per:med(rs.map(r=>r.per)),pbr:med(rs.map(r=>r.pbr)),roe:med(rs.map(r=>r.m.ROE)),top:rs[0]};}).sort((a,b)=>b.n-a.n);
  document.getElementById("gtb").innerHTML=rows.map(x=>`<tr><td class="l">${esc(x.g)}</td><td>${cmp[x.g]??'–'}</td><td>${x.n}</td><td class="${x.rel==='낮음'?'low':''}">${x.rel||''}</td><td>${x.per==null?'–':num(x.per,1)+'배'}</td><td>${x.pbr==null?'–':num(x.pbr,2)+'배'}</td><td>${pct(x.roe)}</td><td class="l"><button class="toplink" data-open="${x.top.c}">${esc(x.top.n)}</button></td><td>${num(x.top.sc,1)}</td></tr>`).join("")||'<tr><td colspan="9" class="l dash">순위 대상 종목이 없습니다.</td></tr>';
}
function tab(which){
  view = which==="grp" ? view : which;
  ["all","watch","grp"].forEach(t=>document.getElementById("tab-"+t).setAttribute("aria-selected", t===which));
  document.getElementById("pane-list").hidden = which==="grp";
  document.getElementById("pane-grp").hidden = which!=="grp";
  document.getElementById("showx").closest("label").hidden = which==="watch";
  if(which==="grp") groupView(); else { limit=150; render(); }
}
// 이벤트
document.getElementById("hdr").addEventListener("click",e=>{const b=e.target.closest("button[data-k]"); if(!b) return;
  const k=b.dataset.k; if(sortK===k) sortDir=-sortDir; else {sortK=k; sortDir=(k==="rk"||k==="n"||k==="g"||k==="per"||k==="pbr")?1:-1;} render();});
document.getElementById("rows").addEventListener("click",e=>{
  const st=e.target.closest(".star"); if(st){e.stopPropagation(); const c=st.dataset.w; WATCH.has(c)?WATCH.delete(c):WATCH.add(c); saveWatch(); render(); glance(); return;}
  const rw=e.target.closest(".row"); if(rw) openSheet(rw.dataset.c);});
document.getElementById("rows").addEventListener("keydown",e=>{ if(e.key!=="Enter"&&e.key!==" ") return; const rw=e.target.closest(".row"); if(!rw||e.target.closest(".star")) return; e.preventDefault(); openSheet(rw.dataset.c);});
document.addEventListener("click",e=>{const o=e.target.closest("[data-open]"); if(o){e.preventDefault(); openSheet(o.dataset.open);}});
document.getElementById("sh-close").addEventListener("click",closeSheet);
document.getElementById("scrim").addEventListener("click",closeSheet);
document.addEventListener("keydown",e=>{ if(e.key==="Escape" && openCode) closeSheet(); });
["q","mk","grp","showx"].forEach(id=>document.getElementById(id).addEventListener("input",()=>{limit=150;
  if(id==="showx"){const x=document.getElementById("showx").checked; sortK=x?"mc":"rk"; sortDir=x?-1:1;} render();}));
document.getElementById("more").addEventListener("click",()=>{limit+=300; render();});
document.getElementById("tab-all").addEventListener("click",()=>tab("all"));
document.getElementById("tab-watch").addEventListener("click",()=>tab("watch"));
document.getElementById("tab-grp").addEventListener("click",()=>tab("grp"));
document.getElementById("csv").addEventListener("click",()=>{
  const headr=["순위","전날순위","종목코드","종목명","시장","사업군","비교n","종합","가치","퀄리티","추세","안정성","PER","PBR","ROE","12-1모멘텀","시총(억)","주 제외사유","제외사유 전체","경고","재무기준","공시일"];
  const lines=[headr].concat(filtered().map(r=>[r.rk??"",r.prk??"",r.c,r.n,r.mk,r.g||"",r.gn??"",r.sc??"",...r.b.map(v=>v??""),r.per??"",r.pbr??"",r.m.ROE??"",r.m["12-1모멘텀"]??"",r.mc?Math.round(r.mc/1e8):"",r.xp||"",r.x.join(" / "),r.w.join(" / "),r.pd||"",r.fd||""]));
  const csv="﻿"+lines.map(l=>l.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
  try{const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"})); a.download=`screener_${S.asof_price}.csv`; a.click();}catch(e){}
});
head(); glance(); render();
</script>
"""
