from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from asset_sync.db import migrations as migrations_module
from asset_sync.db.manager import SQLiteManager
from asset_sync.db.migrations import (
    MigrationError,
    all_migrations,
    applied_names,
    apply_pending,
    column_exists,
    pending_names,
    table_exists,
)
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


def _names(manager: SQLiteManager) -> set[str]:
    with manager.connect() as conn:
        return applied_names(conn)


def test_existing_database_gains_the_new_column(tmp_path: Path) -> None:
    """CREATE TABLE IF NOT EXISTS 는 기존 테이블에 컬럼을 추가하지 않는다."""
    db = _legacy_db(tmp_path)
    manager = SQLiteManager(db)
    with manager.connect() as conn:
        assert column_exists(conn, "audit_log", "module_id") is False

    manager.initialize()

    with manager.connect() as conn:
        assert column_exists(conn, "audit_log", "module_id") is True
        row = conn.execute("SELECT user_id, module_id FROM audit_log WHERE user_id='legacy'").fetchone()
    # 기존 데이터는 보존되고 기본값이 채워진다.
    assert dict(row) == {"user_id": "legacy", "module_id": "portal"}
    assert "idx_audit_module" in _indexes(manager, "audit_log")
    assert "core/audit_module_id" in _names(manager)


def test_fresh_database_also_gets_the_index(tmp_path: Path) -> None:
    """새 DB 는 컬럼을 CREATE TABLE 로 갖지만 인덱스는 마이그레이션이 만든다."""
    manager = SQLiteManager(tmp_path / "fresh.db")
    manager.initialize()
    with manager.connect() as conn:
        assert column_exists(conn, "audit_log", "module_id") is True
        assert pending_names(conn) == []
    assert "idx_audit_module" in _indexes(manager, "audit_log")


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    manager = SQLiteManager(db)
    manager.initialize()
    # 두 번째 호출에서는 적용할 것이 없다.
    assert apply_pending(manager) == []
    manager.initialize()
    with manager.connect() as conn:
        assert pending_names(conn) == []
        assert conn.execute("SELECT COUNT(*) AS cnt FROM audit_log").fetchone()["cnt"] == 1


def test_history_table_is_created_on_a_legacy_database(tmp_path: Path) -> None:
    """이력 테이블은 schema.sql 이 아니라 마이그레이션 코드가 만든다.

    옛 DB 에는 그 테이블이 없는데도 '무엇이 적용됐나' 를 먼저 조회해야 한다.
    """
    db = _legacy_db(tmp_path)
    manager = SQLiteManager(db)
    with manager.connect() as conn:
        assert table_exists(conn, "schema_migration") is False
    manager.initialize()
    with manager.connect() as conn:
        assert table_exists(conn, "schema_migration") is True


def test_names_are_namespaced_and_unique() -> None:
    names = [name for name, _, _ in all_migrations()]
    assert names == sorted(set(names), key=names.index)  # 중복 없음
    assert all("/" in name for name in names)
    assert "core/audit_module_id" in names


def test_module_migrations_are_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """담당자는 공용 파일을 고치지 않고 자기 파일만 추가한다."""
    package = tmp_path / "fake_modules"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "capacity_migrations.py").write_text(
        textwrap.dedent(
            """
            from asset_sync.db.migrations import apply_step

            def _create(conn):
                apply_step(conn, "CREATE TABLE IF NOT EXISTS capacity_note (id INTEGER PRIMARY KEY)")
                return True

            MIGRATIONS = [("create_note", "capacity_note 생성", _create)]
            """
        ),
        encoding="utf-8",
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(migrations_module, "MODULES_DIR", package)
    monkeypatch.setattr(migrations_module, "MODULES_PACKAGE", "fake_modules")
    try:
        names = [name for name, _, _ in all_migrations()]
        assert "capacity/create_note" in names
        manager = SQLiteManager(tmp_path / "mod.db")
        manager.initialize()
        with manager.connect() as conn:
            assert table_exists(conn, "capacity_note") is True
            assert "capacity/create_note" in applied_names(conn)
    finally:
        sys.path.remove(str(tmp_path))


def test_duplicate_names_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    step = lambda conn: False  # noqa: E731
    monkeypatch.setattr(
        migrations_module,
        "CORE_MIGRATIONS",
        [("dup", "a", step), ("dup", "b", step)],
    )
    monkeypatch.setattr(migrations_module, "discover_module_migrations", lambda: [])
    with pytest.raises(MigrationError, match="중복"):
        all_migrations()


def test_slash_in_a_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """이름은 namespace 로 한 단계만 쓴다. 직접 '/' 를 넣으면 충돌 판정이 어긋난다."""
    monkeypatch.setattr(
        migrations_module, "CORE_MIGRATIONS", [("core/other", "x", lambda conn: False)]
    )
    monkeypatch.setattr(migrations_module, "discover_module_migrations", lambda: [])
    with pytest.raises(MigrationError, match="'/'"):
        all_migrations()


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
