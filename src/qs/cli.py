"""명령행 진입점: python -m qs.cli <명령>

  krx-backfill   KRX 전 종목 시세·지수 (기본 2025-09-01~오늘) + 월말 종목기본정보
  sample         검증 표본 30개사 선정 (config/sample_v1.csv, 고정)
  dart-sample    표본 기업 재무·공시 버전·정정 최초본 XBRL 수집
  inventory      표본 계정 ID 분포 집계
  collect-all    위 네 단계를 순서대로
  evaluate       표본 L1·L2 생성, 커버리지, 검증 워크북 (reports/phase1/verification_workbook.xlsx)
  determinism    같은 L0로 두 번 계산해 결과 해시 비교
  adjust         수정주가: KRX 기준가로 조정계수 산출, 분류, 표본 DART 공시 확인, 비공식 수정주가 교차검증
  xcheck         원천값 자동 대조: 우리 계정 선택 결과 vs DART 주요계정·사업보고서 배당 항목 (D-22 변경)
  exp-listing    1단계 실험: 코스피 관리종목 소스(D-15)·상장폐지 소스(D-19) 비교
  universe       전 종목 최근 재무 수집 (2024년~, 재개 가능, 방침 B)
  screen         전 종목 업종 대비 점수 + 화면(reports/screen/screener.html)
  backtest       과거 성과 검증 (--bt-mode dev|validation|smoke, --bt-scenario, --bt-adj)
  exp-admin-rules  4차 Q1 준비: 관리종목 요건 직접 판정 vs 코스닥 정답지 (재현율·정밀도)
  phase1         증분 수집 + 인벤토리 + 평가 + 결정성 검사
모든 명령은 실행 보고서(reports/phase1/runs/)와 보안 검사를 남긴다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback

from . import inventory, sample
from .collect import krx
from .collect.dart import DART, collect_company
from .http import BudgetExceeded
from .storage import DATA, scan_for_secrets, write_manifest
from .util import REPORTS, log, log_lines, now_kst, scrub, write_text


def cmd_krx(args) -> dict:
    start = dt.date.fromisoformat(args.start)
    return krx.run_backfill(start, budget=args.krx_budget)


def cmd_sample(args) -> dict:
    d = DART(budget=args.dart_budget)
    s = sample.select(d, reselect=args.reselect)
    by_cat = {}
    for r in s:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    return {"count": len(s), "by_category": by_cat, **d.http.stats()}


def cmd_dart(args) -> dict:
    d = DART(budget=args.dart_budget)
    s = sample.load_sample()
    if not s:
        raise RuntimeError("표본 파일이 없습니다. sample을 먼저 실행하세요.")
    per, stopped = [], None
    try:
        for row in s:
            r = collect_company(d, row["corp_code"])
            r["name"] = row["name"]
            per.append(r)
            log(f"DART {row['name']}: 보고서 {r['reports']}, 재무 {r['fs_ok']}/{r['fs_ok'] + r['fs_none']}, "
                f"정정 {r['amended']} (XBRL {r['xbrl_ok']})")
    except BudgetExceeded as e:
        stopped = str(e)
        log(f"DART 수집 중단(다음 실행에서 이어받음): {e}")
    return {"companies_done": len(per), "of": len(s), "stopped": stopped, "per_company": per, **d.http.stats()}


def cmd_inventory(args) -> dict:
    return inventory.run()


def cmd_adjust(args) -> dict:
    from . import adjust
    d = DART(budget=args.dart_budget)
    codes = {r["stock_code"] for r in sample.load_sample()}
    try:   # 방침 B: 전 종목 모멘텀을 위해 수집된 유니버스 전체의 기업행위를 공시로 확인 (사건별 결과는 캐시)
        from .storage import L1
        from .util import read_gz_json
        codes |= {u["stock_code"] for u in read_gz_json(L1 / "universe.json.gz")["rows"]}
    except FileNotFoundError:
        pass
    return adjust.run(confirm_codes=codes, dart=d)


def cmd_evaluate(args) -> dict:
    from . import pipeline
    return pipeline.run()


def cmd_xcheck(args) -> dict:
    from . import xcheck
    return xcheck.run(DART(budget=args.dart_budget))


def cmd_exp_listing(args) -> dict:
    from . import exp_listing
    return exp_listing.run(DART(budget=args.dart_budget))


def cmd_universe(args) -> dict:
    from . import universe
    return universe.run(DART(budget=args.universe_budget))


def cmd_screen(args) -> dict:
    from . import screen
    return screen.run()


def cmd_backtest(args) -> dict:
    from . import backtest
    if args.bt_mode == "smoke":
        return backtest.run(start=args.bt_start or "2025-09-01", end=args.bt_end or "2099-12-31", mode="smoke",
                            scenario=args.bt_scenario, adj_policy=args.bt_adj)
    if args.bt_mode == "validation":
        return backtest.run(start=args.bt_start or backtest.VALIDATION_START, end=args.bt_end or "2099-12-31",
                            mode="validation", scenario=args.bt_scenario, adj_policy=args.bt_adj)
    return backtest.run(start=args.bt_start or backtest.DEV[0], end=args.bt_end or backtest.DEV[1], mode="dev",
                        scenario=args.bt_scenario, adj_policy=args.bt_adj)


def cmd_exp_admin_rules(args) -> dict:
    from . import exp_admin_rules
    return exp_admin_rules.run()


def cmd_determinism(args) -> dict:
    from . import pipeline
    return pipeline.determinism_check()


COMMANDS = {"krx-backfill": [cmd_krx], "sample": [cmd_sample], "dart-sample": [cmd_dart],
            "inventory": [cmd_inventory], "collect-all": [cmd_krx, cmd_sample, cmd_dart, cmd_inventory],
            "evaluate": [cmd_evaluate], "determinism": [cmd_determinism], "adjust": [cmd_adjust],
            "xcheck": [cmd_xcheck], "exp-listing": [cmd_exp_listing],
            "universe": [cmd_universe], "screen": [cmd_screen], "backtest": [cmd_backtest], "exp-admin-rules": [cmd_exp_admin_rules],
            "phase1": [cmd_krx, cmd_dart, cmd_universe, cmd_inventory, cmd_adjust, cmd_evaluate, cmd_xcheck,
                       cmd_screen, cmd_determinism]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qs")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--start", default="2017-01-01")      # 백테스트(2018-04~)와 12-1 모멘텀용 13개월 이전 시세
    ap.add_argument("--krx-budget", type=int, default=6000)
    ap.add_argument("--dart-budget", type=int, default=15000)
    ap.add_argument("--universe-budget", type=int, default=16000)   # DART 일 한도 2만 − 표본·실험 몫
    ap.add_argument("--reselect", action="store_true")
    ap.add_argument("--bt-mode", default="smoke", choices=["dev", "validation", "smoke"])
    ap.add_argument("--bt-start")
    ap.add_argument("--bt-end")
    ap.add_argument("--bt-scenario", default="base", choices=["optimistic", "base", "conservative"])
    ap.add_argument("--bt-adj", default="confirmed_only", choices=["confirmed_only", "all"])
    args = ap.parse_args(argv)

    started = now_kst()
    results, failed = {}, False
    for fn in COMMANDS[args.command]:
        name = fn.__name__
        try:
            results[name] = fn(args)
        except Exception as e:  # noqa: BLE001
            failed = True
            results[name] = {"error": scrub(f"{type(e).__name__}: {e}"), "trace": scrub(traceback.format_exc()[-2000:])}
            log(f"!! {name} 실패: {e}")
            break

    for sub in ("l0", "l1", "l2"):
        if (DATA / sub).exists():
            write_manifest(DATA / sub)
    hits = scan_for_secrets(DATA, REPORTS)
    report = {"command": args.command, "started": started.isoformat(), "finished": now_kst().isoformat(),
              "ok": not failed and not hits, "secret_scan_hits": hits, "results": results, "log": log_lines()}
    out = REPORTS / "phase1" / "runs" / f"{started:%Y%m%d_%H%M%S}_{args.command}.json"
    write_text(out, json.dumps(scrub(report), ensure_ascii=False, indent=1, default=str))
    write_text(REPORTS / "phase1" / "last_run.json", json.dumps(scrub(report), ensure_ascii=False, indent=1, default=str))
    if hits:
        log(f"!! 보안 검사 실패: 키 문자열 발견 {hits}")
        sys.exit(2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
