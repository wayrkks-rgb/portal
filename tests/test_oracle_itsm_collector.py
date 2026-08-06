"""수집 실패 메시지가 원인·단계·경과시간을 모두 담는지 확인한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asset_sync.collectors import oracle_itsm_collector as collector_module
from asset_sync.collectors.oracle_itsm_collector import (
    OracleCollectionError,
    OracleITSMCollector,
)
from asset_sync.config import AppConfig

COLUMNS = [
    "CM_ID", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_OS", "CM_OS_VERSION",
    "CM_SVR_CAT_CD", "CM_STA_CD", "CM_CPU_CORE_CNT", "CM_MEMORY", "OS_EOS_DATE",
]


def make_config(tmp_path: Path) -> AppConfig:
    query = tmp_path / "config" / "oracle_query.local.sql"
    query.parent.mkdir(parents=True, exist_ok=True)
    query.write_text("SELECT * FROM ${TABLE_NAME}", encoding="utf-8")
    return AppConfig(
        root_dir=tmp_path,
        oracle={
            "mode": "thin", "host": "db", "port": 1521, "service_name": "orcl",
            "user": "reader", "password": "secret", "asset_source": "ITSM.TB_ASSET",
            "query_file": "config/oracle_query.local.sql", "fetch_size": 2,
        },
        itsm={"os_eos_field": "OS_EOS_DATE", "cpu_compare_field": "CM_CPU_CORE_CNT",
              "memory_field": "CM_MEMORY"},
    )


class FakeLob:
    """CLOB 손잡이. 연결이 닫히면 실제 드라이버처럼 읽기를 거부한다."""

    def __init__(self, text: str, connection: "FakeConnection") -> None:
        self._text = text
        self._connection = connection

    def read(self) -> str:
        if self._connection.closed:
            raise RuntimeError("DPY-1001: not connected to database")
        return self._text

    def __str__(self) -> str:
        return self.read()


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], fail_at: str | None) -> None:
        self._rows = rows
        self._fail_at = fail_at
        self.description = [(name,) for name in COLUMNS]
        self.arraysize = 100

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str) -> None:
        if self._fail_at == "EXECUTE":
            raise RuntimeError("ORA-00942: table or view does not exist")

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        if self._fail_at == "FETCH":
            # 실제 드라이버처럼, 끊긴 뒤 정리 과정에서 결과만 알려주는 오류로 덮인다.
            try:
                raise RuntimeError("DPY-4011: the database or network closed the connection")
            except RuntimeError:
                raise RuntimeError("DPY-1001: not connected to database")
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]], fail_at: str | None) -> None:
        self._rows = rows
        self._fail_at = fail_at
        self.call_timeout = 0
        self.outputtypehandler = None
        self.closed = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return FakeCursor(list(self._rows), self._fail_at)


class FakeDriverBase:
    """apply_lob_handler 가 참조하는 타입 상수만 갖춘 최소 드라이버."""

    DB_TYPE_CLOB = "CLOB"
    DB_TYPE_NCLOB = "NCLOB"
    DB_TYPE_BLOB = "BLOB"
    DB_TYPE_LONG = "LONG"
    DB_TYPE_LONG_RAW = "LONG_RAW"


def install_driver(monkeypatch, rows: list[tuple[Any, ...]], fail_at: str | None) -> list[FakeConnection]:
    opened: list[FakeConnection] = []

    class FakeDriver(FakeDriverBase):
        @staticmethod
        def connect(**kwargs: Any) -> FakeConnection:
            if fail_at == "CONNECT":
                raise RuntimeError("DPY-6005: cannot connect to database")
            connection = FakeConnection(rows, fail_at)
            opened.append(connection)
            return connection

    monkeypatch.setattr(collector_module, "prepare_driver", lambda cfg: FakeDriver)
    return opened


def row(index: int) -> tuple[Any, ...]:
    return tuple(f"{name}-{index}" for name in COLUMNS)


def test_collect_returns_rows(tmp_path, monkeypatch):
    install_driver(monkeypatch, [row(1), row(2), row(3)], None)
    records = OracleITSMCollector(make_config(tmp_path)).collect()
    assert len(records) == 3
    assert records[0]["CM_HOSTNAME"] == "CM_HOSTNAME-1"


def test_max_rows_stops_early(tmp_path, monkeypatch):
    install_driver(monkeypatch, [row(index) for index in range(10)], None)
    assert len(OracleITSMCollector(make_config(tmp_path)).collect(max_rows=3)) == 3


@pytest.mark.parametrize("stage", ["CONNECT", "EXECUTE", "FETCH"])
def test_failure_names_the_stage(tmp_path, monkeypatch, stage):
    install_driver(monkeypatch, [row(1)], stage)
    collector = OracleITSMCollector(make_config(tmp_path))
    with pytest.raises(OracleCollectionError) as caught:
        collector.collect()
    message = str(caught.value)
    assert f"단계={stage}" in message
    assert "경과=" in message
    assert collector_module.COLLECTOR_BUILD in message
    assert collector.last_metadata["failed_stage"] == stage


def test_fetch_failure_keeps_the_real_cause(tmp_path, monkeypatch):
    """DPY-1001 만 남으면 왜 끊겼는지 알 수 없다."""
    install_driver(monkeypatch, [row(1)], "FETCH")
    with pytest.raises(OracleCollectionError) as caught:
        OracleITSMCollector(make_config(tmp_path)).collect()
    message = str(caught.value)
    assert "DPY-1001" in message
    assert "DPY-4011" in message


def test_clob_columns_are_read_before_the_connection_closes(tmp_path, monkeypatch):
    """CM_DESCR 같은 CLOB 은 손잡이로 온다.

    값으로 바꾸지 않고 넘기면, 정규화 단계에서 json 직렬화하다가 연결이 이미 닫혀
    DPY-1001 이 난다. 수집기가 아니라 그 다음 단계에서 터지므로 단계 표시도 안 붙는다.
    """
    opened = install_driver(monkeypatch, [], None)

    class LobDriver(FakeDriverBase):
        @staticmethod
        def connect(**kwargs: Any) -> FakeConnection:
            connection = FakeConnection([], None)
            connection._rows = [
                tuple(FakeLob(f"{name}-내용", connection) for name in COLUMNS)
            ]
            opened.append(connection)
            return connection

    monkeypatch.setattr(collector_module, "prepare_driver", lambda cfg: LobDriver)

    records = OracleITSMCollector(make_config(tmp_path)).collect()

    assert opened[-1].closed, "연결이 닫힌 뒤를 재현해야 의미가 있다"
    # 연결이 닫힌 지금 읽어도 문제가 없어야 한다 = 이미 값이다.
    assert records[0]["CM_HOSTNAME"] == "CM_HOSTNAME-내용"
    import json

    assert json.dumps(records[0], ensure_ascii=False, default=str)


def test_the_lob_handler_is_registered_on_the_connection(tmp_path, monkeypatch):
    """행마다 따로 읽으면 왕복이 행 수만큼 늘어난다. 핸들러로 한 번에 받아야 한다."""
    opened = install_driver(monkeypatch, [row(1)], None)
    OracleITSMCollector(make_config(tmp_path)).collect()
    assert opened[-1].outputtypehandler is not None


def test_prepare_query_fills_placeholders(tmp_path):
    sql, metadata = OracleITSMCollector(make_config(tmp_path)).prepare_query()
    assert sql == "SELECT * FROM ITSM.TB_ASSET"
    assert metadata["asset_source"] == "ITSM.TB_ASSET"
    assert metadata["cpu_field"] == "CM_CPU_CORE_CNT"
