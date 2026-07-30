"""이름 기반 스키마 마이그레이션.

``CREATE TABLE IF NOT EXISTS`` 는 새 테이블만 만든다. 이미 운영 중인 DB 에 컬럼을
추가하려면 별도 단계가 필요하고, 그 이력을 남겨야 두 번 적용되지 않는다.

**왜 버전 숫자가 아니라 이름인가**: 담당자들이 각자 branch 에서 마이그레이션을
추가한다. 숫자를 쓰면 A 와 B 가 모두 ``3`` 을 쓰고, 병합에서 한쪽만 남거나 -- 더
나쁘게 -- 이미 ``3`` 이 찍힌 DB 에서 다른 쪽 단계가 **조용히 영구 스킵**된다.
이름은 서로 겹치지 않으므로 병합 충돌도 없고 스킵도 없다. 이력은
``schema_migration`` 테이블에 이름으로 기록한다.

**규칙**
1. 기존 테이블에 컬럼을 추가할 때 그 컬럼을 참조하는 인덱스를 ``schema.sql`` 에
   넣으면 안 된다. 기존 DB 에서는 ``CREATE TABLE IF NOT EXISTS`` 가 no-op 이라
   컬럼 없이 인덱스를 만들려 해 스키마 적용 자체가 실패한다. 컬럼은
   ``CREATE TABLE`` 에 두고 인덱스는 마이그레이션에서 만든다.
2. 각 단계는 **적용 여부를 스스로 확인**한다. 새로 만든 DB 는 최신 스키마를 이미
   갖고 있으므로 같은 ALTER 를 다시 실행하면 실패하기 때문이다. 여러 WAS 가 동시에
   기동해 같은 DDL 을 돌리는 경우도 같은 이유로 무해하게 넘어간다.
3. 모듈 담당자는 이 파일을 고치지 않는다. ``asset_sync/db/modules/<id>_migrations.py``
   에 ``MIGRATIONS`` 를 두면 자동으로 발견된다. 자세한 계약은
   ``asset_sync/db/modules/README.md`` 를 본다.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Callable, Iterable

LOGGER = logging.getLogger(__name__)

# 중복 DDL 을 나타내는 드라이버 메시지. 엔진마다 문구가 달라 부분 문자열로 본다.
_ALREADY_APPLIED = ("duplicate column", "already exists", "duplicate key name")

MODULES_DIR = Path(__file__).with_name("modules")
MODULES_PACKAGE = f"{__package__}.modules"

#: (이름, 설명, 적용 함수). 함수는 실제로 바꾼 것이 있으면 True 를 돌려준다.
Migration = tuple[str, str, Callable[[Any], bool]]


class MigrationError(RuntimeError):
    pass


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


def table_exists(conn: Any, table: str) -> bool:
    if _engine(conn) == "mysql":
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?
            """,
            (table,),
        ).fetchone()
        return bool(row and int(row["cnt"]))
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row and int(row["cnt"]))


def apply_step(conn: Any, sql: str) -> None:
    """DDL 을 실행하되 '이미 적용됨' 계열 오류는 성공으로 취급한다."""
    try:
        conn.execute(sql)
    except Exception as exc:
        if any(token in str(exc).lower() for token in _ALREADY_APPLIED):
            # 다른 WAS 가 먼저 적용했거나 이미 최신 스키마다.
            LOGGER.info("이미 적용된 마이그레이션 단계를 건너뜁니다: %s", str(exc)[:120])
            return
        raise


# 이전 이름. 모듈 마이그레이션에서 쓰기 편하도록 공개 이름을 두었다.
_apply_step = apply_step


def _add_audit_module_id(conn: Any) -> bool:
    """여러 WAS 가 같은 감사 테이블에 쓰므로 모듈 구분 컬럼이 필요하다.

    컬럼과 인덱스를 따로 확인한다. 새로 만든 DB 는 CREATE TABLE 로 컬럼을 이미
    갖지만 인덱스는 없기 때문이다.
    """
    mysql = _engine(conn) == "mysql"
    changed = False
    if not column_exists(conn, "audit_log", "module_id"):
        apply_step(
            conn,
            "ALTER TABLE audit_log ADD COLUMN module_id VARCHAR(64) NOT NULL DEFAULT 'portal'"
            if mysql
            else "ALTER TABLE audit_log ADD COLUMN module_id TEXT NOT NULL DEFAULT 'portal'",
        )
        changed = True
    apply_step(
        conn,
        "CREATE INDEX idx_audit_module ON audit_log(module_id, created_at DESC)"
        if mysql
        else "CREATE INDEX IF NOT EXISTS idx_audit_module ON audit_log(module_id, created_at DESC)",
    )
    return changed


