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
    assert mapping["CM_IP"]["forced"] is True
    assert "MGMT_IP AS CM_IP" in result["sql"]

    with pytest.raises(ValueError):
        build_asset_query(source_columns=["CM_ID"], itsm_cfg=ITSM_CFG, overrides={"CM_IP": "NOT_THERE"})


def test_blank_override_forces_null_instead_of_auto_match() -> None:
    """Auto-mapping can pick a wrong lookalike column; the operator must be able to unmap it."""
    auto = build_asset_query(source_columns=["CM_ID", "STATUS", "OPER_STATUS"], itsm_cfg=ITSM_CFG)
    auto_mapping = {item["logical_column"]: item["source_column"] for item in auto["mapping"]}
    assert auto_mapping["CM_STA_CD"] == "STATUS"

    corrected = build_asset_query(
        source_columns=["CM_ID", "STATUS", "OPER_STATUS"],
        itsm_cfg=ITSM_CFG,
        overrides={"CM_STA_CD": "OPER_STATUS"},
    )
    assert "OPER_STATUS AS CM_STA_CD" in corrected["sql"]

    unmapped = build_asset_query(
        source_columns=["CM_ID", "STATUS", "OPER_STATUS"],
        itsm_cfg=ITSM_CFG,
        overrides={"CM_STA_CD": ""},
    )
    item = next(row for row in unmapped["mapping"] if row["logical_column"] == "CM_STA_CD")
    assert item["source_column"] is None
    assert item["forced"] is True
    assert "NULL AS CM_STA_CD" in unmapped["sql"]
    assert "CM_STA_CD" in unmapped["missing_columns"]


def test_filter_clause_is_generated_from_selected_column_and_values() -> None:
    result = build_asset_query(
        source_columns=["CM_ID", "CM_CAT_CD"],
        itsm_cfg=ITSM_CFG,
        filter_column="cm_cat_cd",
        filter_values="HW0101, HW0102 , HW0104,,HW0101",
    )
    assert result["where_clause"] == "WHERE CM_CAT_CD IN ('HW0101', 'HW0102', 'HW0104')"
    assert result["filter_values"] == ["HW0101", "HW0102", "HW0104"]
    assert result["sql"].rstrip().endswith("WHERE CM_CAT_CD IN ('HW0101', 'HW0102', 'HW0104')")


def test_filter_values_are_escaped_as_literals() -> None:
    result = build_asset_query(
        source_columns=["CM_ID", "CM_CAT_CD"],
        itsm_cfg=ITSM_CFG,
        filter_column="CM_CAT_CD",
        filter_values=["HW' OR 1=1 --"],
    )
    assert result["where_clause"] == "WHERE CM_CAT_CD IN ('HW'' OR 1=1 --')"


def test_filter_rejects_unknown_column_and_incomplete_input() -> None:
    columns = ["CM_ID", "CM_CAT_CD"]
    with pytest.raises(ValueError):
        build_asset_query(source_columns=columns, itsm_cfg=ITSM_CFG, filter_column="NO_SUCH_COL", filter_values=["A"])
    with pytest.raises(ValueError):
        build_asset_query(source_columns=columns, itsm_cfg=ITSM_CFG, filter_column="CM_CAT_CD", filter_values=[])
    with pytest.raises(ValueError):
        build_asset_query(source_columns=columns, itsm_cfg=ITSM_CFG, filter_values=["HW0101"])
    with pytest.raises(ValueError):
        build_asset_query(
            source_columns=columns,
            itsm_cfg=ITSM_CFG,
            filter_column="CM_CAT_CD",
            filter_values=[f"CODE{index}" for index in range(60)],
        )


def test_no_filter_collects_every_row_and_says_so() -> None:
    result = build_asset_query(source_columns=["CM_ID", "CM_CAT_CD"], itsm_cfg=ITSM_CFG)
    assert result["where_clause"] == ""
    assert "WHERE" not in result["sql"].replace("-- ", "")
    assert "조회 조건 없음" in result["sql"]
    assert "CM_CAT_CD" in result["sql"]


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


# ── 조회 조건: 여러 조건과 비교 연산 ────────────────────────────────────────

