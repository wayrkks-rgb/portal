"""오늘 vCenter 에만 생긴 서버를 며칠 뒤 ITSM 에 등록하면, 그날 운영 대수에 잡히는가.

운영 대수는 ITSM 기준이다. 그래서 VM 이 생긴 날에는 대수가 그대로이고, ITSM 에
**등록(또는 운영 전환)되는 날** +1 이 돼야 한다. 그 사이 일간 화면에는 'ITSM 미반영'
으로 보이다가, 등록한 뒤에는 '반영됨' 으로 바뀌어야 한다. 월간 대수와 대수 감사도
같은 숫자를 내야 한다 -- 일 · 주 · 월이 한 몸으로 움직이는지 실제 비교 엔진으로
날짜를 넘겨 가며 확인한다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.asset_scope import AssetScope
from asset_sync.services.change_digest_service import (
    CHANGED,
    CREATED,
    NOT_REFLECTED,
    REFLECTED,
    ChangeDigestService,
)
from asset_sync.services.count_audit_service import CountAuditService
from asset_sync.services.diff_service import DiffService
from asset_sync.services.server_status_service import ServerStatusService

PREV_END, DAY_VM, DAY_ITSM, DAY_RUN, MONTH_END = (
    "2026-09-30", "2026-10-05", "2026-10-08", "2026-10-10", "2026-10-31",
)


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/late.db"))
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def asset(cm_id: str, host: str, ip: str, status: str = "CMSTA010") -> dict:
    raw = {"CM_ID": cm_id, "CM_NAME": f"업무-{host}", "CM_HOSTNAME": host, "CM_IP": ip,
           "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6", "CM_EOL_DT": "2030-12-31",
           "CM_PLACE": "CMPLACE010", "CM_STA_CD": status, "CM_SVR_CAT_CD": "CMSVRCATCD020",
           "CM_CPU_CORE_CNT": "4", "CM_MEMORY": "8"}
    return {"cm_id": cm_id, "normalized_hostname": host, "primary_ip": ip, "ip": [ip],
            "cpu": 4, "memory": 8192, "status": status, "raw": raw}


def vm(key: str, host: str, ip: str) -> dict:
    return {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}",
        "vcenter": "VC1", "vm_name": host.upper(), "dns_name": host, "normalized_hostname": host,
        "ip_addresses": [ip], "primary_ip": ip, "cpus": 4, "memory_mb": 8192,
        "os_family": "Linux Redhat", "os_version": "8.6", "power_state": "poweredOn",
        "datacenter": "DC1", "cluster": "CL1", "cluster_name": "CL1", "esxi_host": "esxi-01",
        "template_flag": False, "srm_placeholder": False, "raw": {}, "record_hash": key,
    }


def put_itsm(config, manager, day: str, assets: list[dict]) -> None:
    """그날의 ITSM 스냅샷을 만들고, 직전 스냅샷과 비교해 변경 이벤트를 남긴다."""
    stamp = f"{day}T07:00:00"
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        previous = repo.latest_snapshot("ITSM")
        run = repo.start_collection_run("ITSM", stamp)
        repo.finish_collection_run(run, "SUCCESS", len(assets), stamp)
        snap = repo.create_snapshot("ITSM", day, stamp, run, "SUCCESS", len(assets), "h")
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(snap, a["cm_id"], a["normalized_hostname"], a["primary_ip"], json.dumps(a["ip"]),
              a["cpu"], a["memory"], "Linux Redhat", "8.6", a["status"], "CMSVRCATCD020",
              "CMOWNCATCD0010", "2030-12-31",
              # 실제 수집처럼 원본 전체로 해시한다. 비교 엔진은 해시가 같으면 건너뛴다.
              json.dumps(a["raw"], sort_keys=True, ensure_ascii=False),
              json.dumps(a["raw"], ensure_ascii=False)) for a in assets],
        )
        if previous:
            pair = DiffService(config, repo).compare_pair("ITSM", snap, int(previous["id"]))
            repo.replace_change_events(snap, pair["events"])


def put_vcenter(config, manager, day: str, vms: list[dict]) -> None:
    stamp = f"{day}T07:00:00"
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        previous = repo.latest_snapshot("RVTOOLS")
        run = repo.start_collection_run("RVTOOLS", stamp)
        repo.finish_collection_run(run, "SUCCESS", len(vms), stamp, ["VC1"], [])
        snap = repo.create_snapshot("RVTOOLS", day, stamp, run, "SUCCESS", len(vms), "h")
        repo.insert_rv_records(snap, vms)
        if previous:
            pair = DiffService(config, repo).compare_pair("RVTOOLS", snap, int(previous["id"]))
            repo.replace_change_events(snap, pair["events"])


def day_digest(config, manager, day: str, codes=None) -> dict:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        scope = None
        if codes:
            scope = AssetScope.load(SimpleNamespace(server_status={"active_status": codes}), repo)
        return ChangeDigestService(config, repo, scope).digest(f"{day}T00:00:00", f"{day}T23:59:59")


BASE_ASSETS = [asset(f"CM00{i}", f"srv{i}", f"10.0.0.{i}") for i in range(1, 4)]
BASE_VMS = [vm(f"u{i}", f"srv{i}", f"10.0.0.{i}") for i in range(1, 4)]
NEW_VM = vm("u-new", "newsrv", "10.0.0.99")


def setup_history(config, manager, *, registered_status: str = "CMSTA010") -> None:
    put_itsm(config, manager, PREV_END, BASE_ASSETS)
    put_vcenter(config, manager, PREV_END, BASE_VMS)
    # 10/05: vCenter 에만 VM 이 생겼다. ITSM 은 그대로.
    put_itsm(config, manager, DAY_VM, BASE_ASSETS)
    put_vcenter(config, manager, DAY_VM, BASE_VMS + [NEW_VM])
    # 10/08: 사흘 늦게 ITSM 에 등록됐다.
    put_itsm(config, manager, DAY_ITSM,
             BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99", registered_status)])
    put_vcenter(config, manager, DAY_ITSM, BASE_VMS + [NEW_VM])


def test_the_vm_day_does_not_count_and_the_registration_day_does(portal) -> None:
    config, manager = portal
    setup_history(config, manager)

    vm_day = day_digest(config, manager, DAY_VM)["rows"]
    assert len(vm_day) == 1
    assert vm_day[0]["kind"] == CREATED and vm_day[0]["count_effect"] == 0
    assert "등록" in vm_day[0]["count_note"]

    itsm_day = day_digest(config, manager, DAY_ITSM)["rows"]
    assert len(itsm_day) == 1
    assert itsm_day[0]["itsm_cm_id"] == "CM099"
    assert (itsm_day[0]["kind"], itsm_day[0]["count_effect"]) == (CREATED, 1)


def test_the_vm_day_turns_reflected_once_itsm_catches_up(portal) -> None:
    """반영 여부는 '지금 ITSM' 과 견준다. 늦게라도 등록하면 그날 줄도 반영됨이 된다."""
    config, manager = portal
    put_itsm(config, manager, PREV_END, BASE_ASSETS)
    put_vcenter(config, manager, PREV_END, BASE_VMS)
    put_itsm(config, manager, DAY_VM, BASE_ASSETS)
    put_vcenter(config, manager, DAY_VM, BASE_VMS + [NEW_VM])
    assert day_digest(config, manager, DAY_VM)["rows"][0]["reflection"] == NOT_REFLECTED

    put_itsm(config, manager, DAY_ITSM, BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99")])
    put_vcenter(config, manager, DAY_ITSM, BASE_VMS + [NEW_VM])
    assert day_digest(config, manager, DAY_VM)["rows"][0]["reflection"] == REFLECTED


def test_monthly_count_and_audit_agree_with_the_days(portal) -> None:
    """월간 서버 대수 +1, 그 달 일일 증감 합 +1, 대수 감사 OK."""
    config, manager = portal
    setup_history(config, manager)
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        current = repo.snapshot_on_or_before("ITSM", MONTH_END)
        previous = repo.snapshot_on_or_before("ITSM", PREV_END)
        status = ServerStatusService(config, repo).status(int(current["id"]), int(previous["id"]))
        assert status["all"]["delta"]["계"]["소계"] == 1

        audit = CountAuditService(config, repo).audit(date.fromisoformat(MONTH_END))
        check = next(c for c in audit["checks"] if c["name"] == "월간 대수 = 이번 달 일일 증감")
        assert check["verdict"] == "OK", check["message"]
        assert check["daily_net"] == 1


def test_registered_as_standby_counts_when_it_turns_operating(portal) -> None:
    """'운영' 기준에서 대기로 먼저 등록되면 그날은 그대로, 운영으로 바뀌는 날 +1."""
    config, manager = portal
    setup_history(config, manager, registered_status="CMSTA050")
    put_itsm(config, manager, DAY_RUN, BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99")])
    put_vcenter(config, manager, DAY_RUN, BASE_VMS + [NEW_VM])

    registered = day_digest(config, manager, DAY_ITSM)["rows"][0]
    assert (registered["kind"], registered["count_effect"]) == (CHANGED, 0)
    turned_on = day_digest(config, manager, DAY_RUN)["rows"][0]
    assert (turned_on["kind"], turned_on["count_effect"]) == (CREATED, 1)

    # '운영+대기' 기준이면 등록하는 날 바로 잡히고, 운영 전환은 그냥 변경이다.
    wide = ["CMSTA010", "CMSTA050"]
    assert day_digest(config, manager, DAY_ITSM, wide)["rows"][0]["count_effect"] == 1
    assert day_digest(config, manager, DAY_RUN, wide)["rows"][0]["kind"] == CHANGED
