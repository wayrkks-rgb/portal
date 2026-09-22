"""모든 화면이 같은 대수를 말하는지 확인한다.

화면마다 각자 세면 숫자가 달라진다. 실제로 그랬다. 통합 대시보드는 운영·대기
상태만 셌고, 월간 점검은 상태를 안 보는 대신 OS·EOSL 이 모두 빈 것을 뺐다. 그래서
두 화면이 같은 날 다른 대수를 말했다.

여기서 지키는 성질은 하나다. **통합 대시보드 · 월간 점검 · 보고서가 같은 수를
낸다.** 어느 한쪽 계산을 고치면 이 테스트가 깨지므로, 고칠 때 나머지도 같이
고치게 된다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.asset_scope import AssetScope, criteria_from, location, parse_year
from asset_sync.services.integrated_dashboard_service import IntegratedDashboardService
from asset_sync.services.server_status_service import ServerStatusService

CRITERIA = criteria_from(None)


def raw(**overrides) -> dict:
    record = {
        "CM_ID": "CM0001", "CM_NAME": "인사시스템", "CM_HOSTNAME": "hrsrv01",
        "CM_IP": "10.0.0.1", "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6",
        "CM_EOL_DT": "2030-12-31", "CM_PLACE": "IDC-1F",
    }
    record.update(overrides)
    return record


def asset(cm_id, *, status="CMSTA010", physical=False, os_family="Linux Redhat", **raw_overrides):
    return {
        "cm_id": cm_id,
        "normalized_hostname": f"host-{cm_id}",
        "primary_ip": "10.0.0.9",
        "os_family": os_family,
        "status_code": status,
        "server_category_code": "CMSVRCATCD010" if physical else "CMSVRCATCD020",
        "raw": raw(CM_ID=cm_id, **raw_overrides),
    }


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/scope.db"))
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def seed(manager, rows: list[dict]) -> int:
    now = datetime.now()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", now.isoformat())
        repo.finish_collection_run(run, "SUCCESS", len(rows), now.isoformat(), ["ALL"])
        snapshot_id = repo.create_snapshot(
            "ITSM", now.date().isoformat(), now.isoformat(), run, "SUCCESS", len(rows), "h"
        )
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [(snapshot_id, r["cm_id"], r["normalized_hostname"], r["primary_ip"], 4, 8192,
              r["os_family"], "8.6", r["status_code"], r["server_category_code"],
              "CMOWNCATCD0010", r["raw"].get("CM_EOL_DT"), "h",
              json.dumps(r["raw"], ensure_ascii=False)) for r in rows],
        )
        conn.commit()
    return snapshot_id


# ── 위치 판정 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("place,expected", [
    ("63DR", "DR"),              # 앞에 층수가 붙어도 DR 이다
    ("DR센터", "DR"),
    ("IDC-DR-2F", "DR"),         # 둘 다 들어 있으면 DR
    ("IDC-1F", "IDC"),
    ("본사 전산실", "IDC"),
    ("재해복구센터", "DR"),
    ("", "IDC"),                 # 값이 없으면 기본값
    ("3층 서버실", "IDC"),        # 어느 조각도 없으면 기본값
])
def test_location_matches_by_fragment_not_by_equality(place, expected):
    """값이 "63DR" 처럼 앞뒤에 무엇이 붙어 온다. 같은지가 아니라 들어 있는지 본다."""
    assert location({"CM_PLACE": place}, CRITERIA) == expected


# ── EOSL 연도 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("2030-12-31", 2030), ("20301231", 2030), ("2030.12.31", 2030),
    ("2030-12-31 00:00:00", 2030), ("99991231", 9999), ("9999-12-31", 9999),
    ("31/12/2030", 2030),        # 일/월/연 순서여도 연도만 고른다
    ("1231", None),              # 연도로 말이 안 되는 네 자리는 고르지 않는다
    ("", None), (None, None), ("미정", None),
])
def test_only_a_plausible_year_is_taken(value, expected):
    assert parse_year(value) == expected


def test_the_eosl_column_can_have_another_name(portal):
    """ITSM 마다 컬럼 이름이 다르다. 하나로 못 박으면 전부 '미사용' 으로 떨어진다."""
    config, manager = portal
    rows = [asset("CM0001", CM_EOL_DT="", OS_EOS_DATE="2031-06-30")]
    snapshot_id = seed(manager, rows)
    with manager.connect() as conn:
        service = ServerStatusService(config, AssetRepository(conn))
        included, _ = service.select(snapshot_id)
        eosl = service.eosl(snapshot_id)
    assert included[0]["eosl_field"] == "OS_EOS_DATE"
    assert included[0]["eosl_year"] == 2031
    # 어디서 읽었는지 화면에 알려줘야 한다.
    assert eosl["diagnosis"]["by_field"]["OS_EOS_DATE"] == 1


def test_a_missing_eosl_is_reported_not_silently_counted(portal):
    config, manager = portal
    snapshot_id = seed(manager, [asset("CM0001", CM_EOL_DT="", CM_OS="CMCIOSCD010")])
    with manager.connect() as conn:
        eosl = ServerStatusService(config, AssetRepository(conn)).eosl(snapshot_id)
    assert eosl["diagnosis"]["by_field"]["(값 없음)"] == 1
    assert eosl["all"]["counts"]["미사용"] == 1


# ── 화면 사이의 일치 ─────────────────────────────────────────────────────
def test_the_dashboard_and_the_monthly_check_count_the_same_assets(portal):
    config, manager = portal
    rows = [
        asset("CM0001"),                                       # 자산
        asset("CM0002", physical=True, CM_PLACE="63DR"),        # 자산 · DR
        asset("CM0003", status="CMSTA060"),                     # 폐기 -> 제외
        asset("CM0004", CM_OS="", CM_OS_VERSION="", CM_EOL_DT=""),  # 빈 값 -> 제외
    ]
    snapshot_id = seed(manager, rows)

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        dashboard = IntegratedDashboardService(repo, AssetScope.load(config, repo)).summary()
        status = ServerStatusService(config, repo).status(snapshot_id)

    assert dashboard["asset_status"]["total"] == 2
    assert status["all"]["table"]["rows"]["계"]["소계"] == 2
    assert dashboard["asset_status"]["total"] == status["all"]["table"]["rows"]["계"]["소계"]

    # 위치도 같아야 한다. 예전에는 대시보드가 원본을 못 읽어 전부 IDC 였다.
    assert dashboard["asset_status"]["location"] == {"IDC": 1, "DR": 1}
    assert status["all"]["table"]["rows"]["DR"]["소계"] == 1

    # 무엇을 왜 뺐는지 사유별로 나온다.
    basis = dashboard["asset_status"]["counting_basis"]
    assert basis["snapshot_total"] == 4
    assert basis["by_reason"]["STATUS"]["count"] == 1
    assert basis["by_reason"]["EMPTY"]["count"] == 1


def test_a_manual_exclusion_applies_to_every_screen(portal):
    config, manager = portal
    snapshot_id = seed(manager, [asset("CM0001"), asset("CM0002")])

    with manager.connect() as conn:
        from asset_sync.services.asset_scope import save_rules
        save_rules(AssetRepository(conn), "ITSM", [{"asset_key": "CM0002"}],
                   mode="EXCLUDE", reason="실물 없음", updated_by="admin")

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        dashboard = IntegratedDashboardService(repo, AssetScope.load(config, repo)).summary()
        status = ServerStatusService(config, repo).status(snapshot_id)

    assert dashboard["asset_status"]["total"] == 1
    assert status["all"]["table"]["rows"]["계"]["소계"] == 1
    assert status["excluded"]["count"] == 1
    excluded = status["excluded"]["items"][0]
    assert excluded["cm_id"] == "CM0002"
    assert excluded["manual"] is True
    assert excluded["manual_note"] == "실물 없음"


def test_a_manual_inclusion_beats_the_automatic_rule(portal):
    """자동으로 빠진 것을 '이건 자산이 맞다' 고 되돌릴 수 있어야 한다."""
    config, manager = portal
    snapshot_id = seed(manager, [
        asset("CM0001"),
        asset("CM0002", CM_OS="", CM_OS_VERSION="", CM_EOL_DT=""),
    ])
    with manager.connect() as conn:
        status = ServerStatusService(config, AssetRepository(conn)).status(snapshot_id)
    assert status["all"]["table"]["rows"]["계"]["소계"] == 1

    with manager.connect() as conn:
        from asset_sync.services.asset_scope import save_rules
        save_rules(AssetRepository(conn), "ITSM", [{"asset_key": "CM0002"}],
                   mode="INCLUDE", reason="ITSM 값만 빠진 실제 서버")

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        status = ServerStatusService(config, repo).status(snapshot_id)
        dashboard = IntegratedDashboardService(repo, AssetScope.load(config, repo)).summary()
    assert status["all"]["table"]["rows"]["계"]["소계"] == 2
    assert dashboard["asset_status"]["total"] == 2


def test_include_all_brings_everything_back_with_the_reason_kept(portal):
    """원본 전체를 뽑을 때도 어떤 것이 평소 빠지는지는 남아야 한다."""
    config, manager = portal
    snapshot_id = seed(manager, [asset("CM0001"), asset("CM0002", status="CMSTA060")])
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        normal = ServerStatusService(config, repo).status(snapshot_id)
        everything = ServerStatusService(config, repo, include_all=True).status(snapshot_id)
    assert normal["all"]["table"]["rows"]["계"]["소계"] == 1
    assert everything["all"]["table"]["rows"]["계"]["소계"] == 2
    assert everything["scope"]["by_reason"]["STATUS"]["count"] == 1


def test_the_eosl_table_counts_the_same_assets_as_the_server_table(portal):
    """두 장표가 같은 대상을 써야 합계가 맞는다."""
    config, manager = portal
    snapshot_id = seed(manager, [
        asset("CM0001"), asset("CM0002", physical=True),
        asset("CM0003", status="CMSTA020"),
    ])
    with manager.connect() as conn:
        service = ServerStatusService(config, AssetRepository(conn))
        status = service.status(snapshot_id)
        eosl = service.eosl(snapshot_id)
    assert eosl["all"]["total"] == status["all"]["table"]["rows"]["계"]["소계"]
    assert eosl["physical"]["total"] == status["physical"]["table"]["rows"]["계"]["소계"]
