from __future__ import annotations

import sqlite3
from pathlib import Path

from asset_sync.db.manager import SQLiteManager
from asset_sync.db.migrations import LATEST_VERSION, apply_pending, column_exists, current_version
from asset_sync.repositories import AssetRepository

LEGACY_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT INTO schema_version(version) VALUES (1);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, action TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT, reason TEXT, before_json TEXT, after_json TEXT,
    created_at TEXT NOT NULL);
INSERT INTO audit_log(user_id, action, target_type, created_at)
VALUES ('legacy', 'UPDATE', 'old_row', '2026-01-01T00:00:00');
"""


def _legacy_db(tmp_path: Path) -> Path:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _indexes(manager: SQLiteManager, table: str) -> set[str]:
    with manager.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        ).fetchall()
    return {str(row["name"]) for row in rows}


def test_existing_database_gains_the_new_column(tmp_path: Path) -> None:
    """CREATE TABLE IF NOT EXISTS 는 기존 테이블에 컬럼을 추가하지 않는다."""
    db = _legacy_db(tmp_path)
    manager = SQLiteManager(db)
    with manager.connect() as conn:
        assert column_exists(conn, "audit_log", "module_id") is False

    manager.initialize()

    with manager.connect() as conn:
        assert column_exists(conn, "audit_log", "module_id") is True
        assert current_version(conn) == LATEST_VERSION
        row = conn.execute("SELECT user_id, module_id FROM audit_log WHERE user_id='legacy'").fetchone()
    # 기존 데이터는 보존되고 기본값이 채워진다.
    assert dict(row) == {"user_id": "legacy", "module_id": "portal"}
    assert "idx_audit_module" in _indexes(manager, "audit_log")


def test_fresh_database_also_gets_the_index(tmp_path: Path) -> None:
    """새 DB 는 컬럼을 CREATE TABLE 로 갖지만 인덱스는 마이그레이션이 만든다."""
    manager = SQLiteManager(tmp_path / "fresh.db")
    manager.initialize()
    with manager.connect() as conn:
        assert column_exists(conn, "audit_log", "module_id") is True
        assert current_version(conn) == LATEST_VERSION
    assert "idx_audit_module" in _indexes(manager, "audit_log")


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    manager = SQLiteManager(db)
    manager.initialize()
    # 두 번째 호출에서는 적용할 것이 없다.
    assert apply_pending(manager) == []
    manager.initialize()
    with manager.connect() as conn:
        assert current_version(conn) == LATEST_VERSION
        assert conn.execute("SELECT COUNT(*) AS cnt FROM audit_log").fetchone()["cnt"] == 1


def test_audit_records_the_writing_module(tmp_path: Path) -> None:
    manager = SQLiteManager(tmp_path / "audit.db")
    manager.initialize()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        repo.audit("hong", "CREATE", "thing", "1", None, {}, {"a": 1})  # 기본값
        repo.audit("hong", "UPDATE", "thing", "2", None, {}, {}, module_id="capacity")
        rows = conn.execute("SELECT module_id, action FROM audit_log ORDER BY id").fetchall()
    assert [(row["module_id"], row["action"]) for row in rows] == [("portal", "CREATE"), ("capacity", "UPDATE")]


def test_blank_module_id_falls_back_to_portal(tmp_path: Path) -> None:
    manager = SQLiteManager(tmp_path / "audit2.db")
    manager.initialize()
    with manager.connect() as conn:
        AssetRepository(conn).audit("hong", "X", "thing", None, None, {}, {}, module_id="")
        row = conn.execute("SELECT module_id FROM audit_log").fetchone()
    assert row["module_id"] == "portal"
