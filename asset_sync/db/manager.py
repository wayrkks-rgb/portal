"""Database managers for SQLite and MySQL.

SQLite stays the default so the demo mode, the tests and a single-host install
keep working with no external server. MySQL is selected with
``database.engine: mysql`` and is the supported option once several WAS instances
share one database.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .connection import ManagedConnection
from .dialects import Dialect, MySQLDialect, SQLiteDialect

LOGGER = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    pass


def _apply_module_schemas(conn: Any, engine: str) -> None:
    """대메뉴별 스키마 파일을 공용 스키마 뒤에 적용한다.

    담당자마다 ``schema.sql`` 을 고치면 branch 병합에서 매번 충돌하므로 각자
    ``db/modules/<id>.sql`` 을 추가한다. 계약은 그 폴더의 README.md 를 본다.
    """
    from .module_schema import apply_module_schemas

    apply_module_schemas(conn, engine)


class DatabaseManager:
    """Common interface: create the schema, then hand out transactions."""

    dialect: Dialect
    engine: str = "sqlite"

    def initialize(self) -> None:
        raise NotImplementedError

    @contextmanager
    def connect(self) -> Iterator[ManagedConnection]:
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable target for health checks and logs, without secrets."""
        return self.engine


class SQLiteManager(DatabaseManager):
    """SQLite connection and transaction manager."""

    engine = "sqlite"

    def __init__(self, database_path: Path, schema_path: Path | None = None) -> None:
        self.database_path = Path(database_path)
        self.schema_path = schema_path or Path(__file__).with_name("schema.sql")
        self.dialect = SQLiteDialect()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self.schema_path.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(schema)
            conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (1)")
            _apply_module_schemas(conn, self.engine)
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        # 기존 DB 에 컬럼을 추가하는 단계는 CREATE TABLE IF NOT EXISTS 로 처리되지 않는다.
        from .migrations import apply_pending

        apply_pending(self)

    @contextmanager
    def connect(self) -> Iterator[ManagedConnection]:
        raw = sqlite3.connect(self.database_path, timeout=30)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA busy_timeout=30000")
        conn = ManagedConnection(raw, self.dialect)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def describe(self) -> str:
        return f"sqlite:{self.database_path}"


def _import_mysql_driver() -> tuple[Any, str]:
    """Prefer PyMySQL: pure Python, so one wheel is enough in a closed network."""
    try:
        import pymysql  # type: ignore

        return pymysql, "pymysql"
    except ImportError:
        pass
    try:
        import mysql.connector  # type: ignore

        return mysql.connector, "mysql-connector-python"
    except ImportError as exc:
        raise DatabaseError(
            "MySQL 드라이버가 설치되지 않았습니다. requirements-mysql.txt의 PyMySQL을 설치하세요."
        ) from exc


class MySQLManager(DatabaseManager):
    """MySQL connection and transaction manager for multi-WAS operation."""

    engine = "mysql"

    def __init__(self, settings: Mapping[str, Any], schema_path: Path | None = None) -> None:
        self.schema_path = schema_path or Path(__file__).with_name("schema_mysql.sql")
        self.dialect = MySQLDialect()
        self.host = str(settings.get("host") or "").strip()
        self.database = str(settings.get("database") or "").strip()
        self.user = str(settings.get("user") or "").strip()
        self.password = str(settings.get("password") or "")
        self.charset = str(settings.get("charset") or "utf8mb4")
        try:
            self.port = int(settings.get("port") or 3306)
        except (TypeError, ValueError) as exc:
            raise DatabaseError("MySQL Port는 숫자여야 합니다.") from exc
        try:
            self.connect_timeout = int(settings.get("connect_timeout_seconds") or 10)
        except (TypeError, ValueError) as exc:
            raise DatabaseError("MySQL connect_timeout_seconds는 숫자여야 합니다.") from exc
        missing = [
            label
            for label, value in (("host", self.host), ("database", self.database), ("user", self.user))
            if not value
        ]
        if missing:
            raise DatabaseError(f"MySQL 설정이 필요합니다: {', '.join(missing)}")

    def _connect_kwargs(self, driver_name: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
            "autocommit": False,
        }
        if driver_name == "pymysql":
            kwargs["connect_timeout"] = self.connect_timeout
        else:
            kwargs["connection_timeout"] = self.connect_timeout
        return kwargs

    def initialize(self) -> None:
        schema = self.schema_path.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(schema)
            conn.execute("INSERT IGNORE INTO schema_version(version) VALUES (1)")
            _apply_module_schemas(conn, self.engine)
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        from .migrations import apply_pending

        apply_pending(self)

    @contextmanager
    def connect(self) -> Iterator[ManagedConnection]:
        driver, driver_name = _import_mysql_driver()
        raw = driver.connect(**self._connect_kwargs(driver_name))
        conn = ManagedConnection(raw, self.dialect, dictionary_cursor=True)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def describe(self) -> str:
        return f"mysql:{self.host}:{self.port}/{self.database}"


def create_manager(config: Any) -> DatabaseManager:
    """Build the manager the configuration asks for.

    Every entry point (web, batch jobs, scripts, dashboard) goes through here so
    the engine is chosen in exactly one place.
    """
    settings = getattr(config, "database", None) or {}
    engine = str(settings.get("engine") or "sqlite").strip().lower()
    if engine == "sqlite":
        return SQLiteManager(config.database_path)
    if engine == "mysql":
        return MySQLManager(settings.get("mysql") or {})
    raise DatabaseError(f"지원하지 않는 database.engine 값입니다: {engine!r} (사용 가능: sqlite, mysql)")
