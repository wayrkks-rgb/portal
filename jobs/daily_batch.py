from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.config import load_config
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.logging_config import configure_logging
from asset_sync.services.collection_service import CollectionService


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.handle, str(os.getpid()).encode())
        except FileExistsError as exc:
            raise RuntimeError(f"다른 배치가 실행 중입니다: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self.handle is not None:
            os.close(self.handle)
        self.path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="ITSM/vCenter PowerCLI daily collection")
    parser.add_argument("--demo", action="store_true", help="Oracle/vCenter 접속 없이 synthetic 데이터로 전체 흐름 검증")
    args = parser.parse_args()
    config = load_config()
    configure_logging(config.resolve("logs"), config.log_level, config)
    manager = SQLiteManager(config.database_path)
    manager.initialize()
    with FileLock(config.resolve("data/daily_batch.lock")):
        result = CollectionService(config, manager).run_daily(demo=args.demo)
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
