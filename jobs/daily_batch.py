from __future__ import annotations

import argparse
import json
import logging
import sys
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


def main() -> None:
    parser = argparse.ArgumentParser(description="ITSM/vCenter PowerCLI daily collection")
    parser.add_argument("--demo", action="store_true", help="Oracle/vCenter 접속 없이 synthetic 데이터로 전체 흐름 검증")
    args = parser.parse_args()
    config = load_config()
    configure_logging(config.resolve("logs"), config.log_level, config)

    # 화면에서 끈 상태면 여기서 빠진다. Windows 작업은 그대로 두고 배치가 스스로
    # 판단하므로, 켜고 끄는 데 관리자 권한이 필요하지 않다.
    schedule = scheduler.settings(config.scheduler)
    if not schedule["enabled"] and not args.demo:
        LOGGER.info("일일 배치가 꺼져 있어 실행하지 않습니다(설정: scheduler.enabled).")
        print(json.dumps({
            "status": "DISABLED",
            "reason": "일일 배치가 꺼져 있습니다. 관리 → 연계 설정에서 켤 수 있습니다.",
        }, ensure_ascii=False))
        raise SystemExit(0)

    manager = create_manager(config)
    manager.initialize()
    # 파일 잠금은 같은 호스트만 보호한다. 여러 WAS가 하나의 DB를 공유하면
    # 공유 DB에 잠금을 두어야 07:00 배치가 한 번만 실행된다.
    lock_seconds = int(config.scheduler.get("batch_lock_seconds", DEFAULT_LEASE_SECONDS))
    try:
        with DatabaseLock(manager, "daily_batch", lease_seconds=lock_seconds):
            result = CollectionService(config, manager).run_daily(demo=args.demo)
    except LockNotAcquired as exc:
        print(json.dumps({"status": "SKIPPED", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(0)
    summary = {
        "batch_id": result.get("batch_id"),
        "status": result.get("status"),
        "vcenter": {
            "status": result["vcenter"].get("status"),
            "mode": result["vcenter"].get("mode"),
            "count": result["vcenter"].get("count"),
            "error": result["vcenter"].get("error"),
        },
        "itsm": {
            "status": result["itsm"].get("status"),
            "mode": result["itsm"].get("mode"),
            "count": result["itsm"].get("count"),
            "error": result["itsm"].get("error"),
        },
        "reconciliation": {
            "status": result["reconciliation"].get("status"),
            "counts": result["reconciliation"].get("counts", {}),
        },
        "resource_usage": result.get("resource_usage", {}),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if result.get("status") == "FAILED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
