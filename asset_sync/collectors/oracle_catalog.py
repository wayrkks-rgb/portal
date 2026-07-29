"""Read-only Oracle data dictionary browser.

The daily collector needs one asset table (``oracle.asset_source``) and a
matching SELECT statement. When that table name is wrong or was never
registered, the asset query fails with ORA-00942 and the operator has no way to
find the correct object from the closed network. This module lets the admin
screen list the tables and views the read-only account can actually see,
inspect their columns and preview a few rows before committing to one.

Everything here is SELECT-only against ALL_* dictionary views, object names are
validated as Oracle identifiers, and all filters are passed as bind variables.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Iterable, Mapping

from ..utils.validation import validate_oracle_identifier
from .oracle_connection import OracleConnectionError, oracle_connection

LOGGER = logging.getLogger(__name__)

MAX_TABLE_ROWS = 500
MAX_PREVIEW_ROWS = 50
MAX_CELL_LENGTH = 200

# Oracle-maintained schemas never hold ITSM asset data and only add noise to the
# picker. They stay reachable with include_system=True for rare debugging.
SYSTEM_SCHEMAS = (
    "SYS", "SYSTEM", "OUTLN", "XDB", "CTXSYS", "MDSYS", "OLAPSYS", "ORDSYS", "ORDDATA",
    "ORDPLUGINS", "DBSNMP", "WMSYS", "APPQOSSYS", "AUDSYS", "DVSYS", "DVF", "LBACSYS",
    "GSMADMIN_INTERNAL", "OJVMSYS", "DBSFWUSER", "REMOTE_SCHEDULER_AGENT", "EXFSYS",
    "SI_INFORMTN_SCHEMA", "ORACLE_OCM", "ANONYMOUS", "FLOWS_FILES", "MDDATA",
    "SPATIAL_CSW_ADMIN_USR", "SPATIAL_WFS_ADMIN_USR", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC",
)

# Column names that mark a table as a plausible ITSM asset master. Ordered by how
# strongly each one identifies an asset table.
ASSET_SIGNATURE_COLUMNS = (
    "CM_ID", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_NAME", "CM_OS", "CM_OS_VERSION",
    "CM_SVR_CAT_CD", "CM_STA_CD", "CM_CAT_CD", "CM_CPU_CORE_CNT", "CM_CPU_CNT",
    "CM_MEMORY", "CM_SERIAL_NO", "CM_MODEL_NAME",
)


class OracleCatalogError(RuntimeError):
    pass


def _system_schema_predicate(alias: str) -> str:
    quoted = ", ".join(f"'{name}'" for name in SYSTEM_SCHEMAS)
    return f"{alias}.OWNER NOT IN ({quoted})"


def _like_pattern(keyword: str) -> str:
    escaped = keyword.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _cell_value(value: Any) -> Any:
    """Convert an Oracle value into something JSON serialisable and bounded."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<BINARY {len(value)} bytes>"
    reader = getattr(value, "read", None)
    if callable(reader):
        try:
            value = reader()
        except Exception:  # pragma: no cover - driver specific LOB failures
            return "<LOB>"
        if isinstance(value, (bytes, bytearray)):
            return f"<BINARY {len(value)} bytes>"
    text = str(value)
    return text if len(text) <= MAX_CELL_LENGTH else text[:MAX_CELL_LENGTH] + "…"


