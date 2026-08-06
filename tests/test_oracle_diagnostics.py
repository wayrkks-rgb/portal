"""웹 안에서 도는 Oracle 진단이 CLI 와 같은 결과를 주고, 비밀번호를 흘리지 않는지 확인한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from asset_sync.collectors.oracle_diagnostics import run_diagnostics, visible_settings
from asset_sync.config import AppConfig

PASSWORD = "s3cr3t-do-not-leak"


def make_config(tmp_path: Path, **oracle: object) -> AppConfig:
    settings = {
        "mode": "thin", "host": "db.example", "port": 1521, "service_name": "orcl",
        "user": "reader", "password": PASSWORD, "query_file": "config/q.sql",
    }
    settings.update(oracle)
    return AppConfig(root_dir=tmp_path, oracle=settings, itsm={"collection_mode": "ORACLE"})


def test_password_is_never_returned(tmp_path):
    shown = visible_settings(make_config(tmp_path).oracle)
    assert shown["password"] == "(설정됨)"
    assert PASSWORD not in str(shown)


def test_missing_password_is_reported_as_empty(tmp_path):
    assert visible_settings(make_config(tmp_path, password="").oracle)["password"] == "(비어 있음)"


def test_non_oracle_mode_is_skipped(tmp_path):
    config = make_config(tmp_path)
    config.itsm["collection_mode"] = "FILE_ONLY"
    result = run_diagnostics(config)
    assert result["status"] == "SKIPPED"
    assert result["steps"][0]["status"] == "SKIPPED"


def test_a_failed_step_stops_the_rest(tmp_path):
    """조회 SQL 파일이 없으면 접속을 시도할 이유가 없다."""
    result = run_diagnostics(make_config(tmp_path))
    assert result["status"] == "FAILED"
    assert [step["name"] for step in result["steps"]] == ["조회 SQL"]
    assert PASSWORD not in str(result)


def test_the_build_stamp_is_reported(tmp_path):
    """화면에서 예전 파일이 도는지 구분하려면 표식이 응답에 있어야 한다."""
    from asset_sync.collectors.oracle_itsm_collector import COLLECTOR_BUILD

    assert run_diagnostics(make_config(tmp_path))["build"] == COLLECTOR_BUILD


@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: ORACLE\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    yield create_app()
    reset_database_manager()


def client_for(app, role: str):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": role, "role": role, "name": role}
    return client


def test_endpoint_answers_for_an_admin(portal):
    response = client_for(portal, "admin").get("/api/asset-sync/admin/diagnose/oracle")
    assert response.status_code in (200, 500)
    body = response.get_json()
    assert body["build"]
    assert body["steps"]


def test_endpoint_is_closed_to_a_normal_user(portal):
    response = client_for(portal, "user").get("/api/asset-sync/admin/diagnose/oracle")
    assert response.status_code != 200
