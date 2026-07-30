"""Copy an existing SQLite database into MySQL.

Run once when switching a single-host install over to the shared MySQL database
that several WAS instances will use.

    .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_mysql.py --check
    .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_mysql.py

``--check`` only reports row counts so the source and target can be compared
before anything is written. Tables are copied parent-first so foreign keys hold,
and the target must be empty unless ``--truncate`` is given.

``TABLE_ORDER`` lists the shared portal tables only. A module owner who adds
tables through ``asset_sync/db/modules/<id>.sql`` and needs that data carried over
appends their own table names here as well; the target schema is created by
``initialize()`` either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.config import load_config
from asset_sync.db.manager import MySQLManager, SQLiteManager

# Parent tables first: every child references a table listed above it.
TABLE_ORDER = [
    "schema_version",
    "collection_run",
    "snapshot",
    "itsm_asset_snapshot",
    "rv_asset_snapshot",
    "asset_ip",
    "identity_map",
    "change_event",
    "reconciliation_result",
    "sync_result",
    "data_quality_rule",
    "data_quality_result",
    "manual_asset_override",
    "data_quality_exception",
    "audit_log",
    "daily_batch_run",
    "reconciliation_exception",
    "vcenter_resource_daily",
    "resource_usage_run",
    "host_resource_usage_daily",
    "vm_resource_usage_daily",
]

# Columns MySQL generates from other columns; they must never be inserted.
GENERATED_COLUMNS = {"active_cm_id", "active_vm_uuid", "active_key", "dedup_key"}

# Schema metadata, not data: initialize() already seeded it in the target.
# schema_migration is deliberately absent from TABLE_ORDER for the same reason —
# the target records what it applied itself.
SKIP_COPY = {"schema_version", "schema_migration"}

BATCH_SIZE = 500


def _sqlite_columns(conn, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row["name"]) for row in rows]


def _mysql_columns(conn, table: str, database: str) -> list[str]:
    rows = conn.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION",
        (database, table),
    ).fetchall()
    return [str(row["COLUMN_NAME"]) for row in rows]


def _count(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()
    return int(row["cnt"]) if row else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → MySQL 데이터 이관")
    parser.add_argument("--check", action="store_true", help="쓰지 않고 양쪽 건수만 비교")
    parser.add_argument("--truncate", action="store_true", help="대상 테이블에 데이터가 있으면 비우고 이관")
    args = parser.parse_args()

    config = load_config()
    mysql_settings = (config.database or {}).get("mysql") or {}
    source = SQLiteManager(config.database_path)
    target = MySQLManager(mysql_settings)
    if not config.database_path.exists():
        raise SystemExit(f"원본 SQLite 파일이 없습니다: {config.database_path}")

    target.initialize()
    report: dict[str, dict[str, int]] = {}

    with source.connect() as src, target.connect() as dst:
        if args.check:
            for table in TABLE_ORDER:
                report[table] = {"sqlite": _count(src, table), "mysql": _count(dst, table)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        existing = {table: _count(dst, table) for table in TABLE_ORDER}
        occupied = {table: count for table, count in existing.items() if count and table != "schema_version"}
        if occupied and not args.truncate:
            raise SystemExit(
                "대상 MySQL에 이미 데이터가 있습니다. 확인 후 --truncate 로 다시 실행하세요: "
                + ", ".join(f"{table}({count})" for table, count in occupied.items())
            )
        if occupied:
            dst.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in reversed(TABLE_ORDER):
                if table not in SKIP_COPY:
                    dst.execute(f"DELETE FROM {table}")
            dst.execute("SET FOREIGN_KEY_CHECKS=1")

        for table in TABLE_ORDER:
            if table in SKIP_COPY:
                continue
            source_columns = _sqlite_columns(src, table)
            target_columns = [
                name for name in _mysql_columns(dst, table, target.database) if name not in GENERATED_COLUMNS
            ]
            columns = [name for name in source_columns if name in target_columns]
            skipped = sorted(set(source_columns) - set(columns))
            if not columns:
                raise SystemExit(f"{table}: 이관 가능한 공통 컬럼이 없습니다.")
            column_list = ", ".join(columns)
            markers = ", ".join("?" for _ in columns)
            rows = src.execute(f"SELECT {column_list} FROM {table}").fetchall()
            payload = [tuple(row[name] for name in columns) for row in rows]
            for start in range(0, len(payload), BATCH_SIZE):
                dst.executemany(
                    f"INSERT INTO {table}({column_list}) VALUES ({markers})",
                    payload[start : start + BATCH_SIZE],
                )
            report[table] = {"copied": len(payload)}
            if skipped:
                report[table]["skipped_columns"] = len(skipped)
                print(f"  {table}: 대상에 없는 컬럼 제외 {skipped}", file=sys.stderr)

        # AUTO_INCREMENT continues from the copied ids automatically for InnoDB,
        # but verifying the counts here is what proves the migration succeeded.
        for table in TABLE_ORDER:
            if table in SKIP_COPY:
                continue
            copied = report[table]["copied"]
            actual = _count(dst, table)
            if actual != copied:
                raise SystemExit(f"{table}: 이관 건수 불일치 (복사 {copied} / 대상 {actual})")

    print(json.dumps({"target": target.describe(), "tables": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
