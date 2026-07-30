"""Engine-neutral connection wrapper.

Existing code calls ``conn.execute(sql, params).fetchone()`` and reads columns by
name (``row["cm_id"]``, ``dict(row)``). Both work on SQLite through
``sqlite3.Row``. This wrapper keeps that exact surface on MySQL by translating
the statement and using a dictionary cursor, so no repository or service code
needs to change.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from .dialects import Dialect

LOGGER = logging.getLogger(__name__)


def _first_line(statement: str) -> str:
    """Identify a failing DDL statement without dumping the whole table body."""
    head = " ".join(statement.split())
    return head if len(head) <= 160 else head[:157] + "..."


class ManagedConnection:
    """Thin wrapper exposing the small sqlite3-like API the project relies on."""

    def __init__(self, raw: Any, dialect: Dialect, *, dictionary_cursor: bool = False) -> None:
        self._raw = raw
        self._dialect = dialect
        self._dictionary_cursor = dictionary_cursor

    @property
    def raw(self) -> Any:
        """Underlying driver connection, for engine-specific operations."""
        return self._raw

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def _cursor(self) -> Any:
        if self._dictionary_cursor:
            # buffered=True so a following statement on the same connection does not
            # fail with "Unread result found" when rows were only partly consumed.
            try:
                return self._raw.cursor(dictionary=True, buffered=True)
            except TypeError:
                # PyMySQL selects the row type through a cursor class instead.
                import pymysql.cursors  # type: ignore

                return self._raw.cursor(pymysql.cursors.DictCursor)
        return self._raw.cursor()

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        statement = self._dialect.translate(sql, has_params=bool(params))
        cursor = self._cursor()
        if params:
            cursor.execute(statement, tuple(params))
        else:
            # Both drivers skip parameter interpolation entirely with a single
            # argument, which keeps a literal % in a parameterless statement safe.
            cursor.execute(statement)
        return cursor

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> Any:
        rows = [tuple(item) for item in seq_of_params]
        statement = self._dialect.translate(sql, has_params=True)
        cursor = self._cursor()
        if rows:
            cursor.executemany(statement, rows)
        return cursor

    def executescript(self, script: str) -> None:
        if self._dialect.supports_executescript:
            self._raw.executescript(script)
            return
        cursor = self._cursor()
        for statement in self._dialect.split_script(script):
            try:
                cursor.execute(self._dialect.translate(statement))
            except Exception as exc:
                # 드라이버 메시지만으로는 어느 테이블인지 알 수 없다. 스키마 파일에
                # 문장이 수십 개라 이것이 없으면 담당자가 찾아낼 수단이 없다.
                raise type(exc)(f"{exc}\n  ↳ 실패한 문장: {_first_line(statement)}") from exc

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def cursor(self) -> Any:
        return self._cursor()
