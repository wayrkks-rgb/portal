from __future__ import annotations

from pathlib import Path

import pytest

from asset_sync.config import load_config
from asset_sync.db.dialects import MySQLDialect, SQLiteDialect, get_dialect, split_statements, to_pyformat
from asset_sync.db.locks import DatabaseLock, LockNotAcquired
from asset_sync.db.manager import DatabaseError, MySQLManager, SQLiteManager, create_manager
from asset_sync.repositories import AssetRepository


def _config(tmp_path: Path, monkeypatch, body: str = "") -> object:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n" + body, encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    return load_config()


# -- 방언 번역 ---------------------------------------------------------------

def test_placeholders_are_rewritten_only_outside_literals() -> None:
    sql = "SELECT * FROM t WHERE a=? AND b='who? me' AND c=? -- trailing ? comment"
    translated = to_pyformat(sql, escape_percent=False)
    assert translated == "SELECT * FROM t WHERE a=%s AND b='who? me' AND c=%s -- trailing ? comment"


def test_percent_is_doubled_only_when_parameters_are_bound() -> None:
    sql = "SELECT * FROM t WHERE name LIKE '%abc%' AND id=?"
    assert to_pyformat(sql, escape_percent=True) == "SELECT * FROM t WHERE name LIKE '%%abc%%' AND id=%s"
    assert to_pyformat(sql, escape_percent=False) == "SELECT * FROM t WHERE name LIKE '%abc%' AND id=%s"


def test_quoted_identifiers_and_block_comments_are_preserved() -> None:
    sql = 'SELECT "col?name" /* keep ? here */ FROM t WHERE x=?'
    assert to_pyformat(sql, escape_percent=False) == 'SELECT "col?name" /* keep ? here */ FROM t WHERE x=%s'


def test_mysql_dialect_rewrites_insert_or_ignore() -> None:
    dialect = MySQLDialect()
    assert dialect.translate("INSERT OR IGNORE INTO t(a) VALUES (?)", has_params=True) == (
        "INSERT IGNORE INTO t(a) VALUES (%s)"
    )
    # 값 안의 같은 문구는 건드리지 않는다.
    kept = dialect.translate("INSERT INTO t(a) VALUES ('INSERT OR IGNORE INTO x')", has_params=False)
    assert kept == "INSERT INTO t(a) VALUES ('INSERT OR IGNORE INTO x')"


def test_sqlite_dialect_leaves_sql_untouched() -> None:
    sql = "INSERT OR IGNORE INTO t(a) VALUES (?)"
    assert SQLiteDialect().translate(sql, has_params=True) == sql


def test_get_dialect_rejects_unknown_engine() -> None:
    assert get_dialect("MySQL").name == "mysql"
    with pytest.raises(ValueError):
        get_dialect("postgres")


def test_split_statements_ignores_semicolons_inside_literals() -> None:
    script = """
    -- comment; not a statement
    CREATE TABLE a (x TEXT DEFAULT 'a;b');
    CREATE TABLE b (y TEXT);
    """
    statements = split_statements(script)
    assert len(statements) == 2
    assert statements[0].endswith("'a;b')")


def _sql_code_only(text: str) -> str:
    """Executable text with comments and literals removed, for schema assertions."""
    from asset_sync.db.dialects import _scan

    return "".join(chunk for chunk, is_code in _scan(text) if is_code)


def test_real_mysql_schema_splits_into_statements() -> None:
    import re

    schema = (Path(__file__).resolve().parents[1] / "asset_sync" / "db" / "schema_mysql.sql").read_text(encoding="utf-8")
    statements = split_statements(schema)
    assert len(statements) == len(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
    # 선행 주석은 문장에 붙어 있어도 되지만, 실행되는 DDL 은 문장당 하나여야 한다.
    assert all(_sql_code_only(statement).count("CREATE TABLE") == 1 for statement in statements)
    # MySQL 은 CREATE INDEX 에 IF NOT EXISTS 를 지원하지 않으므로 인라인 정의여야 한다.
    assert "CREATE INDEX" not in _sql_code_only(schema).upper()
    # MySQL 은 TEXT/BLOB 컬럼에 DEFAULT 를 허용하지 않는다.
    assert "TEXT NOT NULL DEFAULT" not in _sql_code_only(schema).upper()


def test_mysql_schema_mirrors_sqlite_tables() -> None:
    db_dir = Path(__file__).resolve().parents[1] / "asset_sync" / "db"
    import re

    def tables(path: Path) -> set[str]:
        text = path.read_text(encoding="utf-8")
        return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text))

    assert tables(db_dir / "schema.sql") == tables(db_dir / "schema_mysql.sql")


# -- 매니저 선택 -------------------------------------------------------------

def test_create_manager_defaults_to_sqlite(tmp_path: Path, monkeypatch) -> None:
    manager = create_manager(_config(tmp_path, monkeypatch))
    assert isinstance(manager, SQLiteManager)
    assert manager.engine == "sqlite"
    assert manager.describe().startswith("sqlite:")


