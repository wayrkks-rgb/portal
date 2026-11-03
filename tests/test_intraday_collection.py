"""수시 수집(30분 · 1시간): 바뀐 것만 저장하고, 하루 안의 변동도 보고에 맞게 잡힌다.

확인하는 것
1. 바뀐 것이 없으면 스냅샷을 만들지 않는다(DB 가 커지지 않게).
2. 바뀌면 스냅샷 · 변경 이벤트가 남는다.
3. 일부 통합기 실패 · 건수 이상이면 저장하지 않는다(가짜 삭제를 막는다).
4. 하루 안에 꺼졌다 켜진 VM 은 그날 [변경] 이고, 꺼진 채 끝나면 [삭제] 다.
5. 오래된 수시 스냅샷을 정리해도 변경 이력과 일 · 주 · 월 숫자는 그대로다.
6. 작업 등록 명령 · 설정 저장이 수시 수집 설정을 지우지 않는다.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from asset_sync import scheduler
from asset_sync.collectors import synthetic_collectors
from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services import collection_service as collection_module
from asset_sync.services.change_digest_service import ChangeDigestService
from asset_sync.services.collection_service import CollectionService
from asset_sync.services.diff_service import DiffService
from asset_sync.services.retention_service import RetentionService


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root_dir=tmp_path,
        sqlite_path=Path("data/test.db"),
        itsm={
            "collection_mode": "DEMO", "memory_unit": "GB", "cpu_compare_field": "CM_CPU_CORE_CNT",
            "memory_field": "CM_MEMORY", "os_eos_field": "OS_EOS_DATE", "tracked_fields": [], "ignore_fields": [],
            "owner_field": "CM_WOR_MNG_EMP_ID",
        },
        rvtools={
            "collection_mode": "DEMO", "hostname_suffixes": [".example.invalid"], "power_on_value": "poweredon",
            "exclude_templates": True, "exclude_srm_placeholders": True,
        },
        matching={"use_identity_map_first": True, "auto_remember_exact_match": True, "memory_tolerance_mb": 1},
        quality={"minimum_itsm_records": 1, "minimum_rvtools_records": 1, "eos_near_days": 180},
    )


@pytest.fixture()
def env(tmp_path):
    config = make_config(tmp_path)
    manager = SQLiteManager(config.database_path)
    manager.initialize()
    return config, manager


def snapshot_count(manager, source: str) -> int:
    with manager.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM snapshot WHERE source=?", (source,)).fetchone()["n"])


class FakeVCenter:
    """데모 vCenter 를 바꿔 가며 돌려준다. ``rows`` 를 고치면 다음 수집에 반영된다."""

    rows: list[dict] = []
    meta: dict = {}

    def __init__(self, config) -> None:
        pass

    def collect(self):
        return copy.deepcopy(FakeVCenter.rows), copy.deepcopy(FakeVCenter.meta)


@pytest.fixture()
def fake_vcenter(monkeypatch):
    rows, meta = synthetic_collectors.SyntheticRVToolsCollector(None).collect()
    FakeVCenter.rows, FakeVCenter.meta = rows, meta
    monkeypatch.setattr(collection_module, "SyntheticRVToolsCollector", FakeVCenter)
    return FakeVCenter


def test_unchanged_runs_store_nothing_and_changes_are_stored(env, fake_vcenter) -> None:
    config, manager = env
    service = CollectionService(config, manager)
    first = service.run_daily(demo=True)
    assert first["status"] == "SUCCESS"
    base_rv, base_itsm = snapshot_count(manager, "RVTOOLS"), snapshot_count(manager, "ITSM")

    quiet = service.run_intraday()
    assert quiet["stored"] == []
    assert quiet["vcenter"]["skip_reason"] == "바뀐 것 없음" and quiet["itsm"]["skip_reason"] == "바뀐 것 없음"
    assert (snapshot_count(manager, "RVTOOLS"), snapshot_count(manager, "ITSM")) == (base_rv, base_itsm)
    # 수집은 했다는 기록은 남는다(언제 봤는지 알 수 있게).
    with manager.connect() as conn:
        meta = json.loads(conn.execute(
            "SELECT metadata_json FROM collection_run WHERE source='RVTOOLS' ORDER BY id DESC LIMIT 1"
        ).fetchone()["metadata_json"])
    assert meta["trigger"] == "INTRADAY" and meta["stored"] is False

    fake_vcenter.rows[0]["Powerstate"] = "poweredOff"           # 한 대 꺼짐
    changed = service.run_intraday()
    assert changed["stored"] == ["vcenter"]
    assert snapshot_count(manager, "RVTOOLS") == base_rv + 1
    with manager.connect() as conn:
        types = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM change_event WHERE source='RVTOOLS'").fetchall()]
    assert "RV_POWER_OFF" in types


def test_a_partial_intraday_run_is_not_stored(env, fake_vcenter) -> None:
    config, manager = env
    service = CollectionService(config, manager)
    service.run_daily(demo=True)
    before = snapshot_count(manager, "RVTOOLS")
    fake_vcenter.rows = fake_vcenter.rows[:1]                    # 통합기 하나가 실패해 VM 이 빠진 것처럼
    fake_vcenter.meta = {**fake_vcenter.meta, "failed_scopes": {"vc_b": "timeout"}}
    result = service.collect_vcenter(mode="DEMO", intraday=True)
    assert result["stored"] is False and result["skip_reason"].startswith("보류: 일부 통합기 수집 실패")
    assert snapshot_count(manager, "RVTOOLS") == before
    with manager.connect() as conn:
        removed = conn.execute("SELECT COUNT(*) AS n FROM change_event WHERE event_type='RV_REMOVED'").fetchone()["n"]
    assert removed == 0                                            # 가짜 삭제가 없다


# ── 하루 안의 변동 ───────────────────────────────────────────────────────
def put_rv(config, manager, stamp: str, records: list[dict], trigger: str = "INTRADAY") -> int:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        previous = repo.latest_snapshot("RVTOOLS")
        run = repo.start_collection_run("RVTOOLS", stamp)
        repo.finish_collection_run(run, "SUCCESS", len(records), stamp, ["VC1"], [], metadata={"trigger": trigger})
        snap = repo.create_snapshot("RVTOOLS", stamp[:10], stamp, run, "SUCCESS", len(records), stamp)
        repo.insert_rv_records(snap, records)
        if previous:
            pair = DiffService(config, repo).compare_pair("RVTOOLS", snap, int(previous["id"]))
            repo.replace_change_events(snap, pair["events"])
        return snap


def vm(power: str = "poweredon", key: str = "u1") -> dict:
    return {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}", "vcenter": "VC1",
        "vm_name": "WEB01", "dns_name": "web01", "normalized_hostname": "web01", "ip_addresses": ["10.0.0.1"],
        "primary_ip": "10.0.0.1", "cpus": 4, "memory_mb": 8192, "os_family": "Linux", "os_version": "8",
        "power_state": power, "datacenter": "DC", "cluster": "CL", "cluster_name": "CL", "esxi_host": "esx1",
        "template_flag": False, "srm_placeholder": False, "raw": {}, "record_hash": f"{key}-{power}",
    }


def day_rows(config, manager, day: str) -> list[dict]:
    with manager.connect() as conn:
        return ChangeDigestService(config, AssetRepository(conn)).digest(f"{day}T00:00:00", f"{day}T23:59:59")["rows"]


def test_off_then_on_within_a_day_is_a_change(env) -> None:
    config, manager = env
    put_rv(config, manager, "2026-11-02T07:00:00", [vm()], trigger="DAILY")
    put_rv(config, manager, "2026-11-03T07:00:00", [vm()], trigger="DAILY")
    put_rv(config, manager, "2026-11-03T10:00:00", [vm("poweredoff")])
    put_rv(config, manager, "2026-11-03T14:00:00", [vm()])
    [row] = day_rows(config, manager, "2026-11-03")
    assert row["kind"] == "CHANGED" and "하루 안에" in row["count_note"]
    assert row["vm_step"] == [2, 2]


def test_off_within_a_day_and_stays_off_is_a_removal(env) -> None:
    config, manager = env
    put_rv(config, manager, "2026-11-03T07:00:00", [vm()], trigger="DAILY")
    put_rv(config, manager, "2026-11-03T10:00:00", [vm("poweredoff")])
    put_rv(config, manager, "2026-11-03T12:00:00", [vm()])
    put_rv(config, manager, "2026-11-03T16:00:00", [vm("poweredoff")])
    [row] = day_rows(config, manager, "2026-11-03")
    assert (row["kind"], row["count_effect"]) == ("REMOVED", 0)
    assert "꺼졌습니다" in row["count_note"]


# ── 오래된 수시 스냅샷 정리 ─────────────────────────────────────────────────
def test_thinning_keeps_the_last_of_each_day_and_all_events(env) -> None:
    config, manager = env
    daily = put_rv(config, manager, "2026-10-01T07:00:00", [vm()], trigger="DAILY")
    morning = put_rv(config, manager, "2026-10-01T10:00:00", [vm("poweredoff")])
    noon = put_rv(config, manager, "2026-10-01T12:00:00", [vm()])
    evening = put_rv(config, manager, "2026-10-01T18:00:00", [vm("poweredoff")])
    recent = put_rv(config, manager, "2026-11-01T10:00:00", [vm()])
    before = day_rows(config, manager, "2026-10-01")
    with manager.connect() as conn:
        events_before = conn.execute("SELECT COUNT(*) AS n FROM change_event").fetchone()["n"]

    with manager.connect() as conn:
        dry = RetentionService(config, AssetRepository(conn)).thin_intraday(today=date(2026, 11, 3), dry_run=True)
    assert dry["deleted"]["RVTOOLS"] == 2 and snapshot_count(manager, "RVTOOLS") == 5

    with manager.connect() as conn:
        report = RetentionService(config, AssetRepository(conn)).thin_intraday(today=date(2026, 11, 3))
    assert report["deleted"]["RVTOOLS"] == 2
    with manager.connect() as conn:
        left = {int(r["id"]) for r in conn.execute("SELECT id FROM snapshot WHERE source='RVTOOLS'").fetchall()}
        events_after = conn.execute("SELECT COUNT(*) AS n FROM change_event").fetchone()["n"]
    # 07:00 일일 스냅샷 · 그날 마지막 · 최근(14일 안)은 남는다
    assert left == {daily, evening, recent} and morning not in left and noon not in left
    assert events_after == events_before                         # 변경 이력은 하나도 안 잃는다
    after = day_rows(config, manager, "2026-10-01")
    assert [(r["kind"], r["count_effect"]) for r in after] == [(r["kind"], r["count_effect"]) for r in before]


# ── 작업 등록 · 설정 ──────────────────────────────────────────────────────
def test_intraday_settings_and_task_command(monkeypatch, tmp_path) -> None:
    wanted = scheduler.intraday_settings({"intraday": {"enabled": True, "interval_minutes": 30,
                                                        "start_time": "08:30", "end_time": "19:00"}})
    assert wanted["task_name"] == "AssetIntradayCollection"
    with pytest.raises(scheduler.ScheduleError):
        scheduler.intraday_settings({"intraday": {"interval_minutes": 7}})
    with pytest.raises(scheduler.ScheduleError):
        scheduler.intraday_settings({"intraday": {"start_time": "20:00", "end_time": "08:00"}})

    from datetime import datetime
    assert scheduler.in_window(wanted, datetime(2026, 11, 3, 12, 0))
    assert not scheduler.in_window(wanted, datetime(2026, 11, 3, 7, 59))

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_intraday_collect.bat").write_text("@echo off", encoding="ascii")
    seen = {}
    monkeypatch.setattr(scheduler, "_run", lambda argv: (seen.setdefault("argv", argv), (0, "OK"))[1])
    scheduler.register_intraday(tmp_path, wanted)
    argv = seen["argv"]
    assert argv[argv.index("/RI") + 1] == "30" and argv[argv.index("/DU") + 1] == "10:30"
    assert argv[argv.index("/ST") + 1] == "08:30" and "SYSTEM" in argv


def test_saving_the_daily_card_keeps_intraday(tmp_path, monkeypatch) -> None:
    """실행 설정을 저장할 때 scheduler 를 통째로 덮어써 수시 수집 설정이 사라지면 안 된다."""
    from asset_sync.settings_store import LocalSettingsStore

    monkeypatch.setattr(scheduler, "apply_time", lambda *a, **k: {"applied": False})
    monkeypatch.setattr(scheduler, "apply_intraday", lambda *a, **k: {"applied": False})
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))   # 저장 후 다시 읽는 설정도 이 폴더에서
    store = LocalSettingsStore(tmp_path)
    saved = store.save_runtime({"scheduler": {"enabled": True, "daily_time": "07:00", "task_name": "T",
                                              "intraday": {"enabled": True, "interval_minutes": 30,
                                                           "start_time": "09:00", "end_time": "18:00"}}})
    assert saved["scheduler"]["intraday"]["interval_minutes"] == 30
    again = store.save_runtime({"scheduler": {"enabled": True, "daily_time": "06:30", "task_name": "T"}})
    assert again["scheduler"]["intraday"] == {**saved["scheduler"]["intraday"]}
    assert again["scheduler"]["daily_time"] == "06:30"
    import yaml
    stored = yaml.safe_load((tmp_path / "config" / "app_config.local.yaml").read_text(encoding="utf-8"))
    assert stored["scheduler"]["intraday"]["start_time"] == "09:00"


def test_the_intraday_batch_file_is_safe_for_cmd() -> None:
    root = Path(__file__).resolve().parents[1]
    data = (root / "scripts" / "run_intraday_collect.bat").read_bytes()
    assert all(byte < 128 for byte in data), "cmd 는 UTF-8 배치를 깨뜨린다. ASCII 로 둔다"
    text = data.decode("ascii")
    assert "jobs\\intraday_collect.py" in text and "logs\\intraday_console.log" in text


# ── 통합기 장애 뒤 복구: 가짜 생성이 없어야 하고, 그사이 실제 변화는 잡혀야 한다 ────────────
def put_scoped(config, manager, stamp, records, ok, failed):
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        previous = repo.latest_snapshot("RVTOOLS")
        status = "PARTIAL_SUCCESS" if failed else "SUCCESS"
        run = repo.start_collection_run("RVTOOLS", stamp)
        repo.finish_collection_run(run, status, len(records), stamp, ok, failed)
        snap = repo.create_snapshot("RVTOOLS", stamp[:10], stamp, run, status, len(records), stamp)
        repo.insert_rv_records(snap, records)
        if previous:
            return [(e["event_type"], e["asset_key"]) for e in DiffService(config, repo).compare_rvtools(snap)["events"]]
        return []


def vc2(key: str, power: str = "poweredon") -> dict:
    return dict(vm(power, key), vcenter="VC2", vm_name=key.upper(), normalized_hostname=key,
                dns_name=key, primary_ip=f"10.0.2.{len(key)}", ip_addresses=[f"10.0.2.{len(key)}"])


def test_a_recovered_vcenter_does_not_create_fake_new_vms(env) -> None:
    config, manager = env
    put_scoped(config, manager, "2026-11-01T07:00:00", [vm(), vc2("db1"), vc2("db2"), vc2("db3")], ["VC1", "VC2"], [])
    gap = put_scoped(config, manager, "2026-11-02T07:00:00", [vm()], ["VC1"], ["VC2"])
    assert sorted(gap) == [("COLLECTION_GAP", "db1"), ("COLLECTION_GAP", "db2"), ("COLLECTION_GAP", "db3")]
    # 하루 더 장애(연속 실패)도 되짚는다
    put_scoped(config, manager, "2026-11-03T07:00:00", [vm()], ["VC1"], ["VC2"])
    # 복구: db1 그대로, db2 는 그사이 꺼짐, db3 는 그사이 지워짐, db4 는 그사이 새로 생김
    back = put_scoped(config, manager, "2026-11-04T07:00:00",
                      [vm(), vc2("db1"), vc2("db2", "poweredoff"), vc2("db4")], ["VC1", "VC2"], [])
    assert sorted(back) == [("RV_NEW", "db4"), ("RV_POWER_OFF", "db2"), ("RV_REMOVED", "db3")]
