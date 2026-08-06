"""자산 원본을 지정한 뒤 조건을 고치거나 지울 수 있는지 확인한다.

지정만 되고 되돌릴 수 없으면, 조건 하나를 바꾸려 해도 처음부터 다시 해야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asset_sync.settings_store import LocalSettingsStore, SettingsValidationError


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalSettingsStore:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "oracle:\n  query_file: config/oracle_query.local.sql\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    return LocalSettingsStore(tmp_path)


SPEC = {
    "owner": "ITSM",
    "table_name": "TB_ASSET",
    "asset_source": "ITSM.TB_ASSET",
    "filters": [{"column": "CM_CAT_CD", "operator": "IN", "values": ["HW0101"]}],
    "mapping": {"CM_HOSTNAME": "HOST_NM"},
}


def test_applying_a_source_writes_the_sql_and_remembers_the_inputs(store):
    saved = store.save_asset_source(
        "ITSM.TB_ASSET", query_sql="SELECT 1 FROM DUAL", query_spec=SPEC
    )
    assert saved["oracle"]["asset_source"] == "ITSM.TB_ASSET"
    assert saved["oracle"]["query_file_exists"] is True
    # 이 값이 있어야 화면이 [조건 수정] 에서 원래 입력을 되살릴 수 있다.
    assert saved["oracle"]["asset_query"]["filters"] == SPEC["filters"]
    assert saved["oracle"]["asset_query"]["mapping"] == {"CM_HOSTNAME": "HOST_NM"}


def test_clearing_removes_the_source_and_the_generated_sql(store):
    store.save_asset_source("ITSM.TB_ASSET", query_sql="SELECT 1 FROM DUAL", query_spec=SPEC)
    query_file = store.root / "config" / "oracle_query.local.sql"
    assert query_file.exists()

    cleared = store.clear_asset_source()

    assert cleared["oracle"]["asset_source"] == ""
    assert cleared["oracle"]["asset_query"] == {}
    # SQL 을 남겨두면 다음에 다른 테이블을 골라도 예전 조회가 그대로 돈다.
    assert not query_file.exists()


def test_clearing_keeps_the_connection_settings(store):
    store.save_oracle({"oracle": {"host": "db.example", "port": "1521", "service_name": "orcl",
                                  "user": "reader", "password": "pw", "mode": "thin"}})
    store.save_asset_source("ITSM.TB_ASSET", query_sql="SELECT 1 FROM DUAL", query_spec=SPEC)

    cleared = store.clear_asset_source()

    assert cleared["oracle"]["host"] == "db.example"
    assert cleared["oracle"]["user"] == "reader"
    assert cleared["oracle"]["password_configured"] is True


def test_clearing_twice_is_harmless(store):
    store.save_asset_source("ITSM.TB_ASSET", query_sql="SELECT 1 FROM DUAL", query_spec=SPEC)
    store.clear_asset_source()
    assert store.clear_asset_source()["oracle"]["asset_source"] == ""


def test_a_source_outside_the_project_is_refused(store):
    with pytest.raises(SettingsValidationError):
        store.save_asset_source("ITSM.TB_ASSET", query_sql="SELECT 1", query_file="/etc/passwd")


# ── 화면이 실제로 부르는 API ────────────────────────────────────────────────

@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: ORACLE\noracle:\n  enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    yield create_app(), LocalSettingsStore(tmp_path)
    reset_database_manager()


def admin_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    return client


def test_delete_clears_an_applied_source(portal):
    app, store = portal
    store.save_asset_source("ITSM.TB_ASSET", query_sql="SELECT 1 FROM DUAL", query_spec=SPEC)

    response = admin_client(app).delete("/api/asset-sync/admin/oracle/asset-source")

    assert response.status_code == 200
    body = response.get_json()
    assert body["cleared"] == "ITSM.TB_ASSET"
    assert body["settings"]["oracle"]["asset_source"] == ""


def test_delete_says_so_when_there_is_nothing_to_clear(portal):
    app, _ = portal
    response = admin_client(app).delete("/api/asset-sync/admin/oracle/asset-source")
    assert response.status_code == 400
    assert "없습니다" in response.get_json()["error"]


def test_delete_is_closed_to_a_normal_user(portal):
    app, store = portal
    store.save_asset_source("ITSM.TB_ASSET", query_sql="SELECT 1 FROM DUAL", query_spec=SPEC)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 2, "username": "user", "role": "user", "name": "user"}

    assert client.delete("/api/asset-sync/admin/oracle/asset-source").status_code != 200
    assert store.public_settings()["oracle"]["asset_source"] == "ITSM.TB_ASSET"
