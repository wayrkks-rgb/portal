"""ITSM 수집이 어디서 막히는지 웹 없이 단계별로 확인한다.

    .venv\\Scripts\\python.exe scripts\\diagnose_oracle_itsm.py
    .venv\\Scripts\\python.exe scripts\\diagnose_oracle_itsm.py --rows 100
    .venv\\Scripts\\python.exe scripts\\diagnose_oracle_itsm.py --json > diag.json

[ITSM 수집] 버튼과 같은 코드를 그대로 태우되, 각 단계의 소요시간과 실패한 예외
사슬을 모두 보여준다. DPY-1001("not connected to database")처럼 **결과만 알려주는**
오류는 그것만 봐서는 원인을 알 수 없다. 실제 이유는 그 앞 예외에 들어 있다.

여기서는 통과하는데 웹 화면에서만 실패한다면, 웹 프로세스 쪽 문제다. 그때는
관리자로 로그인한 브라우저에서 아래를 열어 **같은 점검을 웹 프로세스 안에서**
돌려 결과를 비교한다.

    http://<주소>:5100/api/asset-sync/admin/diagnose/oracle

읽기만 한다. 쓰는 문장은 실행하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors import oracle_itsm_collector as collector_module
from asset_sync.collectors.oracle_diagnostics import run_diagnostics
from asset_sync.config import load_config

MARK = {"SUCCESS": "✓", "FAILED": "✗", "SKIPPED": "-"}


def main() -> int:
    parser = argparse.ArgumentParser(description="ITSM Oracle 수집 단계별 진단")
    parser.add_argument("--rows", type=int, default=0,
                        help="이 건수까지만 수집해 본다 (0 이면 전체)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    parser.add_argument("--full", action="store_true", help="실패 시 전체 스택도 출력")
    args = parser.parse_args()

    # 실패 내용은 아래에서 정리해서 보여준다. 스택까지 겹쳐 찍으면 읽기 어렵다.
    logging.basicConfig(level=logging.CRITICAL)
    if args.full:
        logging.getLogger("asset_sync").setLevel(logging.DEBUG)

    config = load_config()
    result = run_diagnostics(config, max_rows=args.rows or None)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result["status"] != "FAILED" else 1

    print("\n[환경]")
    print(f"  python           {result['python']}")
    print(f"  oracledb         {result['oracledb']}")
    print(f"  수집기 파일      {collector_module.__file__}")
    print(f"  수집기 표식      {result['build']}")

    print("\n[설정]")
    print(f"  ITSM 수집모드    {result['collection_mode']}")
    for key, value in result["settings"].items():
        print(f"  {key:<24}{value}")

    print("\n[단계]")
    for step in result["steps"]:
        mark = MARK.get(step["status"], "?")
        seconds = f"{step['seconds']:>7.2f}초" if "seconds" in step else " " * 10
        print(f"  {mark} {step['name']:<12}{seconds}  {step.get('detail') or step.get('error', '')}")

    if result["status"] == "SUCCESS":
        print("\n모든 단계 통과. [ITSM 수집] 버튼도 같은 경로로 동작합니다.")
        print("화면에서만 실패한다면 웹 프로세스 문제입니다. 관리자로 로그인한 브라우저에서")
        print("  /api/asset-sync/admin/diagnose/oracle")
        print("을 열어 같은 점검을 웹 안에서 돌린 결과와 비교하세요.")
        return 0

    metadata = result.get("collector_metadata") or {}
    if metadata.get("failed_stage"):
        print(f"\n  수집기 단계={metadata['failed_stage']} "
              f"수집={metadata.get('fetched_before_failure')}건 "
              f"단계별초={metadata.get('stage_seconds')}")
    return 0 if result["status"] == "SKIPPED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
