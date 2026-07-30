"""대메뉴(모듈)별 스키마 파일 발견과 검증.

담당자마다 공용 ``schema.sql`` 을 고치면 branch 를 병합할 때마다 같은 파일에서
충돌한다. 그래서 각 모듈은 자기 파일만 추가한다.

    asset_sync/db/modules/<module_id>.sql         -- SQLite 용
    asset_sync/db/modules/<module_id>.mysql.sql   -- MySQL 용

두 파일은 **함께** 있어야 한다. 한쪽만 두면 다른 엔진으로 배포했을 때 테이블이
조용히 없는 상태가 되므로, 기동 시점에 바로 실패로 알린다.

**테이블 이름 규칙**: 모듈이 만드는 객체 이름은 ``<module_id>_`` 로 시작해야 한다
(인덱스는 ``idx_<module_id>_`` 처럼 흔한 접두어를 앞에 붙여도 된다). 하나의 DB 를
여러 담당자가 공유하므로 이름이 겹치면 서로의 테이블을 덮어쓴다.

**만들 수 있는 것**: ``CREATE TABLE`` / ``CREATE INDEX`` / ``CREATE VIEW`` /
자기 테이블에 대한 ``INSERT``. 기존 테이블을 바꾸는 ``ALTER`` 나 ``DROP`` 은 여기서
막는다. 그건 이력이 남아야 하므로 ``<module_id>_migrations.py`` 로 간다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable

from .dialects import _scan, split_statements

LOGGER = logging.getLogger(__name__)

MODULES_DIR = Path(__file__).with_name("modules")

MYSQL_SUFFIX = ".mysql.sql"
SQLITE_SUFFIX = ".sql"

# 인덱스/제약에 흔히 쓰는 접두어. 이 뒤에 모듈 접두어가 오면 통과시킨다.
_OBJECT_PREFIXES = ("idx_", "ix_", "uq_", "ux_", "uniq_", "fk_")

_QUOTES = "\"'`[]"

_CREATE_TABLE = re.compile(r"^CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)", re.IGNORECASE)
_CREATE_INDEX = re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)", re.IGNORECASE)
_CREATE_VIEW = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)", re.IGNORECASE)
_INSERT_INTO = re.compile(r"^INSERT\s+(?:OR\s+\w+\s+|IGNORE\s+)?INTO\s+(\S+)", re.IGNORECASE)
_INDEX_ON = re.compile(r"\bON\s+(\S+?)\s*\(", re.IGNORECASE)


class ModuleSchemaError(RuntimeError):
    pass


class ModuleSchemaFile:
    """검증을 통과한 모듈 스키마 파일 하나."""

    def __init__(self, module_id: str, path: Path, statements: list[str]) -> None:
        self.module_id = module_id
        self.path = path
        self.statements = statements

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        return f"ModuleSchemaFile({self.module_id!r}, {self.path.name!r})"


def _unquote(name: str) -> str:
    cleaned = name.strip().strip(",;").strip()
    for quote in _QUOTES:
        cleaned = cleaned.replace(quote, "")
    # schema.table 형태면 테이블 이름만 본다.
    return cleaned.rsplit(".", 1)[-1]


def _prefix_ok(name: str, module_id: str) -> bool:
    lowered = name.lower()
    prefix = f"{module_id.lower()}_"
    if lowered.startswith(prefix):
        return True
    return any(
        lowered.startswith(known) and lowered[len(known) :].startswith(prefix)
        for known in _OBJECT_PREFIXES
    )


def _without_comments(statement: str) -> str:
    """주석만 지운다.

    ``split_statements`` 는 문장 앞에 붙은 주석을 그대로 남기므로 그것부터 걷어내야
    문장이 어떤 종류인지 알 수 있다. 따옴표로 감싼 식별자(``"capacity_x"``)는 이름
    판정에 필요하니 남긴다.
    """
    parts = [
        chunk
        for chunk, is_code in _scan(statement)
        if is_code or not chunk.lstrip().startswith(("--", "/*"))
    ]
    return " ".join("".join(parts).split())


def _check_statement(module_id: str, path: Path, statement: str) -> None:
    text = _without_comments(statement)
    where = f"{path.name}: "

    for pattern in (_CREATE_TABLE, _CREATE_VIEW, _INSERT_INTO):
        match = pattern.match(text)
        if not match:
            continue
        name = _unquote(match.group(1))
        if not _prefix_ok(name, module_id):
            raise ModuleSchemaError(
                f"{where}{name!r} 은 {module_id}_ 로 시작해야 합니다. "
                "하나의 DB 를 여러 담당자가 공유하므로 이름 충돌을 막기 위한 규칙입니다."
            )
        return

    match = _CREATE_INDEX.match(text)
    if match:
        index_name = _unquote(match.group(1))
        if not _prefix_ok(index_name, module_id):
            raise ModuleSchemaError(
                f"{where}인덱스 {index_name!r} 은 {module_id}_ 또는 idx_{module_id}_ 로 시작해야 합니다."
            )
        target = _INDEX_ON.search(text)
        if target and not _prefix_ok(_unquote(target.group(1)), module_id):
            raise ModuleSchemaError(
                f"{where}{_unquote(target.group(1))!r} 은 다른 담당자의 테이블입니다. "
                "모듈 스키마 파일은 자기 테이블만 다룹니다."
            )
        return

    verb = text.split(None, 1)[0].upper() if text.split() else "?"
    if verb in {"ALTER", "DROP", "TRUNCATE", "RENAME"}:
        raise ModuleSchemaError(
            f"{where}{verb} 는 모듈 스키마 파일에 둘 수 없습니다. 이미 배포된 DB 에서는 "
            f"적용 이력이 남아야 하므로 {module_id}_migrations.py 로 옮기세요."
        )
    raise ModuleSchemaError(
        f"{where}허용되지 않는 문장입니다({verb}). CREATE TABLE / CREATE INDEX / CREATE VIEW / "
        "자기 테이블에 대한 INSERT 만 쓸 수 있습니다."
    )


def validate_module_schema(module_id: str, path: Path, script: str) -> list[str]:
    """스크립트를 문장으로 쪼개고 이름 규칙을 확인한 뒤 문장 목록을 돌려준다."""
    statements = split_statements(script)
    for statement in statements:
        _check_statement(module_id, path, statement)
    return statements


def _module_ids(paths: Iterable[Path]) -> set[str]:
    found: set[str] = set()
    for path in paths:
        name = path.name
        if name.startswith("_"):
            continue
        if name.endswith(MYSQL_SUFFIX):
            found.add(name[: -len(MYSQL_SUFFIX)])
        elif name.endswith(SQLITE_SUFFIX):
            found.add(name[: -len(SQLITE_SUFFIX)])
    return found


def discover_module_schemas(engine: str, *, directory: Path | None = None) -> list[ModuleSchemaFile]:
    """엔진에 맞는 모듈 스키마 파일을 모듈 ID 순서로 돌려준다."""
    base = directory or MODULES_DIR
    if not base.is_dir():
        return []
    suffix = MYSQL_SUFFIX if str(engine).strip().lower() == "mysql" else SQLITE_SUFFIX
    files: list[ModuleSchemaFile] = []
    for module_id in sorted(_module_ids(base.glob("*.sql"))):
        sqlite_path = base / f"{module_id}{SQLITE_SUFFIX}"
        mysql_path = base / f"{module_id}{MYSQL_SUFFIX}"
        missing = [path.name for path in (sqlite_path, mysql_path) if not path.is_file()]
        if missing:
            raise ModuleSchemaError(
                f"모듈 {module_id!r} 의 스키마 파일이 짝을 이루지 않습니다: {', '.join(missing)} 없음. "
                "SQLite 와 MySQL 양쪽 파일을 함께 두어야 어느 엔진으로 배포해도 테이블이 만들어집니다."
            )
        path = mysql_path if suffix == MYSQL_SUFFIX else sqlite_path
        script = path.read_text(encoding="utf-8")
        files.append(ModuleSchemaFile(module_id, path, validate_module_schema(module_id, path, script)))
    return files


def apply_module_schemas(conn: Any, engine: str, *, directory: Path | None = None) -> list[str]:
    """발견한 모듈 스키마를 적용하고 적용된 모듈 ID 목록을 돌려준다."""
    applied: list[str] = []
    for schema in discover_module_schemas(engine, directory=directory):
        for statement in schema.statements:
            conn.execute(statement)
        applied.append(schema.module_id)
        LOGGER.info("모듈 스키마 적용: %s (%d 문장)", schema.module_id, len(schema.statements))
    return applied
