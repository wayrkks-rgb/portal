"""Windows 작업 스케줄러에 일일 수집 배치를 등록·해제·확인한다.

    .venv\\Scripts\\python.exe scripts\\manage_daily_task.py            등록/갱신
    .venv\\Scripts\\python.exe scripts\\manage_daily_task.py --status   상태만
    .venv\\Scripts\\python.exe scripts\\manage_daily_task.py --delete   해제

시각과 작업 이름은 설정(`scheduler.daily_time`, `scheduler.task_name`)을 따른다.
등록·해제는 관리자 권한이 필요하다. 상태 확인은 권한이 없어도 된다.

작업을 지우지 않고 배치만 잠시 쉬게 하려면 `scheduler.enabled` 를 끈다
(관리 → 연계 설정). 작업은 그대로 돌지만 배치가 스스로 빠진다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync import scheduler
from asset_sync.config import load_config

MARK = {True: "✓", False: "✗"}


def show(state: dict) -> None:
    if not state.get("supported"):
        print(f"  [건너뜀] {state.get('reason')}")
        return
    if not state.get("registered"):
        print(f"  {MARK[False]} 등록되어 있지 않습니다.")
        if state.get("reason"):
            print(f"    {state['reason'].splitlines()[0]}")
        return
    print(f"  {MARK[True]} 등록됨: {state['task_name']}")
    for label, key in (("상태", "state"), ("다음 실행", "next_run"),
                       ("마지막 실행", "last_run"), ("마지막 결과", "last_result"),
                       ("실행 대상", "command")):
        if state.get(key):
            print(f"    {label:<10}{state[key]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 수집 배치 작업 등록 관리")
    parser.add_argument("--status", action="store_true", help="등록 상태만 확인한다")
    parser.add_argument("--delete", action="store_true", help="등록을 해제한다")
    args = parser.parse_args()

    config = load_config()
    try:
        wanted = scheduler.settings(config.scheduler)
    except scheduler.ScheduleError as exc:
        print(f"[실패] 설정이 올바르지 않습니다: {exc}", file=sys.stderr)
        return 1

    print(f"작업 이름   {wanted['task_name']}")
    print(f"실행 시각   매일 {wanted['daily_time']}")
    print(f"배치 사용   {'켜짐' if wanted['enabled'] else '꺼짐 (작업은 돌지만 배치가 스스로 빠집니다)'}")
    print(f"실행 대상   {scheduler.batch_path(ROOT)}\n")

    if args.status:
        print("[현재 등록 상태]")
        show(scheduler.describe(wanted["task_name"]))
        return 0

    if not scheduler.is_windows():
        print("[실패] Windows 에서만 등록할 수 있습니다.", file=sys.stderr)
        return 1

    try:
        if args.delete:
            scheduler.unregister(wanted["task_name"])
            print(f"[완료] {wanted['task_name']} 등록을 해제했습니다.")
            return 0
        scheduler.register(ROOT, wanted["task_name"], wanted["daily_time"])
    except scheduler.ScheduleError as exc:
        print(f"[실패] {exc}", file=sys.stderr)
        print("\n확인할 것"
              "\n  1. 이 창을 관리자 권한으로 열었는가"
              "\n  2. 작업 이름에 쓸 수 없는 문자가 없는가"
              "\n  3. scripts\\run_daily_batch.bat 이 있는가", file=sys.stderr)
        return 1

    print(f"[완료] 매일 {wanted['daily_time']} 에 실행되도록 등록했습니다.\n")
    print("[등록 결과]")
    show(scheduler.describe(wanted["task_name"]))
    print("\n지금 바로 한 번 돌려보려면: scripts\\run_auto_now.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
