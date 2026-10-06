"""대수가 안 맞을 때 어디서 갈라지는지 밝혀야 한다.

"대수가 안 맞는다" 에는 두 가지가 섞여 있다.

1. **같아야 하는데 다른 것** — 서버 현황 표의 계와 EOSL 전체 수량은 같은 대상을
   세므로 반드시 같다. 다르면 버그다.
2. **달라야 정상인 것** — ITSM 자산과 vCenter VM 은 애초에 다른 것을 센다.
   통합기를 새로 붙이면 vCenter 쪽이 먼저 늘고, 통합서버 자원사용현황은 07시
   배치가 한 번 돌아야 그 통합기를 본다.

표만 보면 둘을 가릴 수 없다. 감사가 그걸 가려 준다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.count_audit_service import CountAuditService, MISMATCH, OK, STALE

PHYSICAL = "CMSVRCATCD010"
LOGICAL = "CMSVRCATCD020"
BASE_DAY = date(2026, 9, 30)


def asset(cm_id: str, *, physical: bool = False, place: str = "CMPLACE010",
          os_family: str = "Linux Redhat", eosl: str = "2030-12-31") -> dict:
    return {
        "cm_id": cm_id,
        "normalized_hostname": f"host-{cm_id}",
        "primary_ip": f"10.0.0.{int(cm_id[-3:]) % 250 + 1}",
        "os_family": os_family,
        "os_version": "8.6",
        "server_category_code": PHYSICAL if physical else LOGICAL,
        "status_code": "CMSTA010",
        "raw": {
            "CM_ID": cm_id, "CM_NAME": f"업무-{cm_id}", "CM_HOSTNAME": f"host-{cm_id}",
            "CM_IP": f"10.0.0.{int(cm_id[-3:]) % 250 + 1}", "CM_OS": "CMCIOSCD010",
            "CM_OS_VERSION": "8.6", "CM_EOL_DT": eosl, "CM_PLACE": place,
        },
    }


def vm(scope: str, index: int) -> dict:
    key = f"{scope}-vm{index:03d}"
    return {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}",
        "vcenter": scope, "vm_name": key, "dns_name": key, "normalized_hostname": key,
        "ip_addresses": ["10.0.0.9"], "primary_ip": "10.0.0.9", "cpus": 4, "memory_mb": 8192,
        "os_family": "Linux Redhat", "os_version": "8.6", "power_state": "poweredon",
        "datacenter": "DC1", "cluster": "CL1", "cluster_name": "CL1",
        "esxi_host": f"{scope}-esxi-01", "template_flag": False, "srm_placeholder": False,
        "raw": {"VM": key}, "record_hash": "h",
    }


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/audit.db"))
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def seed_itsm(manager, day: date, records: list[dict]) -> int:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", day.isoformat())
        repo.finish_collection_run(run, "SUCCESS", len(records), day.isoformat())
        snapshot_id = repo.create_snapshot(
            "ITSM", day.isoformat(), f"{day.isoformat()}T07:00:00", run, "SUCCESS", len(records), "h"
        )
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [(snapshot_id, r["cm_id"], r["normalized_hostname"], r["primary_ip"], 4, 8192,
              r["os_family"], r["os_version"], r["status_code"], r["server_category_code"],
              "CMOWNCATCD0010", r["raw"].get("CM_EOL_DT"), "h",
              json.dumps(r["raw"], ensure_ascii=False)) for r in records],
        )
        conn.commit()
    return snapshot_id


def seed_vcenter(manager, day: date, records: list[dict]) -> int:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("RVTOOLS", day.isoformat())
        repo.finish_collection_run(run, "SUCCESS", len(records), day.isoformat())
        snapshot_id = repo.create_snapshot(
            "RVTOOLS", day.isoformat(), f"{day.isoformat()}T07:10:00", run, "SUCCESS", len(records), "h"
        )
        repo.insert_rv_records(snapshot_id, records)
        conn.commit()
    return snapshot_id


def audit(config, manager, base_day: date = BASE_DAY) -> dict:
    with manager.connect() as conn:
        return CountAuditService(config, AssetRepository(conn)).audit(base_day)


def test_the_numbers_that_must_agree_do_agree(portal) -> None:
    """서버 현황 표의 계 = 자산 대수 = EOSL 전체 수량."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [
        asset("CM001"), asset("CM002"), asset("CM003", physical=True),
        asset("CM004", place="CMPLACE020"), asset("CM005", physical=True, place="CMPLACE020"),
    ])
    result = audit(config, manager)

    counts = result["counts"]["itsm"]
    assert counts["selected"] == 5
    assert counts["table_all"] == 5
    assert counts["eosl_all"] == 5
    assert counts["physical"] == 2
    assert counts["table_physical"] == 2
    assert counts["eosl_physical"] == 2

    named = {check["name"]: check for check in result["checks"]}
    for name in ("서버 현황 표 = 자산 대수", "EOSL 전체 = 자산 대수",
                 "서버 현황 물리 표 = 물리 대수", "EOSL 서버 = 물리 대수",
                 "자산 + 제외 = ITSM 전체"):
        assert named[name]["verdict"] == OK, named[name]["message"]


