from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.config import load_config
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.quality.seed_rules import seed_default_rules
from asset_sync.repositories import AssetRepository


def main() -> None:
    config = load_config()
    local_query = config.root_dir / "config" / "oracle_query.local.sql"
    example_query = config.root_dir / "config" / "oracle_query.example.sql"
    if not local_query.exists() and example_query.exists():
        local_query.write_text(example_query.read_text(encoding="utf-8"), encoding="utf-8")
    manager = SQLiteManager(config.database_path)
    manager.initialize()
    with manager.connect() as connection:
        seed_default_rules(AssetRepository(connection), config)
    print(f"SQLite initialized: {config.database_path}")


if __name__ == "__main__":
    main()
