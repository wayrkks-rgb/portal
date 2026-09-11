"""통합기·ESXi 업무명이 저장되고 보고서에 쓰이는지 확인한다.

vCenter 가 붙인 이름(vc_0001, esxi-07)으로는 보고서에서 무엇인지 알 수 없다.
업무명은 수집 결과와 따로 두므로 다시 수집해도 남아야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services import DisplayNameError, DisplayNameService


@pytest.fixture()
def repo_factory(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/test.db"))
    manager = create_manager(config)
    manager.initialize()

    def open_repo():
        return manager.connect()

    return manager, open_repo


def save(manager, items, user="admin"):
    with manager.connect() as conn:
        result = DisplayNameService(AssetRepository(conn)).save_many(items, user)
        conn.commit()
    return result


def names(manager):
    with manager.connect() as conn:
        return DisplayNameService(AssetRepository(conn)).all()


CLUSTER = {"scope": "CLUSTER", "vcenter_id": "vc_0001", "object_key": "CL-LINUX-01"}


def test_a_name_is_saved_and_read_back(repo_factory):
    manager, _ = repo_factory
    assert save(manager, [{**CLUSTER, "display_name": "Linux 통합기 #1"}]) == {
        "saved_count": 1, "removed_count": 0,
    }
    rows = names(manager)
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Linux 통합기 #1"
    assert rows[0]["updated_by"] == "admin"


def test_saving_the_same_target_twice_updates_it(repo_factory):
    manager, _ = repo_factory
    save(manager, [{**CLUSTER, "display_name": "처음 이름"}])
    save(manager, [{**CLUSTER, "display_name": "고친 이름"}])
    rows = names(manager)
    assert len(rows) == 1, "같은 대상에 두 줄이 생기면 어느 쪽이 쓰일지 알 수 없다"
    assert rows[0]["display_name"] == "고친 이름"


def test_an_empty_name_releases_the_assignment(repo_factory):
    manager, _ = repo_factory
    save(manager, [{**CLUSTER, "display_name": "Linux 통합기 #1"}])
    assert save(manager, [{**CLUSTER, "display_name": "  "}]) == {"saved_count": 0, "removed_count": 1}
    assert names(manager) == []


def test_the_resolver_falls_back_to_the_original_name(repo_factory):
    manager, _ = repo_factory
    save(manager, [{**CLUSTER, "display_name": "Linux 통합기 #1"}])
    with manager.connect() as conn:
        resolver = DisplayNameService(AssetRepository(conn)).resolver()

    assert resolver.name("CLUSTER", "CL-LINUX-01", "vc_0001") == "Linux 통합기 #1"
    # 이름을 안 붙인 대상은 원래 이름이 나와야 한다. 빈칸이면 사라진 것처럼 보인다.
    assert resolver.name("CLUSTER", "CL-LINUX-99", "vc_0001") == "CL-LINUX-99"
    assert resolver.named("CLUSTER", "CL-LINUX-01", "vc_0001") is True
    assert resolver.named("CLUSTER", "CL-LINUX-99", "vc_0001") is False


def test_a_vcenter_specific_name_wins_over_a_global_one(repo_factory):
    """같은 ESXi 이름이 다른 vCenter 에 있을 수 있다."""
    manager, _ = repo_factory
    save(manager, [
        {"scope": "ESXI", "vcenter_id": "", "object_key": "esxi-07", "display_name": "공통"},
        {"scope": "ESXI", "vcenter_id": "vc_0002", "object_key": "esxi-07", "display_name": "DR 전용"},
    ])
    with manager.connect() as conn:
        resolver = DisplayNameService(AssetRepository(conn)).resolver()
    assert resolver.name("ESXI", "esxi-07", "vc_0002") == "DR 전용"
    assert resolver.name("ESXI", "esxi-07", "vc_0009") == "공통"


@pytest.mark.parametrize("scope", ["CLUSTER", "ESXI", "DATASTORE", "VCENTER", "cluster"])
def test_every_supported_scope_is_accepted(repo_factory, scope):
    manager, _ = repo_factory
    save(manager, [{"scope": scope, "vcenter_id": "vc", "object_key": "k", "display_name": "이름"}])
    assert names(manager)[0]["scope"] == scope.upper()


def test_an_unknown_scope_is_refused(repo_factory):
    manager, _ = repo_factory
    with pytest.raises(DisplayNameError, match="대상 구분"):
        save(manager, [{"scope": "RACK", "object_key": "k", "display_name": "이름"}])


def test_a_missing_target_is_refused_with_its_row_number(repo_factory):
    manager, _ = repo_factory
    with pytest.raises(DisplayNameError, match="2번째 줄"):
        save(manager, [
            {**CLUSTER, "display_name": "정상"},
            {"scope": "ESXI", "object_key": "", "display_name": "대상이 없음"},
        ])


def test_nothing_is_written_when_one_row_is_wrong(repo_factory):
    """절반만 반영되면 어느 줄이 저장됐는지 화면에서 알 수 없다."""
    manager, _ = repo_factory
    with pytest.raises(DisplayNameError):
        save(manager, [
            {**CLUSTER, "display_name": "정상"},
            {"scope": "RACK", "object_key": "k", "display_name": "잘못"},
        ])
    assert names(manager) == []


def test_a_too_long_name_is_refused(repo_factory):
    manager, _ = repo_factory
    with pytest.raises(DisplayNameError, match="너무 깁니다"):
        save(manager, [{**CLUSTER, "display_name": "가" * 300}])


# ── 화면이 부르는 API ──────────────────────────────────────────────────────

@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n", encoding="utf-8"
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


def test_the_api_round_trips_a_name(portal):
    client = admin_client(portal)
    body = client.get("/api/asset-sync/admin/display-names").get_json()
    assert "CLUSTER" in body["scopes"]
    assert "objects" in body

    saved = client.put("/api/asset-sync/admin/display-names", json={
        "names": [{**CLUSTER, "display_name": "Linux 통합기 #1"}]
    }).get_json()
    assert saved["status"] == "SUCCESS" and saved["saved_count"] == 1

    after = client.get("/api/asset-sync/admin/display-names").get_json()
    assert after["names"][0]["display_name"] == "Linux 통합기 #1"


def test_the_api_rejects_a_bad_row_with_400(portal):
    response = admin_client(portal).put("/api/asset-sync/admin/display-names", json={
        "names": [{"scope": "RACK", "object_key": "k", "display_name": "이름"}]
    })
    assert response.status_code == 400
    assert "대상 구분" in response.get_json()["error"]


def test_a_normal_user_cannot_change_names(portal):
    client = portal.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 2, "username": "user", "role": "user", "name": "user"}
    assert client.put("/api/asset-sync/admin/display-names", json={"names": []}).status_code != 200


# ── 보고서에 실제로 반영되는지 ─────────────────────────────────────────────

def test_the_report_shows_the_business_name(tmp_path: Path) -> None:
    """이게 목적이다. 보고서에 vc_0001 대신 업무명이 나와야 한다."""
    from asset_sync.db.manager import SQLiteManager
    from asset_sync.services.collection_service import CollectionService
    from asset_sync.services.resource_usage_service import VMResourceUsageExportService

    config = AppConfig(
        root_dir=tmp_path,
        sqlite_path=Path("data/report.db"),
        itsm={"collection_mode": "DEMO", "memory_unit": "GB",
              "cpu_compare_field": "CM_CPU_CORE_CNT", "memory_field": "CM_MEMORY",
              "os_eos_field": "OS_EOS_DATE", "tracked_fields": [], "ignore_fields": [],
              "owner_field": "CM_WOR_MNG_EMP_ID"},
        rvtools={"collection_mode": "DEMO", "hostname_suffixes": [".example.invalid"],
                 "power_on_value": "poweredon", "exclude_templates": True,
                 "exclude_srm_placeholders": True, "resource_usage": {"enabled": True}},
        matching={"use_identity_map_first": True, "auto_remember_exact_match": True,
                  "memory_tolerance_mb": 1},
        quality={"minimum_itsm_records": 1, "minimum_rvtools_records": 1, "eos_near_days": 180},
    )
    manager = SQLiteManager(config.database_path)
    manager.initialize()
    batch = CollectionService(config, manager).run_daily(demo=True)
    period = batch["resource_usage"]["period_end"]

    with manager.connect() as conn:
        before = VMResourceUsageExportService(config, AssetRepository(conn)).summary(period, period)
    host = before["hosts"][0]
    raw_esxi = host["esxi_host"]
    # 이름을 붙이기 전에는 원래 이름이 그대로 나온다.
    assert host["esxi_display_name"] == raw_esxi

    save(manager, [{"scope": "ESXI", "vcenter_id": host["vcenter_id"],
                    "object_key": raw_esxi, "display_name": "Linux 통합기 #1"}])

    with manager.connect() as conn:
        after = VMResourceUsageExportService(config, AssetRepository(conn)).summary(period, period)
    named = next(row for row in after["hosts"] if row["esxi_host"] == raw_esxi)
    assert named["esxi_display_name"] == "Linux 통합기 #1"
    # 원래 이름은 지우지 않는다. vCenter 화면에서 찾을 때 필요하다.
    assert named["esxi_host"] == raw_esxi