FILTER_COLUMNS = ["CM_ID", "CM_CAT_CD", "CM_STA_CD", "CM_HOSTNAME", "CM_DESCR"]


def build_with(filters):
    return build_asset_query(source_columns=FILTER_COLUMNS, itsm_cfg=ITSM_CFG, filters=filters)


def test_several_conditions_must_all_hold() -> None:
    result = build_with([
        {"column": "CM_CAT_CD", "operator": "IN", "values": "HW0101, HW0102"},
        {"column": "CM_STA_CD", "operator": "=", "values": "USE"},
    ])
    assert result["where_clause"] == (
        "WHERE CM_CAT_CD IN ('HW0101', 'HW0102')\n  AND CM_STA_CD = 'USE'"
    )
    assert result["sql"].rstrip().endswith("AND CM_STA_CD = 'USE'")
    assert len(result["filters"]) == 2


def test_every_operator_produces_its_own_shape() -> None:
    cases = {
        "IN": "CM_STA_CD IN ('USE')",
        "NOT IN": "CM_STA_CD NOT IN ('USE')",
        "=": "CM_STA_CD = 'USE'",
        "!=": "CM_STA_CD != 'USE'",
        "LIKE": "CM_STA_CD LIKE 'USE'",
        "NOT LIKE": "CM_STA_CD NOT LIKE 'USE'",
    }
    for operator, expected in cases.items():
        result = build_with([{"column": "CM_STA_CD", "operator": operator, "values": ["USE"]}])
        assert result["where_clause"] == f"WHERE {expected}", operator


def test_null_operators_take_no_values() -> None:
    result = build_with([{"column": "CM_DESCR", "operator": "IS NOT NULL", "values": []}])
    assert result["where_clause"] == "WHERE CM_DESCR IS NOT NULL"
    with pytest.raises(ValueError, match="코드값을 넣지 않습니다"):
        build_with([{"column": "CM_DESCR", "operator": "IS NULL", "values": ["X"]}])


def test_single_value_operators_reject_a_list() -> None:
    with pytest.raises(ValueError, match="1개만"):
        build_with([{"column": "CM_STA_CD", "operator": "=", "values": ["A", "B"]}])


def test_an_unknown_operator_is_rejected_with_the_allowed_list() -> None:
    with pytest.raises(ValueError, match="지원하지 않는 조회 조건"):
        build_with([{"column": "CM_STA_CD", "operator": "DROP", "values": ["A"]}])


def test_a_condition_cannot_smuggle_sql_through_its_value() -> None:
    result = build_with([{"column": "CM_STA_CD", "operator": "=", "values": ["A' OR '1'='1"]}])
    assert result["where_clause"] == "WHERE CM_STA_CD = 'A'' OR ''1''=''1'"


def test_blank_rows_from_the_screen_are_ignored() -> None:
    """화면은 빈 줄을 하나 띄워 두므로, 그게 조건으로 세지면 안 된다."""
    result = build_with([
        {"column": "CM_CAT_CD", "operator": "IN", "values": "HW0101"},
        {"column": "", "operator": "IN", "values": ""},
    ])
    assert len(result["filters"]) == 1
    assert result["where_clause"] == "WHERE CM_CAT_CD IN ('HW0101')"


def test_the_older_single_condition_form_still_works() -> None:
    """저장된 설정과 예전 화면이 그대로 남아 있어도 같은 결과여야 한다."""
    old = build_asset_query(
        source_columns=FILTER_COLUMNS, itsm_cfg=ITSM_CFG,
        filter_column="CM_CAT_CD", filter_values=["HW0101"],
    )
    new = build_with([{"column": "CM_CAT_CD", "operator": "IN", "values": ["HW0101"]}])
    assert old["where_clause"] == new["where_clause"]
    assert old["filter_column"] == "CM_CAT_CD"
    assert old["filter_values"] == ["HW0101"]


def test_too_many_conditions_are_refused() -> None:
    with pytest.raises(ValueError, match="최대"):
        build_with([{"column": "CM_STA_CD", "operator": "=", "values": [str(n)]} for n in range(11)])