def test_the_audit_says_which_pair_broke(portal, monkeypatch) -> None:
    """같아야 하는 쌍이 깨지면 어느 쌍이 몇 건 차이인지 말해야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001"), asset("CM002")])

    # EOSL 표만 한 건을 빼먹는 상황을 흉내낸다.
    from asset_sync.services import count_audit_service as module

    original = module.ServerStatusService.eosl

    def short_eosl(self, snapshot_id, today=None, previous_snapshot_id=None):
        result = original(self, snapshot_id, today, previous_snapshot_id)
        result["all"]["total"] -= 1
        return result

    monkeypatch.setattr(module.ServerStatusService, "eosl", short_eosl)
    result = audit(config, manager)

    assert result["verdict"] == MISMATCH
    broken = next(c for c in result["checks"] if c["name"] == "EOSL 전체 = 자산 대수")
    assert broken["verdict"] == MISMATCH
    assert "1 vs 2" in broken["message"]
    assert "-1" in broken["message"], "몇 건 차이인지 적어야 한다"


def test_a_new_vcenter_before_the_batch_is_called_out_not_treated_as_a_bug(portal) -> None:
    """통합기를 새로 붙이면 자원사용현황에 아직 없다. 그게 버그가 아니라고 말해야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    # 어제: 통합기 1대
    old_snapshot = seed_vcenter(manager, BASE_DAY - timedelta(days=1),
                                [vm("vc01", i) for i in range(3)])
    # 자원사용률은 그 스냅샷을 보고 돌았다.
    with manager.connect() as conn:
        conn.execute(
            "INSERT INTO resource_usage_run(vcenter_snapshot_id, period_start, period_end,"
            " started_at, status, host_count, vm_count)"
            " VALUES(?, ?, ?, ?, 'SUCCESS', 1, 3)",
            (old_snapshot, (BASE_DAY - timedelta(days=1)).isoformat(),
             (BASE_DAY - timedelta(days=1)).isoformat(), "2026-09-29T07:00:00"),
        )
        conn.commit()
    # 오늘: 통합기를 하나 더 붙여 다시 수집했다. 배치는 아직 안 돌았다.
    seed_vcenter(manager, BASE_DAY,
                 [vm("vc01", i) for i in range(3)] + [vm("vc02", i) for i in range(5)])

    result = audit(config, manager)
    named = {check["name"]: check for check in result["checks"]}

    stale = named["자원사용률 기준 스냅샷"]
    assert stale["verdict"] == STALE
    assert "07시 배치" in stale["message"]
    # 같아야 하는 쌍은 여전히 맞는다. 그러니 MISMATCH 가 아니다.
    assert result["verdict"] == STALE
    assert all(c["verdict"] != MISMATCH for c in result["checks"])


def test_different_snapshot_days_are_explained_before_anyone_hunts_a_bug(portal) -> None:
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_vcenter(manager, BASE_DAY - timedelta(days=2), [vm("vc01", 1)])

    result = audit(config, manager)
    named = {check["name"]: check for check in result["checks"]}
    assert named["ITSM · vCenter 기준일"]["verdict"] == STALE
    # ITSM 자산과 vCenter VM 을 견주지 말라고 분명히 적어야 한다.
    assert "같은 것을 세지 않습니다" in named["ITSM 자산 · vCenter VM"]["message"]


def test_an_itsm_only_site_still_gets_an_answer(portal) -> None:
    """vCenter 가 없어도 감사는 돌아야 한다. 없는 것을 검사하지 않을 뿐이다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001"), asset("CM002", physical=True)])
    result = audit(config, manager)
    assert result["verdict"] == OK
    assert result["counts"]["vcenter"] is None
    assert result["counts"]["resource_usage"] is None


def test_without_any_snapshot_it_says_so_instead_of_failing(portal) -> None:
    config, manager = portal
    result = audit(config, manager)
    assert result["verdict"] == STALE
    assert any("스냅샷이 없습니다" in c["message"] for c in result["checks"])


def test_reconciliation_run_before_the_new_vcenter_is_called_out(portal) -> None:
    """통합기를 붙인 뒤 수집만 하고 정합성을 다시 돌리지 않으면 옛 결과가 남는다."""
    config, manager = portal
    itsm = seed_itsm(manager, BASE_DAY, [asset("CM001")])
    old_rv = seed_vcenter(manager, BASE_DAY, [vm("vc01", 1)])
    with manager.connect() as conn:
        conn.execute(
            "INSERT INTO reconciliation_result(itsm_snapshot_id, rv_snapshot_id, cm_id,"
            " match_status, score, drift_json, created_at)"
            " VALUES(?, ?, 'CM001', 'MATCHED', 100, '[]', '2026-09-30T08:00:00')",
            (itsm, old_rv),
        )
        conn.commit()
    # 통합기를 하나 더 붙여 다시 수집했다. 정합성은 그대로다.
    seed_vcenter(manager, BASE_DAY, [vm("vc01", 1), vm("vc02", 1)])

    named = {c["name"]: c for c in audit(config, manager)["checks"]}
    check = named["정합성 기준 스냅샷"]
    assert check["verdict"] == STALE
    assert "정합성을 다시 돌리지 않으면" in check["message"]


def test_a_fresh_reconciliation_reads_as_ok(portal) -> None:
    config, manager = portal
    itsm = seed_itsm(manager, BASE_DAY, [asset("CM001")])
    rv = seed_vcenter(manager, BASE_DAY, [vm("vc01", 1)])
    with manager.connect() as conn:
        conn.execute(
            "INSERT INTO reconciliation_result(itsm_snapshot_id, rv_snapshot_id, cm_id,"
            " match_status, score, drift_json, created_at)"
            " VALUES(?, ?, 'CM001', 'MATCHED', 100, '[]', '2026-09-30T08:00:00')",
            (itsm, rv),
        )
        conn.commit()
    named = {c["name"]: c for c in audit(config, manager)["checks"]}
    assert named["정합성 기준 스냅샷"]["verdict"] == OK
