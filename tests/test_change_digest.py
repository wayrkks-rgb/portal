"""증감 현황은 두 가지만 답하면 된다.

1. **어떤 서버가** 실제로 추가·삭제·수정됐나
2. vCenter 에서 바뀐 것이 **ITSM 에도 반영됐나**

지금까지는 한 자산의 변경이 묶음 이벤트와 항목별 이벤트로 흩어져 같은 서버가
네다섯 줄로 나왔고, 0 에서 빈 값으로 바뀐 것처럼 뜻 없는 변경이 진짜 변경과 같은
무게로 끼어 있었다. 반영 여부는 아예 없었다. 그 셋을 여기서 지킨다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.change_digest_service import (
    CHANGED,
    CREATED,
    NO_ASSET,
    NOT_REFLECTED,
    REFLECTED,
    REMOVED,
    ChangeDigestService,
)

DAY = "2026-10-07"
START, END = "2026-10-07T00:00:00", "2026-10-08T00:00:00"


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/digest.db"))
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def asset(cm_id: str, *, hostname: str, ip: str, name: str, status: str = "CMSTA010",
          cpu: int = 4, memory_mb: int = 8192) -> dict:
    return {
        "cm_id": cm_id, "normalized_hostname": hostname, "primary_ip": ip,
        "ip_addresses": [ip], "cpu_cores": cpu, "memory_mb": memory_mb,
        "os_family": "Linux Redhat", "os_version": "8.6", "status_code": status,
        "server_category_code": "CMSVRCATCD020",
        "raw": {"CM_ID": cm_id, "CM_NAME": name, "CM_HOSTNAME": hostname, "CM_IP": ip,
                "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6", "CM_EOL_DT": "2030-12-31",
                "CM_PLACE": "CMPLACE010"},
    }


def vm(key: str, *, hostname: str, ip: str, name: str, cpu: int = 4,
       memory_mb: int = 8192) -> dict:
    return {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}",
        "vcenter": "VC1", "vm_name": name, "dns_name": hostname,
        "normalized_hostname": hostname, "ip_addresses": [ip], "primary_ip": ip,
        "cpus": cpu, "memory_mb": memory_mb, "os_family": "Linux Redhat", "os_version": "8.6",
        "power_state": "poweredOn", "datacenter": "DC1", "cluster": "CL1",
        "cluster_name": "CL1", "esxi_host": "esxi-01", "template_flag": False,
        "srm_placeholder": False, "raw": {}, "record_hash": "h",
    }


def seed(manager, *, assets: list[dict] | None = None, vms: list[dict] | None = None,
         events: list[dict] | None = None) -> None:
    now = datetime.now()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        itsm_snapshot = rv_snapshot = None
        if assets is not None:
            run = repo.start_collection_run("ITSM", now.isoformat())
            repo.finish_collection_run(run, "SUCCESS", len(assets), now.isoformat())
            itsm_snapshot = repo.create_snapshot(
                "ITSM", DAY, now.isoformat(), run, "SUCCESS", len(assets), "h")
            conn.executemany(
                "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,"
                "primary_ip,ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,"
                "server_category_code,environment_code,eos_value,record_hash,raw_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(itsm_snapshot, r["cm_id"], r["normalized_hostname"], r["primary_ip"],
                  json.dumps(r["ip_addresses"]), r["cpu_cores"], r["memory_mb"],
                  r["os_family"], r["os_version"], r["status_code"],
                  r["server_category_code"], "CMOWNCATCD0010", r["raw"]["CM_EOL_DT"], "h",
                  json.dumps(r["raw"], ensure_ascii=False)) for r in assets],
            )
        if vms is not None:
            run = repo.start_collection_run("RVTOOLS", now.isoformat())
            repo.finish_collection_run(run, "SUCCESS", len(vms), now.isoformat(), ["VC1"], [])
            rv_snapshot = repo.create_snapshot(
                "RVTOOLS", DAY, now.isoformat(), run, "SUCCESS", len(vms), "h")
            repo.insert_rv_records(rv_snapshot, vms)
        for event in events or []:
            snapshot = rv_snapshot if event["source"] == "RVTOOLS" else itsm_snapshot
            conn.execute(
                "INSERT INTO change_event(source, snapshot_id, previous_snapshot_id, asset_key,"
                " event_type, field_name, old_value, new_value, detected_at, group_key,"
                " metadata_json) VALUES(?,?,NULL,?,?,?,?,?,?,?,'{}')",
                (event["source"], snapshot, event["asset_key"], event["event_type"],
                 event.get("field_name"), event.get("old_value"), event.get("new_value"),
                 event.get("detected_at", f"{DAY}T14:30:37"), event.get("group_key")),
            )
        conn.commit()


def digest(config, manager, **kwargs) -> dict:
    with manager.connect() as conn:
        return ChangeDigestService(config, AssetRepository(conn)).digest(START, END, **kwargs)


def field_event(key: str, field: str, old, new, **extra) -> dict:
    return {"source": "ITSM", "asset_key": key, "event_type": extra.pop("event_type",
            "ITSM_FIELD_CHANGED"), "field_name": field, "old_value": old, "new_value": new,
            "group_key": f"ITSM|{key}|{DAY}T14:30:37", **extra}


# ── 1. 어떤 서버가 바뀌었나 ────────────────────────────────────────────


def test_one_server_becomes_one_row(portal) -> None:
    """한 자산의 변경 네 건이 네 줄이 아니라 한 줄이어야 한다."""
    config, manager = portal
    seed(manager,
         assets=[asset("CM054830", hostname="klisdrcon2", ip="10.10.10.80",
                       name="OA2 DR SD#3(슈퍼돔 콘솔)")],
         events=[
             {"source": "ITSM", "asset_key": "CM054830", "event_type": "ITSM_ASSET_UPDATED",
              "group_key": f"ITSM|CM054830|{DAY}T14:30:37"},
             field_event("CM054830", "CM_NAME", "OA2 SD#3(슈퍼돔 콘솔)",
                         "OA2 DR SD#3(슈퍼돔 콘솔)"),
             field_event("CM054830", "CM_MTN_YN", None, "1"),
         ])
    result = digest(config, manager)

    assert len(result["rows"]) == 1, [r["summary"] for r in result["rows"]]
    row = result["rows"][0]
    assert row["kind"] == CHANGED
    assert row["asset_key"] == "CM054830"
    # 어느 서버인지 바로 보여야 한다.
    assert row["hostname"] == "klisdrcon2"
    assert row["primary_ip"] == "10.10.10.80"
    assert row["label"] == "OA2 DR SD#3(슈퍼돔 콘솔)"
    # 바뀐 항목이 줄 안에 들어 있다.
    assert len(row["fields"]) == 2
    assert "OA2 DR SD#3" in row["summary"]
    assert result["summary"]["servers"] == 1


def test_the_group_event_is_not_a_row_of_its_own(portal) -> None:
    """ITSM_ASSET_UPDATED 는 항목별 이벤트와 같은 내용이다. 한 줄 더 차지하면 안 된다."""
    config, manager = portal
    seed(manager, assets=[asset("CM001", hostname="a", ip="10.0.0.1", name="업무A")],
         events=[
             {"source": "ITSM", "asset_key": "CM001", "event_type": "ITSM_ASSET_UPDATED"},
             field_event("CM001", "CM_NAME", "old", "new"),
         ])
    rows = digest(config, manager)["rows"]
    assert len(rows) == 1
    assert "ITSM_ASSET_UPDATED" not in str(rows[0]["fields"])


def test_a_zero_to_blank_change_is_folded_away(portal) -> None:
    """0 과 빈 값은 둘 다 '모름' 이다. 같은 무게로 끼면 진짜 변경이 묻힌다."""
    config, manager = portal
    seed(manager, assets=[asset("CM001", hostname="a", ip="10.0.0.1", name="업무A")],
         events=[
             field_event("CM001", "CM_CPU_CORE_CNT", "0", None,
                         event_type="ITSM_CPU_CHANGED"),
             field_event("CM001", "CM_MEMORY", "0", "", event_type="ITSM_MEMORY_CHANGED"),
             field_event("CM001", "CM_NAME", "업무A", "업무B"),
         ])
    row = digest(config, manager)["rows"][0]
    assert [f["name"] for f in row["fields"]] == ["CM_NAME"], row["fields"]
    assert row["trivial_count"] == 2, "숨긴 건수는 알려줘야 한다"
    assert "CM_CPU_CORE_CNT" not in row["summary"]

    # 숨기지 않고 전부 보겠다면 그것도 된다.
    everything = digest(config, manager, include_trivial=True)["rows"][0]
    assert len(everything["fields"]) == 3
    assert everything["trivial_count"] == 0


def test_a_row_with_only_trivial_changes_is_marked_not_dropped(portal) -> None:
    """뜻 없는 변경만 있어도 조용히 지우지는 않는다. 표시해 둔다."""
    config, manager = portal
    seed(manager, assets=[asset("CM001", hostname="a", ip="10.0.0.1", name="업무A")],
         events=[field_event("CM001", "CM_CPU_CORE_CNT", "0", None)])
    rows = digest(config, manager)["rows"]
    assert len(rows) == 1
    assert rows[0]["only_trivial"] is True
    assert rows[0]["trivial_count"] == 1


def test_created_and_removed_do_not_dump_the_whole_record(portal) -> None:
    """생성·삭제 이벤트는 값 자리에 원본 전체가 들어 있다. 그대로 적으면 못 읽는다."""
    config, manager = portal
    blob = json.dumps({"vm_name": "test-1180494", "os_family": "Linux Redhat"},
                      ensure_ascii=False)
    seed(manager, assets=[], vms=[vm("u1", hostname="rhel9-6", ip="10.0.0.9",
                                     name="test-1180494")],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_REMOVED",
                  "old_value": blob}])
    row = digest(config, manager)["rows"][0]
    assert row["kind"] == REMOVED
    assert row["kind_label"] == "삭제"
    assert row["fields"] == [], "원본 전체를 항목 변경으로 적으면 안 된다"
    assert row["summary"] == "삭제"
    assert row["label"] == "test-1180494"


def test_the_strongest_kind_wins_for_a_row(portal) -> None:
    config, manager = portal
    seed(manager, assets=[asset("CM001", hostname="a", ip="10.0.0.1", name="업무A")],
         events=[
             field_event("CM001", "CM_NAME", "x", "y"),
             {"source": "ITSM", "asset_key": "CM001", "event_type": "ITSM_ASSET_CREATED"},
         ])
    assert digest(config, manager)["rows"][0]["kind"] == CREATED


# ── 2. ITSM 에 반영됐나 ────────────────────────────────────────────────


def test_a_new_vm_without_an_itsm_asset_is_flagged(portal) -> None:
    """vCenter 에 VM 이 생겼는데 ITSM 에 없다. 등록해야 한다."""
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="other", ip="10.0.0.1", name="다른업무")],
         vms=[vm("u-new", hostname="newsrv", ip="10.0.0.50", name="new-vm")],
         events=[{"source": "RVTOOLS", "asset_key": "u-new", "event_type": "RV_NEW"}])
    row = digest(config, manager)["rows"][0]
    assert row["reflection"] == NOT_REFLECTED
    assert row["itsm_action"] == "ITSM 에 자산 등록"
    assert "ITSM 에 자산이 없습니다" in row["reflection_note"]


def test_a_new_vm_already_in_itsm_counts_as_reflected(portal) -> None:
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="newsrv", ip="10.0.0.50", name="새업무")],
         vms=[vm("u-new", hostname="newsrv", ip="10.0.0.50", name="new-vm")],
         events=[{"source": "RVTOOLS", "asset_key": "u-new", "event_type": "RV_NEW"}])
    row = digest(config, manager)["rows"][0]
    assert row["reflection"] == REFLECTED
    assert row["itsm_cm_id"] == "CM001"
    assert row["itsm_action"] == ""


def test_a_removed_vm_still_active_in_itsm_is_flagged(portal) -> None:
    """vCenter 에서 사라졌는데 ITSM 은 아직 운영이다. 상태를 바꿔야 한다."""
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="gone", ip="10.0.0.7", name="사라진업무")],
         vms=[vm("u-gone", hostname="gone", ip="10.0.0.7", name="gone-vm")],
         events=[{"source": "RVTOOLS", "asset_key": "u-gone", "event_type": "RV_REMOVED"}])
    row = digest(config, manager)["rows"][0]
    assert row["reflection"] == NOT_REFLECTED
    assert row["itsm_action"] == "ITSM 상태를 미사용·폐기로"


def test_a_removed_vm_already_disposed_in_itsm_is_reflected(portal) -> None:
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="gone", ip="10.0.0.7", name="사라진업무",
                       status="CMSTA060")],
         vms=[vm("u-gone", hostname="gone", ip="10.0.0.7", name="gone-vm")],
         events=[{"source": "RVTOOLS", "asset_key": "u-gone", "event_type": "RV_REMOVED"}])
    assert digest(config, manager)["rows"][0]["reflection"] == REFLECTED


def test_a_cpu_change_not_carried_into_itsm_is_flagged_with_both_values(portal) -> None:
    """ITSM 은 4코어인데 vCenter 는 8코어다. 양쪽 값을 다 적어야 고칠 수 있다."""
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A", cpu=4)],
         vms=[vm("u1", hostname="srv", ip="10.0.0.3", name="srv-vm", cpu=8)],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_CPU_CHANGED",
                  "field_name": "cpus", "old_value": "4", "new_value": "8"}])
    row = digest(config, manager)["rows"][0]
    assert row["reflection"] == NOT_REFLECTED
    assert "vCenter 8" in row["reflection_note"] and "ITSM 4" in row["reflection_note"]
    assert row["itsm_action"] == "ITSM 값을 vCenter 와 맞추기"


def test_a_cpu_change_already_in_itsm_is_reflected_even_if_updated_later(portal) -> None:
    """ITSM 을 며칠 늦게 고쳤어도 지금 값이 맞으면 반영된 것이다.

    이벤트끼리 견주면 늦게 고친 것을 미반영으로 잘못 읽는다. 지금 값을 본다.
    """
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A", cpu=8)],
         vms=[vm("u1", hostname="srv", ip="10.0.0.3", name="srv-vm", cpu=8)],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_CPU_CHANGED",
                  "field_name": "cpus", "old_value": "4", "new_value": "8"}])
    row = digest(config, manager)["rows"][0]
    assert row["reflection"] == REFLECTED
    assert "같습니다" in row["reflection_note"]


def test_a_memory_change_is_judged_within_tolerance(portal) -> None:
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A",
                       memory_mb=16384)],
         vms=[vm("u1", hostname="srv", ip="10.0.0.3", name="srv-vm", memory_mb=16384)],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_MEMORY_CHANGED",
                  "field_name": "memory_mb", "old_value": "8192", "new_value": "16384"}])
    assert digest(config, manager)["rows"][0]["reflection"] == REFLECTED


def test_a_vm_with_no_matching_asset_says_so(portal) -> None:
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="other", ip="10.0.0.1", name="다른업무")],
         vms=[vm("u1", hostname="nomatch", ip="10.9.9.9", name="orphan")],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_CPU_CHANGED",
                  "field_name": "cpus", "old_value": "4", "new_value": "8"}])
    row = digest(config, manager)["rows"][0]
    assert row["reflection"] == NO_ASSET
    assert "정합성 화면" in row["itsm_action"]


def test_itsm_rows_are_not_judged_for_reflection(portal) -> None:
    """ITSM 쪽 변경에 '반영 여부' 를 붙이면 뜻이 없다."""
    config, manager = portal
    seed(manager, assets=[asset("CM001", hostname="a", ip="10.0.0.1", name="업무A")],
         events=[field_event("CM001", "CM_NAME", "x", "y")])
    assert "reflection" not in digest(config, manager)["rows"][0]


def test_the_pending_list_is_what_goes_to_the_itsm_owner(portal) -> None:
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A", cpu=4)],
         vms=[vm("u1", hostname="srv", ip="10.0.0.3", name="srv-vm", cpu=8),
              vm("u2", hostname="ok", ip="10.0.0.4", name="ok-vm", cpu=4)],
         events=[
             {"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_CPU_CHANGED",
              "field_name": "cpus", "old_value": "4", "new_value": "8"},
             {"source": "RVTOOLS", "asset_key": "u2", "event_type": "RV_HOST_CHANGED",
              "field_name": "esxi_host", "old_value": "esxi-01", "new_value": "esxi-02"},
         ])
    with manager.connect() as conn:
        pending = ChangeDigestService(config, AssetRepository(conn)).pending_itsm(START, END)
    assert [row["asset_key"] for row in pending] == ["u1"]
    assert digest(config, manager)["summary"]["itsm_pending"] == 1


# ── 엑셀 ───────────────────────────────────────────────────────────────


def test_the_export_leads_with_the_server_and_what_to_do(portal, tmp_path: Path) -> None:
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A", cpu=4)],
         vms=[vm("u1", hostname="srv", ip="10.0.0.3", name="srv-vm", cpu=8)],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_CPU_CHANGED",
                  "field_name": "cpus", "old_value": "4", "new_value": "8"}])
    result = digest(config, manager)
    path = tmp_path / "digest.xlsx"
    ChangeDigestService.write_xlsx(result, result["rows"], path)

    sheet = load_workbook(path).active
    head = next(r for r in range(1, sheet.max_row + 1) if sheet.cell(r, 1).value == "일시")
    labels = [sheet.cell(head, c).value for c in range(1, 14)]
    for name in ("서버", "호스트명", "IP", "바뀐 내용", "ITSM 반영", "해야 할 일"):
        assert name in labels, labels
    row = {label: sheet.cell(head + 1, index).value
           for index, label in enumerate(labels, start=1)}
    assert row["ITSM 반영"] == "ITSM 미반영"
    assert row["해야 할 일"] == "ITSM 값을 vCenter 와 맞추기"
    assert "미반영 1건" in str(sheet["A2"].value)


# ── 화면이 받는 모양 ───────────────────────────────────────────────────


def test_every_row_carries_the_day_it_belongs_to(portal) -> None:
    """일간 화면은 날짜로 묶는다. ``day`` 가 없으면 '가장 최근 변경일' 을 못 찾는다."""
    config, manager = portal
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A")],
         events=[field_event("CM001", "CM_NAME", "업무A", "업무B")])
    rows = digest(config, manager)["rows"]
    assert rows and all(row["day"] == DAY for row in rows), rows


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    app = create_app()
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    yield client
    reset_database_manager()


def test_the_screen_gets_the_digest_over_http(web) -> None:
    """서비스가 맞아도 주소가 어긋나면 화면은 빈 표만 보여 준다."""
    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager

    config = load_config()
    manager = create_manager(config)
    seed(manager,
         assets=[asset("CM001", hostname="srv", ip="10.0.0.3", name="업무A", cpu=4)],
         vms=[vm("u1", hostname="srv", ip="10.0.0.3", name="srv-vm", cpu=8)],
         events=[{"source": "RVTOOLS", "asset_key": "u1", "event_type": "RV_CPU_CHANGED",
                  "field_name": "cpus", "old_value": "4", "new_value": "8"}])

    payload = web.get(f"/api/asset-sync/change-digest?start={START}&end={END}").get_json()
    assert payload["summary"]["servers"] == 1, payload
    row = payload["rows"][0]
    assert row["day"] == DAY
    assert row["reflection"] == NOT_REFLECTED
    assert row["kind"] == CHANGED

    # ITSM 미반영만 거르는 길도 서버가 안다. 화면마다 따로 세면 또 어긋난다.
    only = web.get(
        f"/api/asset-sync/change-digest?start={START}&end={END}&pending=1").get_json()
    assert [r["asset_key"] for r in only["rows"]] == ["u1"]


def test_start_and_end_are_required(web) -> None:
    response = web.get("/api/asset-sync/change-digest")
    assert response.status_code == 400
    assert "start" in response.get_json()["error"]
