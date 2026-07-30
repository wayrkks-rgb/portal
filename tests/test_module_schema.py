from __future__ import annotations

from pathlib import Path

import pytest

from asset_sync.db.manager import SQLiteManager
from asset_sync.db.module_schema import (
    MYSQL_SUFFIX,
    ModuleSchemaError,
    apply_module_schemas,
    discover_module_schemas,
    validate_module_schema,
)

CAPACITY_SQLITE = """
-- 담당자가 추가하는 파일. 공용 schema.sql 은 건드리지 않는다.
CREATE TABLE IF NOT EXISTS capacity_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_name TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capacity_snapshot_time ON capacity_snapshot(captured_at DESC);
INSERT OR IGNORE INTO capacity_snapshot(id, cluster_name, captured_at) VALUES (1, 'seed', '2026-01-01');
"""

CAPACITY_MYSQL = """
CREATE TABLE IF NOT EXISTS capacity_snapshot (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    cluster_name VARCHAR(128) NOT NULL,
    captured_at VARCHAR(32) NOT NULL,
    KEY idx_capacity_snapshot_time (captured_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _write_pair(directory: Path, module_id: str, sqlite_sql: str, mysql_sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{module_id}.sql").write_text(sqlite_sql, encoding="utf-8")
    (directory / f"{module_id}{MYSQL_SUFFIX}").write_text(mysql_sql, encoding="utf-8")


def test_discovery_reads_the_engine_specific_file(tmp_path: Path) -> None:
    _write_pair(tmp_path, "capacity", CAPACITY_SQLITE, CAPACITY_MYSQL)
    sqlite_files = discover_module_schemas("sqlite", directory=tmp_path)
    mysql_files = discover_module_schemas("mysql", directory=tmp_path)
    assert [file.module_id for file in sqlite_files] == ["capacity"]
    assert sqlite_files[0].path.name == "capacity.sql"
    assert mysql_files[0].path.name == f"capacity{MYSQL_SUFFIX}"
    # 주석만 있는 줄은 문장으로 세지 않는다.
    assert len(sqlite_files[0].statements) == 3


def test_discovery_is_sorted_by_module_id(tmp_path: Path) -> None:
    _write_pair(tmp_path, "capacity", CAPACITY_SQLITE, CAPACITY_MYSQL)
    _write_pair(
        tmp_path,
        "backup",
        "CREATE TABLE IF NOT EXISTS backup_job (id INTEGER PRIMARY KEY);",
        "CREATE TABLE IF NOT EXISTS backup_job (id BIGINT PRIMARY KEY);",
    )
    assert [file.module_id for file in discover_module_schemas("sqlite", directory=tmp_path)] == [
        "backup",
        "capacity",
    ]


def test_a_missing_engine_file_fails_at_startup(tmp_path: Path) -> None:
    """한쪽만 두면 다른 엔진으로 배포했을 때 테이블이 조용히 없는 상태가 된다."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "capacity.sql").write_text(CAPACITY_SQLITE, encoding="utf-8")
    with pytest.raises(ModuleSchemaError, match="capacity.mysql.sql"):
        discover_module_schemas("sqlite", directory=tmp_path)


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert discover_module_schemas("sqlite", directory=tmp_path / "nope") == []


def test_table_without_the_module_prefix_is_rejected() -> None:
    with pytest.raises(ModuleSchemaError, match="capacity_"):
        validate_module_schema(
            "capacity", Path("capacity.sql"), "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER);"
        )


def test_index_on_another_owners_table_is_rejected() -> None:
    with pytest.raises(ModuleSchemaError, match="다른 담당자"):
        validate_module_schema(
            "capacity",
            Path("capacity.sql"),
            "CREATE INDEX IF NOT EXISTS idx_capacity_x ON audit_log(created_at);",
        )


def test_insert_into_another_owners_table_is_rejected() -> None:
    with pytest.raises(ModuleSchemaError, match="capacity_"):
        validate_module_schema(
            "capacity", Path("capacity.sql"), "INSERT INTO app_user(user_id) VALUES ('x');"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE capacity_snapshot ADD COLUMN note TEXT",
        "DROP TABLE capacity_snapshot",
        "TRUNCATE TABLE capacity_snapshot",
    ],
)
def test_destructive_statements_point_at_migrations(statement: str) -> None:
    with pytest.raises(ModuleSchemaError, match="capacity_migrations.py"):
        validate_module_schema("capacity", Path("capacity.sql"), statement + ";")


def test_common_index_prefixes_are_allowed() -> None:
    script = """
    CREATE TABLE IF NOT EXISTS capacity_snapshot (id INTEGER PRIMARY KEY, cluster_name TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_capacity_snapshot_cluster ON capacity_snapshot(cluster_name);
    CREATE VIEW IF NOT EXISTS capacity_latest AS SELECT * FROM capacity_snapshot;
    """
    assert len(validate_module_schema("capacity", Path("capacity.sql"), script)) == 3


def test_a_semicolon_inside_a_literal_does_not_split_the_statement() -> None:
    statements = validate_module_schema(
        "capacity",
        Path("capacity.sql"),
        "INSERT INTO capacity_snapshot(cluster_name) VALUES ('a;b');",
    )
    assert len(statements) == 1


def test_module_tables_are_created_alongside_the_shared_schema(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    _write_pair(modules, "capacity", CAPACITY_SQLITE, CAPACITY_MYSQL)
    manager = SQLiteManager(tmp_path / "portal.db")
    manager.initialize()  # 공용 스키마만
    with manager.connect() as conn:
        assert apply_module_schemas(conn, "sqlite", directory=modules) == ["capacity"]
        # 재적용해도 안전하다. 기동할 때마다 실행되기 때문이다.
        assert apply_module_schemas(conn, "sqlite", directory=modules) == ["capacity"]
        rows = conn.execute("SELECT cluster_name FROM capacity_snapshot").fetchall()
        # 공용 테이블도 그대로 있다.
        assert conn.execute("SELECT COUNT(*) AS cnt FROM app_user").fetchone() is not None
    assert [row["cluster_name"] for row in rows] == ["seed"]
