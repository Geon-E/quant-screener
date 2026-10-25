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
            out[g][k] = vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2
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


TEMPLATE = r"""<title>저평가 스크리너</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">
<style>
/* 레이아웃: 상단 요약 띠 → 필터 줄 → 순위 표(행을 누르면 근거 패널이 펼쳐짐). 표는 자체 가로 스크롤 */
:root{
  --bg:#f5f6f8; --surface:#ffffff; --ink:#16202c; --muted:#5d6878; --line:#dde1e7; --soft:#eceff3;
  --accent:#1f4e8c; --accent-soft:#e3ebf6;
  --up:#c8322b; --down:#2559b8; --warn:#9a6400; --warn-soft:#fbf1dc; --bad:#a3262b; --bad-soft:#f8e4e3;
  --font-body:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;
  --font-num:var(--font-body);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#10151c; --surface:#171e27; --ink:#e4e9f0; --muted:#97a3b3; --line:#2a3440; --soft:#1e2731;
  --accent:#7fa8e3; --accent-soft:#1d2c42; --up:#ef6b62; --down:#6f9cf0; --warn:#e2b04f; --warn-soft:#33290f;
  --bad:#f08a86; --bad-soft:#3a1d1e; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#10151c; --surface:#171e27; --ink:#e4e9f0; --muted:#97a3b3; --line:#2a3440; --soft:#1e2731;
  --accent:#7fa8e3; --accent-soft:#1d2c42; --up:#ef6b62; --down:#6f9cf0; --warn:#e2b04f; --warn-soft:#33290f;
  --bad:#f08a86; --bad-soft:#3a1d1e; color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--font-body);font-size:14px;line-height:1.5;font-variant-numeric:tabular-nums;margin:0}
.wrap{max-width:1120px;margin:0 auto;padding-block:20px 48px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:14px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 16px}
header a{font-size:13px;color:var(--accent)}
.top{display:grid;gap:6px}
h1{font-size:22px;font-weight:700;margin:0;letter-spacing:-.01em;text-wrap:balance}
.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:4px 14px}
.meta b{color:var(--ink);font-weight:600;font-family:var(--font-num)}
.notice{background:var(--warn-soft);color:var(--ink);border:1px solid color-mix(in srgb,var(--warn) 35%,transparent);border-radius:6px;padding:8px 12px;font-size:13px}
.notice summary{cursor:pointer;list-style:none}
.notice summary::-webkit-details-marker{display:none}
.notice summary span{color:var(--muted);font-size:12px;margin-left:6px;text-decoration:underline}
.notice[open] summary span{display:none}
.notice div{margin-top:4px;color:var(--muted)}
.notice strong{color:var(--warn)}
.status{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--muted);align-items:center}
.status .ok{color:var(--ink)}
.status .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#2f8f5b;margin-right:6px;vertical-align:middle}
.status .dot.w{background:var(--warn)}
.status ul{margin:0;padding-left:18px;color:var(--warn);flex-basis:100%}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.controls input[type=search],.controls select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:7px 10px;min-width:0}
.controls input[type=search]{flex:1 1 200px}
.controls label{display:flex;gap:6px;align-items:center;color:var(--muted);font-size:13px;cursor:pointer}
.count{color:var(--muted);font-size:13px;margin-left:auto}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line)}
.tabs button{font:inherit;background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;color:var(--muted);cursor:pointer}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.ghost{font:inherit;font-size:13px;color:var(--accent);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer}
.star{background:none;border:0;cursor:pointer;font-size:15px;color:var(--line);padding:0 6px 0 0;line-height:1;vertical-align:middle}
.star.on{color:#d39a00}
table.gt{min-width:760px} .gt tbody tr{cursor:pointer} .gt tbody tr:hover{background:var(--soft)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
[hidden]{display:none!important}
.tablebox{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:980px;font-size:13.5px}
th,td{padding:7px 10px;border-bottom:1px solid var(--soft);text-align:right;white-space:nowrap;vertical-align:middle}
th.c,td.c{text-align:center}
@media (min-width:641px){#listbox>table{table-layout:fixed}}
th{position:sticky;top:0;background:var(--soft);color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.01em;cursor:pointer;user-select:none;border-bottom:1px solid var(--line)}
th[aria-sort]{color:var(--ink)}
th[aria-sort="ascending"]::after{content:" ▲";font-size:9px}
th[aria-sort="descending"]::after{content:" ▼";font-size:9px}
th.l,td.l{text-align:left}
td{font-family:var(--font-num);font-variant-numeric:tabular-nums}
td.name{font-family:var(--font-body)}
td.name{overflow:hidden}
.name .nmw{display:inline-flex;flex-direction:column;vertical-align:middle;min-width:0;max-width:calc(100% - 22px)}
.name .nm{font-weight:600;font-size:14px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name .cd{color:var(--muted);font-size:11.5px;line-height:1.3}
.name .gs{display:none}
td.grp{overflow:hidden;text-overflow:ellipsis}
.grp{font-family:var(--font-body);color:var(--muted)}
.grp small{font-family:var(--font-num);margin-left:4px}
tbody tr.row{cursor:pointer}
tbody tr.row:hover{background:var(--soft)}
tbody tr.open{background:var(--accent-soft)}
.score{font-weight:700;color:var(--accent);font-size:14.5px}
.bars{display:inline-grid;grid-template-columns:repeat(4,22px);gap:3px;vertical-align:middle}
.bar{height:18px;background:var(--soft);border-radius:2px;position:relative;overflow:hidden}
.bar i{position:absolute;left:0;right:0;bottom:0;background:var(--accent);opacity:.75}
.bar.na{background:repeating-linear-gradient(135deg,var(--soft) 0 3px,transparent 3px 6px)}
.chip{display:inline-block;font-family:var(--font-body);font-size:11px;padding:1px 7px;border-radius:999px;background:var(--warn-soft);color:var(--warn);font-weight:500}
.chip.x{background:var(--bad-soft);color:var(--bad)}
.rel-낮음{color:var(--warn)}
.up{color:var(--up)} .down{color:var(--down)}
.dash{color:var(--muted)}
tr.detail>td{background:var(--surface);padding:0;white-space:normal;text-align:left;font-family:var(--font-body)}
.panel{position:sticky;left:0;box-sizing:border-box;width:min(100%,calc(100vw - 2*max(16px,env(safe-area-inset-left,0px)) - 2px));display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:18px;padding:16px 18px;border-bottom:2px solid var(--accent)}
tr.detail>td{overflow:visible}
.why{grid-column:1/-1;font-size:13.5px;line-height:1.6;display:grid;gap:2px;word-break:keep-all}
.why b{font-weight:600}
.why .pos{color:var(--accent)} .why .neg{color:var(--warn)}
.why .note{color:var(--muted);font-size:12.5px}
.panel h3{font-size:13px;margin:0 0 8px;color:var(--muted);font-weight:600;letter-spacing:.02em}
.mt{width:100%;min-width:0;font-size:12.5px}
.mt th,.mt td{padding:5px 8px;cursor:default;position:static}
.mt td.l{font-family:var(--font-body)}
.pct{display:inline-block;width:64px;height:6px;background:var(--soft);border-radius:3px;vertical-align:middle;margin-left:6px;position:relative}
.pct i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:3px}
.side{display:grid;gap:12px;align-content:start}
.side ul{margin:0;padding-left:18px;display:grid;gap:3px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:12.5px}
.kv dt{color:var(--muted)} .kv dd{margin:0;font-family:var(--font-num)}
.more{justify-self:center;font:inherit;color:var(--accent);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 18px;cursor:pointer}
.legend{color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;gap:4px 16px}
footer{color:var(--muted);font-size:12px;display:grid;gap:4px}
@media (max-width:720px){.panel{grid-template-columns:1fr}h1{font-size:19px}}
/* 휴대폰: 맨 뒤에 둬야 위 규칙을 덮어씀 */
@media (max-width:640px){
  .tablebox table{min-width:0}
  #listbox col{width:auto!important}
  .mt th:nth-child(2),.mt td:nth-child(2){display:none}
  .mt td.l{white-space:nowrap;font-size:12px}
  .mt th,.mt td{padding:5px 4px}
  .mt .pct{display:none}
  .mt th{white-space:normal;word-break:keep-all}
  td.name{overflow:visible;white-space:normal;width:100%}
  .name .cd{white-space:normal;word-break:keep-all}
  .name .nmw{max-width:none;display:inline-flex}
  td.name{position:relative;padding-left:26px}
  td.name .star{position:absolute;left:4px;top:50%;transform:translateY(-50%)}
  .status>span:not(.ok){display:none}
  .meta{font-size:12.5px}
  .wrap{gap:12px;padding-top:14px}
  .name .gs{display:inline}
  .controls input[type=search]{flex-basis:100%}
  .controls select{flex:1 1 40%}
  .count{margin-left:0}
  .legend span:nth-child(2){display:none}
  #hdr th:nth-child(5),#tb tr.row td:nth-child(5),#hdr th:nth-child(8),#tb tr.row td:nth-child(8),
  #hdr th:nth-child(9),#tb tr.row td:nth-child(9),#hdr th:nth-child(10),#tb tr.row td:nth-child(10),
  #hdr th:nth-child(3),#tb tr.row td:nth-child(3){display:none}
  th,td{padding:8px 5px}
  .name .nm{white-space:normal;line-height:1.3}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="wrap">
  <div class="top">
    <header>
      <h1>업종 대비 저평가 스크리너</h1>
      <a href="backtest.html">과거 성과 검증 결과 →</a>
    </header>
    <div class="meta" id="meta"></div>
    <div class="status" id="status"></div>
  </div>
  <details class="notice">
    <summary><strong>점수는 가설입니다.</strong> 과거 성과로 아직 검증되지 않았고, 투자 권유가 아닙니다.<span>자세히</span></summary>
    <div>가중치: 가치 40%·퀄리티 30%·추세 20%·안정성 10%. <span id="notice2"></span></div>
  </details>
  <div class="tabs" role="tablist">
    <button role="tab" id="tab-list" aria-selected="true">종목 순위</button>
    <button role="tab" id="tab-grp" aria-selected="false">사업군 요약</button>
  </div>
  <div class="controls" id="ctl">
    <input type="search" id="q" placeholder="종목명 또는 코드" aria-label="종목 검색">
    <select id="mk" aria-label="시장"><option value="">전체 시장</option><option>KOSPI</option><option>KOSDAQ</option></select>
    <select id="grp" aria-label="사업군"><option value="">전체 사업군</option></select>
    <label><input type="checkbox" id="showx"> 제외 종목 보기</label>
    <label><input type="checkbox" id="onlyw"> ★ 관심 종목만</label>
    <button class="ghost" id="csv">CSV로 받기</button>
    <span class="count" id="count"></span>
  </div>
  <div class="legend">
    <span>막대 4개 = 가치·퀄리티·추세·안정성 (사업군 내 백분위, 빗금은 지표 부족)</span>
    <span>PER·PBR은 지배주주 기준, 우선주 시총 합산</span>
  </div>
  <div class="tablebox" id="listbox">
    <table>
      <colgroup><col class="ck" style="width:52px"><col class="cn"><col class="cg" style="width:190px"><col class="cs" style="width:64px"><col class="cb" style="width:118px"><col class="cp" style="width:62px"><col class="cpb" style="width:62px"><col class="cr" style="width:70px"><col class="cm" style="width:76px"><col class="cc" style="width:104px"><col class="cf" style="width:62px"></colgroup>
      <thead><tr id="hdr"></tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
  <div class="tablebox" id="grpbox" hidden>
    <table class="gt"><thead><tr><th class="l">사업군</th><th>순위 대상</th><th>신뢰도</th><th>PER 중앙값</th><th>PBR 중앙값</th><th>ROE 중앙값</th><th class="l">1위 종목</th><th>1위 점수</th></tr></thead><tbody id="gtb"></tbody></table>
  </div>
  <button class="more" id="more" hidden>더 보기</button>
  <footer>
    <div>자료: KRX 정보데이터시스템 OPEN API(시세), OpenDART(재무). 재무는 최근 4개 분기 합(TTM), 연결재무제표 우선.</div>
    <div>사업군: KSIC 업종코드를 묶은 고정 표(v1). 표본이 10개 미만인 사업군은 신뢰도 '낮음'.</div>
  </footer>
</div>

<script>
const DATA = /*__DATA__*/null;
const S = DATA.summary, ROWS = DATA.rows, MED = DATA.med;
const LABEL = {"E/P":"이익수익률(E/P)","B/P":"장부가수익률(B/P)","EBIT/EV":"EBIT/EV","PBR-ROE잔차":"예상 PBR 대비","ROE":"ROE","GP/A":"GP/A","CFO/NI":"이익의 질(CFO/NI)","12-1모멘텀":"12-1 모멘텀","영업이익증가율":"영업이익 증가율","이자보상배율":"이자보상배율"};
const BUCKET = {"E/P":"가치","B/P":"가치","EBIT/EV":"가치","PBR-ROE잔차":"가치","ROE":"퀄리티","GP/A":"퀄리티","CFO/NI":"퀄리티","12-1모멘텀":"추세","영업이익증가율":"추세","이자보상배율":"안정성"};
const PCTM = new Set(["E/P","B/P","EBIT/EV","ROE","GP/A","12-1모멘텀","영업이익증가율"]);
const COLS = [
  {k:"rk",t:"순위",a:"c"},{k:"n",t:"종목",a:"l"},{k:"g",t:"사업군",a:"l"},{k:"sc",t:"종합"},{k:"b",t:"묶음 점수",ns:1,a:"c"},
  {k:"per",t:"PER"},{k:"pbr",t:"PBR"},{k:"roe",t:"ROE"},{k:"mom",t:"모멘텀"},{k:"mc",t:"시총(억)"},{k:"flag",t:"경고",ns:1,a:"c"}
];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (v,d=1) => v==null ? '<span class="dash">–</span>' : v.toLocaleString("ko-KR",{maximumFractionDigits:d,minimumFractionDigits:d});
const pct = (v,d=1,color=false) => v==null ? '<span class="dash">–</span>' : `<span class="${color?(v>0?'up':v<0?'down':''):''}">${(v*100).toFixed(d)}%</span>`;
const CHANGE = new Set(["12-1모멘텀","영업이익증가율"]);   // 상승 빨강·하락 파랑은 변화율에만 (수익률·비율 지표에는 쓰지 않음)
function fmtMetric(k,v){
  if(v==null) return '<span class="dash">–</span>';
  if(k==="PBR-ROE잔차"){const d=Math.exp(-v)-1; return `<span class="${d<0?'down':'up'}">${(d*100).toFixed(0)}%</span>`;}
  if(PCTM.has(k)) return pct(v,1,CHANGE.has(k));
  if(k==="이자보상배율") return v>=100?"100배+":num(v,1)+"배";
  return num(v,2)+"배";
}
let sortK="rk", sortDir=1, limit=200, openCode=null;
let WATCH=new Set(); try{WATCH=new Set(JSON.parse(localStorage.getItem("qs-watch")||"[]"));}catch(e){}
function saveWatch(){try{localStorage.setItem("qs-watch",JSON.stringify([...WATCH]));}catch(e){}}
const val = (r,k) => k==="roe"?r.m.ROE : k==="mom"?r.m["12-1모멘텀"] : k==="g"?(r.g||"") : k==="n"?(r.n||"") : r[k];

function meta(){
  const fin = S.collection_complete ? "전 종목 수집 완료" : `재무 수집 진행 중 ${S.universe_collected.toLocaleString()}/${S.universe_total.toLocaleString()}개사`;
  const d = S.asof_price; const ds = d ? `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}` : "";
  document.getElementById("meta").innerHTML =
    `<span>시세 기준 <b>${ds}</b></span><span>순위 <b>${S.ranked.toLocaleString()}</b>개 종목</span><span>평가 <b>${S.evaluated.toLocaleString()}</b>개사</span><span>${fin}</span>`;
  const ka=(S.status||{}).kind_admin;
  document.getElementById("notice2").textContent = (ka ? `관리종목 제외: 코스닥은 KRX 소속부, 코스피는 KIND 현재 목록(${ka.date.slice(0,4)}-${ka.date.slice(4,6)}-${ka.date.slice(6)}, ${ka.count}종목) 기준.` : "코스피 관리종목은 아직 걸러내지 못합니다(코스닥은 제외 반영).") + " 결측 지표는 빈칸이며 점수를 채워 넣지 않습니다.";
  const gs=[...new Set(ROWS.filter(r=>r.g&&r.gid!=="EX").map(r=>r.g))].sort((a,b)=>a.localeCompare(b,"ko"));
  const sel=document.getElementById("grp"); gs.forEach(g=>{const o=document.createElement("option");o.textContent=g;sel.append(o);});
}
function status(){
  const st=S.status||{}, w=(st.warnings||[]).filter(x=>!x.startsWith("재무 수집 진행 중"));
  const built=(st.built_at||"").replace("T"," ");
  const h=st.history; const hs = h&&h.targets ? `과거 이력(2015~) 수집 ${h.processed.toLocaleString()}/${h.targets.toLocaleString()}개사${h.complete?" 완료":""}` : "과거 이력(2015~) 수집 대기";
  document.getElementById("status").innerHTML =
    `<span class="ok"><span class="dot ${w.length?'w':''}"></span>${w.length?'갱신됨 · 확인 필요 '+w.length+'건':'정상 갱신'}</span><span>화면 생성 ${esc(built)}</span><span>${hs}</span>`+
    (w.length?`<ul>${w.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:"");
}
function header(){
  document.getElementById("hdr").innerHTML = COLS.map(c=>`<th class="${c.a||''}" data-k="${c.k}" ${c.ns?'data-ns="1"':''} ${sortK===c.k?`aria-sort="${sortDir>0?'ascending':'descending'}"`:''}>${c.t}</th>`).join("");
}
function filtered(){
  const q=document.getElementById("q").value.trim().toLowerCase(), mk=document.getElementById("mk").value,
        g=document.getElementById("grp").value, sx=document.getElementById("showx").checked, ow=document.getElementById("onlyw").checked;
  let rs=ROWS.filter(r=>(ow? WATCH.has(r.c) : (sx? r.x.length>0 : r.x.length===0)) && (!mk||r.mk===mk) && (!g||r.g===g) &&
    (!q || (r.n||"").toLowerCase().includes(q) || r.c.includes(q)));
  rs.sort((a,b)=>{const x=val(a,sortK), y=val(b,sortK);
    if(x==null&&y==null) return 0; if(x==null) return 1; if(y==null) return -1;
    return (typeof x==="string" ? x.localeCompare(y,"ko") : x-y)*sortDir;});
  return rs;
}
function bars(b){return `<span class="bars" aria-label="가치 ${b[0]??'부족'}, 퀄리티 ${b[1]??'부족'}, 추세 ${b[2]??'부족'}, 안정성 ${b[3]??'부족'}">${b.map(v=>v==null?'<span class="bar na"></span>':`<span class="bar"><i style="height:${v}%"></i></span>`).join("")}</span>`;}
function row(r){
  const flag = r.x.length ? `<span class="chip x" title="${esc(r.x.join(' · '))}">제외 ${r.x.length}</span>` : (r.w.length?`<span class="chip" title="${esc(r.w.join(' · '))}">${r.w.length}</span>`:'');
  return `<tr class="row ${openCode===r.c?'open':''}" data-c="${r.c}" tabindex="0">
    <td class="c">${r.rk??'<span class="dash">–</span>'}</td>
    <td class="l name"><button class="star ${WATCH.has(r.c)?'on':''}" data-w="${r.c}" aria-label="관심 종목 ${WATCH.has(r.c)?'해제':'추가'}">★</button><span class="nmw"><span class="nm">${esc(r.n)}</span><span class="cd">${r.c} · ${r.mk==="KOSPI"?"코스피":"코스닥"}<span class="gs"> · ${esc(r.g||'미배정')}</span></span></span></td>
    <td class="l grp" title="${esc(r.g||'')}">${esc(r.g||'미배정')}${r.gn?`<small class="rel-${r.rel}">${r.gn}</small>`:''}</td>
    <td class="score">${num(r.sc,1)}</td><td class="c">${bars(r.b)}</td>
    <td>${r.per==null?'<span class="dash">–</span>':num(r.per,1)}</td><td>${r.pbr==null?'<span class="dash">–</span>':num(r.pbr,2)}</td>
    <td>${pct(r.m.ROE)}</td><td>${pct(r.m["12-1모멘텀"],0,true)}</td>
    <td>${r.mc==null?'<span class="dash">–</span>':Math.round(r.mc/1e8).toLocaleString()}</td><td class="c">${flag}</td></tr>`;
}
function why(r){
  // 근거 한 줄 요약: 백분위로 강점(상위 20%)·약점(하위 30%)·자료 없음을 뽑아 문장으로 (점수 계산과 같은 값만 사용)
  const ks=Object.keys(LABEL).filter(k=>r.p[k]!=null);
  const strong=ks.filter(k=>r.p[k]>=80).sort((a,b)=>r.p[b]-r.p[a]).slice(0,3);
  const weak=ks.filter(k=>r.p[k]<=30).sort((a,b)=>r.p[a]-r.p[b]).slice(0,2);
  const miss=Object.keys(LABEL).filter(k=>r.p[k]==null).map(k=>LABEL[k]);
  const top=k=>`${LABEL[k]} 상위 ${Math.max(1,Math.round(100-r.p[k]))}%`;
  const B=["가치","퀄리티","추세","안정성"];
  const bi=r.b.map((v,i)=>[v,i]).filter(x=>x[0]!=null).sort((a,b)=>b[0]-a[0]);
  let head = r.rk ? `<b>전체 ${r.rk}위</b> · ${esc(r.g)} 사업군 ${r.gn}개 중 ${r.grk}위 (종합 ${r.sc.toFixed(1)})`
           : r.x.length ? `<b>순위 제외</b> — ${esc(r.x[0])}${r.x.length>1?` 외 ${r.x.length-1}건`:''}` : '<b>점수 없음</b>';
  const lines=[head];
  if(bi.length) lines.push(`가장 높은 묶음은 <b>${B[bi[0][1]]}</b>(${bi[0][0].toFixed(0)}점)${bi.length>1?`, 가장 낮은 묶음은 <b>${B[bi[bi.length-1][1]]}</b>(${bi[bi.length-1][0].toFixed(0)}점)`:''}.`);
  if(strong.length) lines.push(`<span class="pos">강점</span> ${strong.map(top).join(" · ")}`);
  if(weak.length) lines.push(`<span class="neg">약점</span> ${weak.map(k=>`${LABEL[k]} 하위 ${Math.max(1,Math.round(r.p[k]))}%`).join(" · ")}`);
  const notes=[];
  if(miss.length) notes.push(`자료 없음: ${miss.join(", ")}`);
  if(r.rel==="낮음") notes.push(`비교 대상이 ${r.gn}개뿐이라 백분위가 거칠다`);
  if(r.w.length) notes.push(`경고 ${r.w.length}건 — 오른쪽 확인`);
  if(notes.length) lines.push(`<span class="note">${esc(notes.join(" · "))}</span>`);
  return `<div class="why">${lines.map(l=>`<div>${l}</div>`).join("")}</div>`;
}
function detail(r){
  const med = MED[r.gid]||{};
  const mrows = Object.keys(LABEL).map(k=>`<tr><td class="l">${LABEL[k]}</td><td class="l" style="color:var(--muted)">${BUCKET[k]}</td>
    <td>${fmtMetric(k,r.m[k])}</td><td>${k==="PBR-ROE잔차"?'<span class="dash">–</span>':fmtMetric(k,med[k]??null)}</td>
    <td>${r.p[k]==null?'<span class="dash">–</span>':`${r.p[k].toFixed(0)}<span class="pct"><i style="width:${r.p[k]}%"></i></span>`}</td></tr>`).join("");
  const B=["가치","퀄리티","추세","안정성"];
  const list=(a,cls)=>a.length?`<ul>${a.map(x=>`<li class="${cls||''}">${esc(x)}</li>`).join("")}</ul>`:'<div class="dash">없음</div>';
  const fb = r.fb.length ? r.fb.map(x=>x.replace("ni_owners","지배주주 순이익").replace("equity_owners","지배주주 자본").replace("separate_only","별도재무제표만 제출 → 전체=지배").replace("no_nci","비지배지분 없음 → 전체=지배")).join(", ") : "표준 계정으로 확인";
  return `<tr class="detail"><td colspan="${COLS.length}"><div class="panel">${why(r)}
    <div style="min-width:0;overflow-x:auto"><h3>지표 — ${esc(r.g||'')} 사업군 ${r.gn?`${r.gn}개 종목 (신뢰도 ${r.rel})`:''}</h3>
      <table class="mt"><thead><tr><th class="l">지표</th><th class="l">묶음</th><th>이 종목</th><th>사업군 중앙값</th><th>백분위</th></tr></thead><tbody>${mrows}</tbody></table></div>
    <div class="side">
      <div><h3>묶음 점수</h3><dl class="kv">${B.map((b,i)=>`<dt>${b}</dt><dd>${r.b[i]==null?'지표 부족':r.b[i].toFixed(1)}</dd>`).join("")}<dt>종합</dt><dd>${r.sc==null?'–':r.sc.toFixed(1)}${r.grk?` (사업군 ${r.grk}위)`:''}</dd></dl></div>
      ${r.x.length?`<div><h3>순위 제외 사유</h3>${list(r.x)}</div>`:''}
      <div><h3>경고</h3>${list(r.w)}</div>
      <div><h3>데이터</h3><dl class="kv"><dt>재무 기준</dt><dd>${r.pd||'–'} ${r.fs==="CFS"?"연결":r.fs==="OFS"?"별도":""}</dd>
        <dt>현재가</dt><dd>${r.px?Number(r.px).toLocaleString()+"원":"–"}</dd><dt>20일 거래대금</dt><dd>${r.tv==null?'–':Math.round(r.tv/1e8).toLocaleString()+"억"}</dd>
        <dt>F-스코어</dt><dd>${r.fsc??'–'}</dd><dt>업종코드</dt><dd>${r.ksic||'–'}</dd><dt>계정 선택</dt><dd style="font-family:var(--font-body)">${esc(fb)}</dd>
        ${r.ib?`<dt>이자 기준</dt><dd style="font-family:var(--font-body)">${esc(r.ib)}</dd>`:''}</dl></div>
    </div></div></td></tr>`;
}
function render(){
  header();
  const rs=filtered(), shown=rs.slice(0,limit);
  document.getElementById("tb").innerHTML = shown.map(r=>row(r)+(openCode===r.c?detail(r):"")).join("") ||
    `<tr><td colspan="${COLS.length}" class="l" style="padding:20px;font-family:var(--font-body);color:var(--muted)">조건에 맞는 종목이 없습니다.</td></tr>`;
  document.getElementById("count").textContent = `${rs.length.toLocaleString()}개 종목`;
  const more=document.getElementById("more"); more.hidden = rs.length<=limit; more.textContent=`더 보기 (${(rs.length-limit).toLocaleString()}개 남음)`;
}
document.getElementById("hdr").addEventListener("click",e=>{const th=e.target.closest("th"); if(!th||th.dataset.ns) return;
  const k=th.dataset.k; if(sortK===k) sortDir=-sortDir; else {sortK=k; sortDir=(k==="rk"||k==="n"||k==="g"||k==="per"||k==="pbr")?1:-1;} render();});
document.getElementById("tb").addEventListener("click",e=>{const st=e.target.closest(".star"); if(st){e.stopPropagation(); const c=st.dataset.w; WATCH.has(c)?WATCH.delete(c):WATCH.add(c); saveWatch(); render(); return;} const tr=e.target.closest("tr.row"); if(!tr) return; openCode = openCode===tr.dataset.c?null:tr.dataset.c; render();});
document.getElementById("tb").addEventListener("keydown",e=>{if(e.key!=="Enter") return; const tr=e.target.closest("tr.row"); if(!tr) return; openCode=openCode===tr.dataset.c?null:tr.dataset.c; render();});
["q","mk","grp","showx","onlyw"].forEach(id=>document.getElementById(id).addEventListener("input",()=>{limit=200; if(id==="showx"){sortK=document.getElementById("showx").checked?"mc":"rk"; sortDir=document.getElementById("showx").checked?-1:1;} render();}));
document.getElementById("more").addEventListener("click",()=>{limit+=300; render();});
function groupView(){
  const by={}; ROWS.filter(r=>!r.x.length&&r.g).forEach(r=>(by[r.g]=by[r.g]||[]).push(r));
  const med=a=>{a=a.filter(v=>v!=null).sort((x,y)=>x-y); if(!a.length) return null; const m=a.length>>1; return a.length%2?a[m]:(a[m-1]+a[m])/2;};
  const rows=Object.entries(by).map(([g,rs])=>{rs.sort((a,b)=>(a.rk??1e9)-(b.rk??1e9)); return {g,n:rs.length,rel:rs[0].rel,per:med(rs.map(r=>r.per)),pbr:med(rs.map(r=>r.pbr)),roe:med(rs.map(r=>r.m.ROE)),top:rs[0]};})
    .sort((a,b)=>b.n-a.n);
  document.getElementById("gtb").innerHTML=rows.map(x=>`<tr data-g="${esc(x.g)}"><td class="l" style="font-family:var(--font-body)">${esc(x.g)}</td><td>${x.n}</td><td class="rel-${x.rel}" style="font-family:var(--font-body)">${x.rel||''}</td><td>${x.per==null?'–':num(x.per,1)}</td><td>${x.pbr==null?'–':num(x.pbr,2)}</td><td>${pct(x.roe)}</td><td class="l" style="font-family:var(--font-body)">${esc(x.top.n)}</td><td class="score">${num(x.top.sc,1)}</td></tr>`).join("")||'<tr><td colspan="8" class="l dash">순위 대상 종목이 없습니다.</td></tr>';
}
function tab(which){
  const list=which==="list";
  document.getElementById("tab-list").setAttribute("aria-selected",list); document.getElementById("tab-grp").setAttribute("aria-selected",!list);
  document.getElementById("listbox").hidden=!list; document.getElementById("ctl").hidden=!list; document.querySelector(".legend").hidden=!list; document.getElementById("more").hidden=!list||document.getElementById("more").hidden;
  document.getElementById("grpbox").hidden=list; if(!list) groupView(); else render();
}
document.getElementById("tab-list").addEventListener("click",()=>tab("list"));
document.getElementById("tab-grp").addEventListener("click",()=>tab("grp"));
document.getElementById("gtb").addEventListener("click",e=>{const tr=e.target.closest("tr[data-g]"); if(!tr) return; document.getElementById("grp").value=tr.dataset.g; tab("list");});
document.getElementById("csv").addEventListener("click",()=>{
  const head=["순위","종목코드","종목명","시장","사업군","종합","가치","퀄리티","추세","안정성","PER","PBR","ROE","12-1모멘텀","시총(억)","제외사유","경고"];
  const lines=[head].concat(filtered().map(r=>[r.rk??"",r.c,r.n,r.mk,r.g||"",r.sc??"",...r.b.map(v=>v??""),r.per??"",r.pbr??"",r.m.ROE??"",r.m["12-1모멘텀"]??"",r.mc?Math.round(r.mc/1e8):"",r.x.join(" / "),r.w.join(" / ")]));
  const csv="\ufeff"+lines.map(l=>l.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
  try{const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"})); a.download=`screener_${S.asof_price}.csv`; a.click();}catch(e){}
});
meta(); status(); render();
</script>
"""