def _clamp(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


class OracleCatalogBrowser:
    """List and preview the objects a read-only Oracle account can query."""

    def __init__(self, config: Any) -> None:
        self.config = config

    # -- helpers ---------------------------------------------------------
    def _rows(self, sql: str, binds: Mapping[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
        try:
            with oracle_connection(self.config.oracle) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, dict(binds))
                    columns = [str(column[0]).upper() for column in cursor.description]
                    return columns, list(cursor.fetchall())
        except OracleConnectionError:
            raise
        except Exception as exc:
            LOGGER.exception("Oracle catalog query failed")
            raise OracleCatalogError(str(exc)) from exc

    def current_user(self) -> str:
        _, rows = self._rows("SELECT USER FROM DUAL", {})
        return str(rows[0][0]).upper() if rows else ""

    # -- browsing --------------------------------------------------------
    def list_tables(
        self,
        *,
        keyword: str = "",
        owner: str = "",
        limit: int = 200,
        include_system: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = ["tc.TABLE_TYPE IN ('TABLE', 'VIEW')"]
        binds: dict[str, Any] = {"max_rows": _clamp(limit, 200, MAX_TABLE_ROWS)}
        if not include_system:
            conditions.append(_system_schema_predicate("tc"))
        owner = str(owner or "").strip()
        if owner:
            binds["owner"] = validate_oracle_identifier(owner.upper(), "owner")
            conditions.append("tc.OWNER = :owner")
        keyword = str(keyword or "").strip()
        if keyword:
            binds["keyword"] = _like_pattern(keyword)
            conditions.append(
                "(UPPER(tc.TABLE_NAME) LIKE :keyword ESCAPE '\\'"
                " OR UPPER(tc.COMMENTS) LIKE :keyword ESCAPE '\\')"
            )
        sql = f"""
            SELECT OWNER, TABLE_NAME, TABLE_TYPE, COMMENTS, NUM_ROWS FROM (
                SELECT tc.OWNER, tc.TABLE_NAME, tc.TABLE_TYPE, tc.COMMENTS, t.NUM_ROWS
                FROM ALL_TAB_COMMENTS tc
                LEFT JOIN ALL_TABLES t
                       ON t.OWNER = tc.OWNER AND t.TABLE_NAME = tc.TABLE_NAME
                WHERE {' AND '.join(conditions)}
                ORDER BY tc.OWNER, tc.TABLE_NAME
            ) WHERE ROWNUM <= :max_rows
        """
        _, rows = self._rows(sql, binds)
        return [
            {
                "owner": str(row[0]),
                "table_name": str(row[1]),
                "full_name": f"{row[0]}.{row[1]}",
                "object_type": str(row[2]),
                "comments": _cell_value(row[3]),
                "num_rows": _cell_value(row[4]),
            }
            for row in rows
        ]

    def resolve_object(self, owner: str, table_name: str) -> dict[str, str]:
        """Find the accessible object, preferring the connected account's schema."""
        table_name = validate_oracle_identifier(str(table_name or "").strip().upper(), "table_name")
        owner = str(owner or "").strip()
        binds: dict[str, Any] = {"table_name": table_name}
        condition = ""
        if owner:
            binds["owner"] = validate_oracle_identifier(owner.upper(), "owner")
            condition = "AND OWNER = :owner"
        sql = f"""
            SELECT OWNER, TABLE_NAME, TABLE_TYPE FROM (
                SELECT OWNER, TABLE_NAME, TABLE_TYPE
                FROM ALL_TAB_COMMENTS
                WHERE TABLE_NAME = :table_name
                  AND TABLE_TYPE IN ('TABLE', 'VIEW')
                  {condition}
                ORDER BY CASE WHEN OWNER = USER THEN 0 ELSE 1 END, OWNER
            ) WHERE ROWNUM <= 1
        """
        _, rows = self._rows(sql, binds)
        if not rows:
            target = f"{owner}.{table_name}" if owner else table_name
            raise OracleCatalogError(
                f"조회 계정으로 접근할 수 있는 테이블/뷰가 아닙니다: {target}. "
                "테이블 목록에서 실제 이름을 확인하거나 DBA에게 SELECT 권한을 요청하세요."
            )
        return {"owner": str(rows[0][0]), "table_name": str(rows[0][1]), "object_type": str(rows[0][2])}

    def list_columns(self, owner: str, table_name: str) -> dict[str, Any]:
        target = self.resolve_object(owner, table_name)
        sql = """
            SELECT c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH, c.DATA_PRECISION,
                   c.DATA_SCALE, c.NULLABLE, c.COLUMN_ID, cc.COMMENTS
            FROM ALL_TAB_COLUMNS c
            LEFT JOIN ALL_COL_COMMENTS cc
                   ON cc.OWNER = c.OWNER
                  AND cc.TABLE_NAME = c.TABLE_NAME
                  AND cc.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.OWNER = :owner AND c.TABLE_NAME = :table_name
            ORDER BY c.COLUMN_ID
        """
        _, rows = self._rows(sql, {"owner": target["owner"], "table_name": target["table_name"]})
        columns = [
            {
                "column_name": str(row[0]),
                "data_type": str(row[1]),
                "data_length": _cell_value(row[2]),
                "data_precision": _cell_value(row[3]),
                "data_scale": _cell_value(row[4]),
                "nullable": str(row[5] or "") == "Y",
                "column_id": _cell_value(row[6]),
                "comments": _cell_value(row[7]),
            }
            for row in rows
        ]
        return {**target, "full_name": f"{target['owner']}.{target['table_name']}", "columns": columns}

    def preview_rows(self, owner: str, table_name: str, limit: int = 10) -> dict[str, Any]:
        target = self.resolve_object(owner, table_name)
        max_rows = _clamp(limit, 10, MAX_PREVIEW_ROWS)
        # Names come from the data dictionary and are re-validated, so quoting them
        # is exact and cannot inject SQL.
        source = '"{owner}"."{table}"'.format(
            owner=validate_oracle_identifier(target["owner"], "owner"),
            table=validate_oracle_identifier(target["table_name"], "table_name"),
        )
        sql = f"SELECT * FROM (SELECT * FROM {source}) WHERE ROWNUM <= :max_rows"
        columns, rows = self._rows(sql, {"max_rows": max_rows})
        return {
            **target,
            "full_name": f"{target['owner']}.{target['table_name']}",
            "columns": columns,
            "rows": [[_cell_value(value) for value in row] for row in rows],
            "row_count": len(rows),
        }

    def suggest_asset_sources(
        self,
        *,
        signature_columns: Iterable[str] = ASSET_SIGNATURE_COLUMNS,
        limit: int = 20,
        include_system: bool = False,
    ) -> list[dict[str, Any]]:
        """Rank accessible objects by how many ITSM asset columns they carry."""
        names = [str(name).strip().upper() for name in signature_columns if str(name).strip()]
        if not names:
            return []
        binds: dict[str, Any] = {"max_rows": _clamp(limit, 20, MAX_TABLE_ROWS)}
        placeholders = []
        for index, name in enumerate(names):
            key = f"sig{index}"
            binds[key] = name
            placeholders.append(f":{key}")
        matched = f"COUNT(CASE WHEN c.COLUMN_NAME IN ({', '.join(placeholders)}) THEN 1 END)"
        conditions = [_system_schema_predicate("c")] if not include_system else []
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
            SELECT OWNER, TABLE_NAME, MATCHED_COUNT, TOTAL_COLUMNS FROM (
                SELECT c.OWNER, c.TABLE_NAME,
                       {matched} AS MATCHED_COUNT,
                       COUNT(*) AS TOTAL_COLUMNS
                FROM ALL_TAB_COLUMNS c
                {where_clause}
                GROUP BY c.OWNER, c.TABLE_NAME
                HAVING {matched} > 0
                ORDER BY MATCHED_COUNT DESC, c.OWNER, c.TABLE_NAME
            ) WHERE ROWNUM <= :max_rows
        """
        _, rows = self._rows(sql, binds)
        total_signature = len(names)
        return [
            {
                "owner": str(row[0]),
                "table_name": str(row[1]),
                "full_name": f"{row[0]}.{row[1]}",
                "matched_count": int(row[2]),
                "total_columns": int(row[3]),
                "match_ratio": round(int(row[2]) / total_signature, 3),
            }
            for row in rows
        ]