#: 통합 웹(포털)이 소유하는 마이그레이션. 이름 앞에 ``core/`` 가 붙는다.
CORE_MIGRATIONS: list[Migration] = [
    ("audit_module_id", "audit_log.module_id 추가", _add_audit_module_id),
]


def _normalize(namespace: str, entries: Iterable[Any], *, source: str) -> list[Migration]:
    normalized: list[Migration] = []
    for entry in entries:
        try:
            name, description, step = entry
        except (TypeError, ValueError) as exc:
            raise MigrationError(
                f"{source}: MIGRATIONS 항목은 (이름, 설명, 함수) 3-튜플이어야 합니다: {entry!r}"
            ) from exc
        name = str(name).strip()
        if not name:
            raise MigrationError(f"{source}: 마이그레이션 이름이 비어 있습니다.")
        if "/" in name:
            raise MigrationError(f"{source}: 마이그레이션 이름에 '/' 를 쓸 수 없습니다: {name!r}")
        if not callable(step):
            raise MigrationError(f"{source}: {name} 의 적용 함수가 호출 가능하지 않습니다.")
        normalized.append((f"{namespace}/{name}", str(description), step))
    return normalized


def discover_module_migrations() -> list[Migration]:
    """``modules/<id>_migrations.py`` 의 ``MIGRATIONS`` 를 모아 온다.

    파일 이름 순서로 적용한다. 모듈 간 순서를 보장하지 않으므로 다른 모듈의
    테이블에 의존하는 단계는 두지 않는다.
    """
    if not MODULES_DIR.is_dir():
        return []
    collected: list[Migration] = []
    for path in sorted(MODULES_DIR.glob("*_migrations.py")):
        module_id = path.name[: -len("_migrations.py")]
        if module_id.startswith("_"):
            continue
        imported = importlib.import_module(f"{MODULES_PACKAGE}.{path.stem}")
        entries = getattr(imported, "MIGRATIONS", None)
        if entries is None:
            LOGGER.warning("%s 에 MIGRATIONS 가 없어 건너뜁니다.", path.name)
            continue
        collected.extend(_normalize(module_id, entries, source=path.name))
    return collected


def all_migrations() -> list[Migration]:
    """코어 + 모듈 마이그레이션. 이름 중복은 즉시 실패로 알린다."""
    migrations = _normalize("core", CORE_MIGRATIONS, source="migrations.py")
    migrations.extend(discover_module_migrations())
    seen: set[str] = set()
    for name, _, _ in migrations:
        if name in seen:
            raise MigrationError(f"마이그레이션 이름이 중복되었습니다: {name}")
        seen.add(name)
    return migrations


def ensure_history_table(conn: Any) -> None:
    """이력 테이블을 만든다.

    ``schema.sql`` 이 아니라 여기서 만든다. 이 테이블은 스키마 적용보다 먼저
    있어야 하고, 옛 DB(테이블이 없는 상태)에서도 조회 가능해야 한다.
    """
    if _engine(conn) == "mysql":
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                name VARCHAR(190) NOT NULL PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            name TEXT NOT NULL PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def applied_names(conn: Any) -> set[str]:
    ensure_history_table(conn)
    rows = conn.execute("SELECT name FROM schema_migration").fetchall()
    return {str(row["name"]) for row in rows}


def pending_names(conn: Any) -> list[str]:
    done = applied_names(conn)
    return [name for name, _, _ in all_migrations() if name not in done]


def apply_pending(manager: Any) -> list[str]:
    """미적용 마이그레이션을 실행하고 적용한 이름 목록을 돌려준다."""
    applied: list[str] = []
    migrations = all_migrations()
    with manager.connect() as conn:
        done = applied_names(conn)
        for name, description, step in migrations:
            if name in done:
                continue
            changed = step(conn)
            conn.execute("INSERT OR IGNORE INTO schema_migration(name) VALUES (?)", (name,))
            applied.append(name)
            LOGGER.info(
                "스키마 마이그레이션 적용: %s %s%s", name, description, "" if changed else " (이미 반영됨)"
            )
    return applied


def migrate(manager: Any) -> list[str]:
    """스키마를 만들고 미적용 마이그레이션까지 적용한다."""
    manager.initialize()
    return apply_pending(manager)
