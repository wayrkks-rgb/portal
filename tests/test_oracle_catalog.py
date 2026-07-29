from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from asset_sync.collectors import oracle_catalog
from asset_sync.collectors.oracle_catalog import OracleCatalogBrowser, OracleCatalogError
from asset_sync.collectors.oracle_query_builder import build_asset_query, logical_columns
from asset_sync.config import load_config
from asset_sync.settings_store import LocalSettingsStore, SettingsValidationError

ITSM_CFG = {
    "cpu_compare_field": "CM_CPU_CORE_CNT",
    "memory_field": "CM_MEMORY",
    "os_eos_field": "OS_EOS_DATE",
}


class FakeCursor:
    """Minimal cursor recording the SQL and binds the browser sends to Oracle."""

    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.description: list[tuple[str, ...]] = []
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        self.connection.calls.append((sql, dict(binds or {})))
        columns, rows = self.connection.result_for(sql)
        self.description = [(name,) for name in columns]
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, results: list[tuple[list[str], list[tuple[Any, ...]]]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._index = 0

    def result_for(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        result = self.results[min(self._index, len(self.results) - 1)]
        self._index += 1
        return result

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _browser(monkeypatch, results: list[tuple[list[str], list[tuple[Any, ...]]]]) -> tuple[OracleCatalogBrowser, FakeConnection]:
    connection = FakeConnection(results)

    class _Ctx:
        def __enter__(self) -> FakeConnection:
            return connection

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(oracle_catalog, "oracle_connection", lambda cfg: _Ctx())

    class _Config:
        oracle = {"user": "u", "password": "p", "host": "h", "service_name": "s"}

    return OracleCatalogBrowser(_Config()), connection


def test_list_tables_filters_system_schemas_and_binds_keyword(monkeypatch) -> None:
    browser, connection = _browser(
        monkeypatch,
        [(["OWNER", "TABLE_NAME", "TABLE_TYPE", "COMMENTS", "NUM_ROWS"], [("ITSM", "TB_ASSET", "TABLE", "자산", 1200)])],
    )
    tables = browser.list_tables(keyword="asset", limit=50)

    sql, binds = connection.calls[0]
    assert "'SYS'" in sql and "OWNER NOT IN" in sql
    assert binds["keyword"] == "%ASSET%"
    assert binds["max_rows"] == 50
    assert tables == [
        {
            "owner": "ITSM",
            "table_name": "TB_ASSET",
            "full_name": "ITSM.TB_ASSET",
            "object_type": "TABLE",
            "comments": "자산",
            "num_rows": 1200,
        }
    ]


def test_list_tables_escapes_like_wildcards(monkeypatch) -> None:
    browser, connection = _browser(monkeypatch, [(["OWNER", "TABLE_NAME", "TABLE_TYPE", "COMMENTS", "NUM_ROWS"], [])])
    browser.list_tables(keyword="a_b%c")
    assert connection.calls[0][1]["keyword"] == "%A\\_B\\%C%"


def test_list_tables_rejects_invalid_owner(monkeypatch) -> None:
    browser, _ = _browser(monkeypatch, [(["OWNER"], [])])
    with pytest.raises(ValueError):
        browser.list_tables(owner="ITSM; DROP TABLE X")


def test_resolve_object_reports_missing_table(monkeypatch) -> None:
    browser, _ = _browser(monkeypatch, [(["OWNER", "TABLE_NAME", "TABLE_TYPE"], [])])
    with pytest.raises(OracleCatalogError) as error:
        browser.list_columns("", "TB_MISSING")
    assert "TB_MISSING" in str(error.value)


def test_preview_rows_quotes_names_and_limits(monkeypatch) -> None:
    browser, connection = _browser(
        monkeypatch,
        [
            (["OWNER", "TABLE_NAME", "TABLE_TYPE"], [("ITSM", "TB_ASSET", "TABLE")]),
            (["CM_ID", "CM_REG_DTTM"], [("A001", dt.datetime(2026, 1, 2, 3, 4, 5))]),
        ],
    )
    result = browser.preview_rows("itsm", "tb_asset", limit=999)

    sql, binds = connection.calls[1]
    assert '"ITSM"."TB_ASSET"' in sql
    assert binds["max_rows"] == oracle_catalog.MAX_PREVIEW_ROWS
    assert result["rows"] == [["A001", "2026-01-02T03:04:05"]]


def test_suggest_asset_sources_ranks_by_signature_columns(monkeypatch) -> None:
    browser, connection = _browser(
        monkeypatch,
        [(["OWNER", "TABLE_NAME", "MATCHED_COUNT", "TOTAL_COLUMNS"], [("ITSM", "TB_ASSET", 12, 40)])],
    )
    candidates = browser.suggest_asset_sources(signature_columns=["CM_ID", "CM_HOSTNAME"], limit=5)

    _, binds = connection.calls[0]
    assert binds["sig0"] == "CM_ID" and binds["sig1"] == "CM_HOSTNAME"
    assert candidates[0]["full_name"] == "ITSM.TB_ASSET"
    assert candidates[0]["matched_count"] == 12


def test_build_asset_query_maps_exact_and_missing_columns() -> None:
    result = build_asset_query(
        source_columns=["CM_ID", "CM_HOSTNAME", "CM_IP", "CM_OS"],
        itsm_cfg=ITSM_CFG,
        asset_source="ITSM.TB_ASSET",
        generated_at=dt.datetime(2026, 7, 29, 9, 0, 0),
    )
    sql = result["sql"]

    assert "FROM ${ASSET_SOURCE}" in sql
    assert "    CM_ID" in sql
    assert "NULL AS CM_SUB_IP" in sql
    assert "NULL AS ${CPU_FIELD}" in sql
    assert "NULL AS ${EOS_FIELD}" in sql
    assert "CM_SUB_IP" in result["missing_required_columns"]
    assert result["total_count"] == len(logical_columns(ITSM_CFG))


def test_build_asset_query_auto_maps_site_naming() -> None:
    result = build_asset_query(
        source_columns=["ASSET_ID", "HOSTNAME", "IP_ADDR", "SUB_IP", "OS_NAME", "OS_VERSION", "CORE_CNT", "MEMORY_SIZE", "EOS_DATE", "SVR_CAT_CD", "STATUS_CD"],
        itsm_cfg=ITSM_CFG,
        asset_source="ITSM.SERVER_MASTER",
    )
    mapping = {item["logical_column"]: item["source_column"] for item in result["mapping"]}

    assert mapping["CM_ID"] == "ASSET_ID"
    assert mapping["CM_HOSTNAME"] == "HOSTNAME"
    assert mapping["CM_IP"] == "IP_ADDR"
    assert mapping["CM_SUB_IP"] == "SUB_IP"
    assert mapping["CM_CPU_CORE_CNT"] == "CORE_CNT"
    assert mapping["CM_MEMORY"] == "MEMORY_SIZE"
    assert mapping["OS_EOS_DATE"] == "EOS_DATE"
    assert result["missing_required_columns"] == []
    assert "ASSET_ID AS CM_ID" in result["sql"]
    assert "CORE_CNT AS ${CPU_FIELD}" in result["sql"]


def test_build_asset_query_honours_manual_override() -> None:
    result = build_asset_query(
        source_columns=["CM_ID", "PRIMARY_IP", "MGMT_IP"],
        itsm_cfg=ITSM_CFG,
        overrides={"CM_IP": "MGMT_IP"},
    )
    mapping = {item["logical_column"]: item for item in result["mapping"]}
    assert mapping["CM_IP"]["source_column"] == "MGMT_IP"
    assert mapping["CM_IP"]["match_type"] == "OVERRIDE"

    with pytest.raises(ValueError):
        build_asset_query(source_columns=["CM_ID"], itsm_cfg=ITSM_CFG, overrides={"CM_IP": "NOT_THERE"})


def test_generated_query_passes_collector_placeholder_substitution() -> None:
    result = build_asset_query(source_columns=["CM_ID"], itsm_cfg=ITSM_CFG, asset_source="ITSM.TB_ASSET")
    sql = (
        result["sql"]
        .replace("${ASSET_SOURCE}", "ITSM.TB_ASSET")
        .replace("${EOS_FIELD}", "OS_EOS_DATE")
        .replace("${CPU_FIELD}", "CM_CPU_CORE_CNT")
    )
    assert "${" not in sql
    assert "FROM ITSM.TB_ASSET" in sql
    for required in ("CM_ID", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_OS", "CM_OS_VERSION", "CM_SVR_CAT_CD", "CM_STA_CD", "CM_CPU_CORE_CNT", "CM_MEMORY", "OS_EOS_DATE"):
        assert required in sql


def test_save_asset_source_writes_query_file_and_config(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: ORACLE\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    store = LocalSettingsStore(tmp_path)

    generated = build_asset_query(source_columns=["CM_ID"], itsm_cfg=ITSM_CFG, asset_source="ITSM.TB_ASSET")
    saved = store.save_asset_source("itsm.tb_asset", query_sql=generated["sql"])

    assert saved["oracle"]["asset_source"] == "ITSM.TB_ASSET"
    assert saved["oracle"]["query_file_exists"] is True
    query_path = tmp_path / "config" / "oracle_query.local.sql"
    assert "FROM ${ASSET_SOURCE}" in query_path.read_text(encoding="utf-8")
    assert load_config().oracle["asset_source"] == "ITSM.TB_ASSET"


def test_save_asset_source_rejects_unsafe_values(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: ORACLE\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    store = LocalSettingsStore(tmp_path)

    with pytest.raises(SettingsValidationError):
        store.save_asset_source("")
    with pytest.raises(SettingsValidationError):
        store.save_asset_source("TB_ASSET; DROP TABLE X")
    with pytest.raises(SettingsValidationError):
        store.save_asset_source("TB_ASSET", query_file="../../escape.sql")
