"""SQL dialect translation between SQLite and MySQL.

All application SQL is written once in the SQLite flavour that this project
already uses: ``?`` parameter markers and ``INSERT OR IGNORE``. Rewriting the
319 parameter markers spread over 19 files would be a large, error-prone diff,
so the differences are absorbed here instead and every statement passes through
``Dialect.translate`` on its way to the driver.

The rewrite is done with a small scanner rather than ``str.replace`` because a
naive replacement would also corrupt ``?`` and ``%`` inside string literals,
quoted identifiers and comments.
"""

from __future__ import annotations

import re
from typing import Iterator

_INSERT_OR_IGNORE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)


def _scan(sql: str) -> Iterator[tuple[str, bool]]:
    """Yield (chunk, is_code) pairs where is_code marks executable SQL text.

    Literals, quoted identifiers and comments are yielded with is_code=False so
    the caller leaves their contents alone.
    """
    index = 0
    length = len(sql)
    buffer: list[str] = []
    while index < length:
        char = sql[index]
        pair = sql[index : index + 2]
        if char in "'\"`":
            if buffer:
                yield "".join(buffer), True
                buffer = []
            quote = char
            end = index + 1
            while end < length:
                if sql[end] == "\\" and quote == "'":
                    # SQLite treats backslash literally, MySQL uses it as an escape.
                    # Skipping the next character is correct for MySQL and harmless
                    # for SQLite because the pair stays inside the literal either way.
                    end += 2
                    continue
                if sql[end] == quote:
                    if end + 1 < length and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            yield sql[index:end], False
            index = end
            continue
        if pair == "--":
            if buffer:
                yield "".join(buffer), True
                buffer = []
            end = sql.find("\n", index)
            end = length if end == -1 else end
            yield sql[index:end], False
            index = end
            continue
        if pair == "/*":
            if buffer:
                yield "".join(buffer), True
                buffer = []
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            yield sql[index:end], False
            index = end
            continue
        buffer.append(char)
        index += 1
    if buffer:
        yield "".join(buffer), True


def to_pyformat(sql: str, *, escape_percent: bool) -> str:
    """Convert ``?`` markers to ``%s`` for drivers using the pyformat style.

    ``escape_percent`` must be True whenever parameters are bound, because
    PyMySQL and mysql-connector apply ``%``-formatting to the whole statement in
    that case, so a literal ``%`` has to be doubled.
    """
    parts: list[str] = []
    for chunk, is_code in _scan(sql):
        if escape_percent:
            chunk = chunk.replace("%", "%%")
        if is_code:
            chunk = chunk.replace("?", "%s")
        parts.append(chunk)
    return "".join(parts)


def split_statements(script: str) -> list[str]:
    """Split a DDL script into statements, ignoring semicolons inside literals."""
    statements: list[str] = []
    current: list[str] = []
    for chunk, is_code in _scan(script):
        if not is_code:
            current.append(chunk)
            continue
        start = 0
        for position, char in enumerate(chunk):
            if char == ";":
                current.append(chunk[start:position])
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                start = position + 1
        current.append(chunk[start:])
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return [statement for statement in statements if not _is_comment_only(statement)]


def _is_comment_only(statement: str) -> bool:
    return not any(chunk.strip() for chunk, is_code in _scan(statement) if is_code)


class Dialect:
    """Per-engine SQL differences. The SQLite form is the canonical source."""

    name = "sqlite"
    placeholder = "?"
    supports_executescript = True

    def translate(self, sql: str, *, has_params: bool = False) -> str:
        return sql

    def split_script(self, script: str) -> list[str]:
        return split_statements(script)


class SQLiteDialect(Dialect):
    name = "sqlite"


class MySQLDialect(Dialect):
    name = "mysql"
    placeholder = "%s"
    supports_executescript = False

    def translate(self, sql: str, *, has_params: bool = False) -> str:
        sql = _INSERT_OR_IGNORE.sub(r"\1INSERT IGNORE INTO", sql)
        return to_pyformat(sql, escape_percent=has_params)


_DIALECTS = {"sqlite": SQLiteDialect, "mysql": MySQLDialect}


def get_dialect(engine: str) -> Dialect:
    key = str(engine or "sqlite").strip().lower()
    if key not in _DIALECTS:
        supported = ", ".join(sorted(_DIALECTS))
        raise ValueError(f"지원하지 않는 database.engine 값입니다: {engine!r} (사용 가능: {supported})")
    return _DIALECTS[key]()
