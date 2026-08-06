"""[ITSM 수집] 버튼이 실제로 화면에 무엇을 돌려주는지 확인한다.

브라우저가 받는 JSON 을 그대로 검사한다. 드라이버가 결과만 알려주는 오류
(DPY-1001)를 내도, 화면에는 원인과 단계가 남아야 한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asset_sync.collectors import oracle_itsm_collector as collector_module


@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n"
        "  collection_mode: ORACLE\n"
        "oracle:\n"
        "  enabled: true\n"
        "  mode: thin\n"
        "  host: db.example\n"
        "  port: 1521\n"
        "  service_name: orcl\n"
        "  user: reader\n"
        "  password: pw\n"
        "  asset_source: ITSM.TB_ASSET\n"
        "  query_file: config/oracle_query.local.sql\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "oracle_query.local.sql").write_text(
        "SELECT * FROM ${TABLE_NAME}", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    yield create_app()
    reset_database_manager()


def admin_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    return client


def install_failing_driver(monkeypatch, error: Exception) -> None:
    class FakeDriver:
        @staticmethod
        def connect(**kwargs: Any) -> Any:
            raise error

    monkeypatch.setattr(collector_module, "prepare_driver", lambda cfg: FakeDriver)


def test_the_button_reports_stage_and_build(portal, monkeypatch):
    install_failing_driver(monkeypatch, RuntimeError("DPY-1001: not connected to database"))
    response = admin_client(portal).post("/api/asset-sync/collect/itsm", json={})
    assert response.status_code == 500
    error = response.get_json()["error"]
    assert "DPY-1001" in error
    assert "단계=CONNECT" in error, f"화면에 단계가 안 나온다: {error}"
    assert collector_module.COLLECTOR_BUILD in error


def test_the_button_keeps_the_masked_cause(portal, monkeypatch):
    """정리 단계에서 덮인 원인이 화면까지 살아 와야 한다."""
    try:
        try:
            raise RuntimeError("DPY-4011: the database or network closed the connection")
        except RuntimeError:
            raise RuntimeError("DPY-1001: not connected to database")
    except RuntimeError as masked:
        install_failing_driver(monkeypatch, masked)

    error = admin_client(portal).post("/api/asset-sync/collect/itsm", json={}).get_json()["error"]
    assert "DPY-4011" in error, f"원인이 사라졌다: {error}"


def test_the_failure_is_written_to_the_run_history(portal, monkeypatch):
    """화면 상단 문구가 아니라 [수집 이력] 표를 보고 있어도 같은 내용이어야 한다."""
    install_failing_driver(monkeypatch, RuntimeError("DPY-1001: not connected to database"))
    client = admin_client(portal)
    client.post("/api/asset-sync/collect/itsm", json={})

    rows = client.get("/api/asset-sync/collection-runs").get_json()
    itsm = [row for row in rows if row.get("source") == "ITSM"]
    assert itsm, f"수집 이력에 ITSM 행이 없다: {rows}"
    assert "단계=CONNECT" in (itsm[0].get("error_message") or "")
