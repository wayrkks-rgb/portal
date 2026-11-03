"""업무 시간 중 30분 · 1시간마다 도는 수시 수집. Windows 작업 스케줄러가 부른다.

07:00 일일 배치만 있으면 하루 안에 꺼졌다 켜진 VM, 오전에 바뀐 ITSM 상태를 다음 날에야
본다. 수시 수집은 인벤토리(vCenter · ITSM)만 다시 읽어 바뀐 것을 기록한다.

- 바뀐 것이 없으면 저장하지 않는다(DB 가 커지지 않게).
- 일부 통합기 실패 · 건수 이상 · 조회 SQL 변경이면 저장하지 않는다(가짜 생성 · 삭제를 막는다).
- 일일 배치와 같은 잠금을 쓴다. 07:00 배치가 도는 중이면 이번 회차는 건너뛴다.
- 자원사용률 · 일일 배치 기록은 하루 단위이므로 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync import scheduler
from asset_sync.config import load_config
from asset_sync.db.locks import DEFAULT_LEASE_SECONDS, DatabaseLock, LockNotAcquired
from asset_sync.db.manager import create_manager
from asset_sync.logging_config import configure_logging
from asset_sync.services.collection_service import CollectionService

LOGGER = logging.getLogger(__name__)


def main() -> int:
    config = load_config()
    configure_logging(config.resolve("logs"), config.log_level, config)
    wanted = scheduler.intraday_settings(config.scheduler)
    if not wanted["enabled"]:
        print(json.dumps({"status": "DISABLED", "reason": "수시 수집이 꺼져 있습니다(scheduler.intraday.enabled)."},
                         ensure_ascii=False))
        return 0
    if not scheduler.settings(config.scheduler)["enabled"]:
        print(json.dumps({"status": "DISABLED", "reason": "일일 배치가 꺼져 있어 수시 수집도 쉽니다."}, ensure_ascii=False))
        return 0
    if not scheduler.in_window(wanted, datetime.now()):
        print(json.dumps({"status": "SKIPPED", "reason": f"수시 수집 시간({wanted['start_time']}~{wanted['end_time']}) 밖입니다."},
                         ensure_ascii=False))
        return 0
    manager = create_manager(config)
    manager.initialize()
    lock_seconds = int(config.scheduler.get("batch_lock_seconds", DEFAULT_LEASE_SECONDS))
    try:
        # 일일 배치와 같은 잠금. 둘이 동시에 스냅샷을 쓰면 비교 순서가 꼬인다.
        with DatabaseLock(manager, "daily_batch", lease_seconds=lock_seconds):
            result = CollectionService(config, manager).run_intraday()
    except LockNotAcquired as exc:
        print(json.dumps({"status": "SKIPPED", "reason": str(exc)}, ensure_ascii=False))
        return 0
    summary = {
        "status": result["status"],
        "stored": result["stored"],
        "vcenter": {k: result["vcenter"].get(k) for k in ("status", "count", "stored", "skip_reason", "error")},
        "itsm": {k: result["itsm"].get(k) for k in ("status", "count", "stored", "skip_reason", "error")},
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 2 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
