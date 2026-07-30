"""버전별 스키마 마이그레이션.

``CREATE TABLE IF NOT EXISTS`` 는 새 테이블만 만든다. 이미 운영 중인 DB 에 컬럼을
추가하려면 별도 단계가 필요하고, 그 이력을 ``schema_version`` 에 남겨야 두 번
적용되지 않는다.

**규칙**: 기존 테이블에 컬럼을 추가할 때 그 컬럼을 참조하는 인덱스를
``schema.sql`` 에 넣으면 안 된다. 기존 DB 에서는 ``CREATE TABLE IF NOT EXISTS`` 가
no-op 이라 컬럼 없이 인덱스를 만들려 해 스키마 적용 자체가 실패한다. 컬럼은
``CREATE TABLE`` 에 두고 인덱스는 이 파일에서 만든다.

각 단계는 **적용 여부를 스스로 확인**한다. 새로 만든 DB 는 최신 스키마를 이미
갖고 있으므로 같은 ALTER 를 다시 실행하면 실패하기 때문이다. 여러 WAS 가 동시에
기동해 같은 DDL 을 돌리는 경우도 같은 이유로 무해하게 넘어간다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

# 중복 DDL 을 나타내는 드라이버 메시지. 엔진마다 문구가 달라 부분 문자열로 본다.
_ALREADY_APPLIED = ("duplicate column", "already exists", "duplicate key name")


def _engine(conn: Any) -> str:
    return str(getattr(getattr(conn, "dialect", None), "name", "sqlite"))


def column_exists(conn: Any, table: str, column: str) -> bool:
    if _engine(conn) == "mysql":
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?
            """,
            (table, column),
        ).fetchone()
        return bool(row and int(row["cnt"]))
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row["name"]) == column for row in rows)


def _apply_step(conn: Any, sql: str) -> None:
    try:
        conn.execute(sql)
    except Exception as exc:
        if any(token in str(exc).lower() for token in _ALREADY_APPLIED):
            # 다른 WAS 가 먼저 적용했거나 이미 최신 스키마다.
            LOGGER.info("이미 적용된 마이그레이션 단계를 건너뜁니다: %s", str(exc)[:120])
            return
        raise


def _add_audit_module_id(conn: Any) -> bool:
    """v2: 여러 WAS 가 같은 감사 테이블에 쓰므로 모듈 구분 컬럼이 필요하다.

    컬럼과 인덱스를 따로 확인한다. 새로 만든 DB 는 CREATE TABLE 로 컬럼을 이미
    갖지만 인덱스는 없기 때문이다.
    """
    mysql = _engine(conn) == "mysql"
    changed = False
    if not column_exists(conn, "audit_log", "module_id"):
        _apply_step(
            conn,
            "ALTER TABLE audit_log ADD COLUMN module_id VARCHAR(64) NOT NULL DEFAULT 'portal'"
            if mysql
            else "ALTER TABLE audit_log ADD COLUMN module_id TEXT NOT NULL DEFAULT 'portal'",
        )
        changed = True
    _apply_step(
        conn,
        "CREATE INDEX idx_audit_module ON audit_log(module_id, created_at DESC)"
        if mysql
        else "CREATE INDEX IF NOT EXISTS idx_audit_module ON audit_log(module_id, created_at DESC)",
    )
    return changed


# (버전, 설명, 적용 함수). 함수는 실제로 바꾼 것이 있으면 True 를 돌려준다.
MIGRATIONS: list[tuple[int, str, Callable[[Any], bool]]] = [
    (2, "audit_log.module_id 추가", _add_audit_module_id),
]

LATEST_VERSION = max((version for version, _, _ in MIGRATIONS), default=1)


def current_version(conn: Any) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    if not row or row["version"] is None:
        return 0
    return int(row["version"])


def apply_pending(manager: Any) -> list[int]:
    """미적용 마이그레이션을 순서대로 실행하고 적용한 버전 목록을 돌려준다."""
    applied: list[int] = []
    with manager.connect() as conn:
        version = current_version(conn)
        for target, description, step in MIGRATIONS:
            if target <= version:
                continue
            changed = step(conn)
            conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (target,))
            applied.append(target)
            LOGGER.info(
                "스키마 마이그레이션 적용: v%d %s%s", target, description, "" if changed else " (이미 반영됨)"
            )
    return applied


def migrate(manager: Any) -> list[int]:
    """스키마를 만들고 미적용 마이그레이션까지 적용한다."""
    manager.initialize()
    return apply_pending(manager)