def test_create_manager_selects_mysql_from_config(tmp_path: Path, monkeypatch) -> None:
    body = (
        "database:\n  engine: mysql\n  mysql:\n    host: db.example.invalid\n"
        "    port: 3306\n    database: assetdb\n    user: asset\n    password: secret\n"
    )
    manager = create_manager(_config(tmp_path, monkeypatch, body))
    assert isinstance(manager, MySQLManager)
    assert manager.describe() == "mysql:db.example.invalid:3306/assetdb"
    assert manager.dialect.name == "mysql"


def test_mysql_engine_can_be_selected_by_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASSET_DB_ENGINE", "mysql")
    monkeypatch.setenv("MYSQL_HOST", "10.0.0.9")
    monkeypatch.setenv("MYSQL_DATABASE", "assetdb")
    monkeypatch.setenv("MYSQL_USER", "asset")
    monkeypatch.setenv("MYSQL_PASSWORD", "secret")
    cfg = _config(tmp_path, monkeypatch)
    assert cfg.database["engine"] == "mysql"
    assert create_manager(cfg).describe() == "mysql:10.0.0.9:3306/assetdb"


def test_mysql_manager_requires_connection_values() -> None:
    with pytest.raises(DatabaseError) as error:
        MySQLManager({"host": "", "database": "", "user": ""})
    assert "host" in str(error.value)


def test_create_manager_rejects_unknown_engine(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path, monkeypatch, "database:\n  engine: postgres\n")
    with pytest.raises(DatabaseError):
        create_manager(cfg)


# -- 다중 WAS 배치 잠금 ------------------------------------------------------

def _manager(tmp_path: Path, monkeypatch) -> SQLiteManager:
    manager = create_manager(_config(tmp_path, monkeypatch))
    manager.initialize()
    return manager


def test_second_was_cannot_take_a_held_lock(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    first = DatabaseLock(manager, "daily_batch", owner="was-1:100")
    first.acquire()
    try:
        with pytest.raises(LockNotAcquired) as error:
            DatabaseLock(manager, "daily_batch", owner="was-2:200").acquire()
        assert "was-1:100" in str(error.value)
    finally:
        first.release()

    # 해제 후에는 다른 WAS 가 정상적으로 획득한다.
    second = DatabaseLock(manager, "daily_batch", owner="was-2:200")
    second.acquire()
    assert second.acquired is True
    second.release()


def test_expired_lock_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    crashed = DatabaseLock(manager, "daily_batch", lease_seconds=-1, owner="was-1:100")
    crashed.acquire()
    # 리스가 이미 지났으므로 죽은 보유자를 회수할 수 있어야 한다.
    survivor = DatabaseLock(manager, "daily_batch", owner="was-2:200")
    survivor.acquire()
    assert survivor.acquired is True
    with manager.connect() as conn:
        row = conn.execute("SELECT owner FROM process_lock WHERE lock_name='daily_batch'").fetchone()
    assert row["owner"] == "was-2:200"
    survivor.release()


def test_release_only_removes_own_lock(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    holder = DatabaseLock(manager, "daily_batch", owner="was-1:100")
    holder.acquire()
    intruder = DatabaseLock(manager, "daily_batch", owner="was-2:200")
    intruder.acquired = True  # 획득하지 않았는데 해제를 시도하는 상황
    intruder.release()
    with manager.connect() as conn:
        row = conn.execute("SELECT owner FROM process_lock WHERE lock_name='daily_batch'").fetchone()
    assert row is not None and row["owner"] == "was-1:100"
    holder.release()


def test_lock_context_manager_releases_on_error(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        with DatabaseLock(manager, "daily_batch", owner="was-1:100"):
            raise RuntimeError("배치 실패")
    with manager.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS cnt FROM process_lock").fetchone()["cnt"] == 0


# -- 래핑된 커넥션이 기존 호출 형태를 유지하는지 -----------------------------

def test_wrapped_connection_keeps_sqlite_call_style(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run_id = repo.start_collection_run("ITSM", "2026-07-30T07:00:00")
        assert isinstance(run_id, int) and run_id > 0
        row = conn.execute("SELECT source, status FROM collection_run WHERE id=?", (run_id,)).fetchone()
        assert row["source"] == "ITSM"
        assert dict(row)["status"] == "RUNNING"
        # 파라미터가 없는 문장, executemany, 기본값 의존 INSERT 모두 동작해야 한다.
        assert conn.execute("SELECT COUNT(*) AS cnt FROM collection_run").fetchone()["cnt"] == 1
        conn.executemany(
            "INSERT INTO process_lock(lock_name, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            [("a", "o", "t", "t"), ("b", "o", "t", "t")],
        )
        assert conn.execute("SELECT COUNT(*) AS cnt FROM process_lock").fetchone()["cnt"] == 2
