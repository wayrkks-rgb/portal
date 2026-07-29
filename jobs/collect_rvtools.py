from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.config import load_config
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.logging_config import configure_logging
from asset_sync.services.collection_service import CollectionService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["DEMO", "FILE_ONLY", "COMMAND"])
    args = parser.parse_args()
    cfg = load_config()
    configure_logging(cfg.resolve("logs"), cfg.log_level, cfg)
    manager = SQLiteManager(cfg.database_path)
    manager.initialize()
    result = CollectionService(cfg, manager).collect_rvtools(mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
