"""실제로 쓰지 않는 VM 을 세지 않는다.

vCLS 는 vCenter 가 클러스터마다 스스로 만들고 수시로 다시 만든다. 세면 대수가
흔들리고, 다시 만들어질 때마다 식별자가 바뀌어 변경 내역에도 생성·삭제로
올라온다. 실제 서버가 아니므로 빼는 쪽이 맞다.

그리고 **키가 바뀐 것을 생성·삭제로 세지 않는** 성질을 지킨다. asset_key 는
vm_uuid → smbios_uuid → MoRef → 이름 순으로 고른다. 어느 날 vCenter 가 Config
를 돌려주지 않으면 uuid 가 비고 키가 MoRef 로 떨어진다. 그러면 삭제·생성이 한
쌍씩 생기는데 실제로는 아무 일도 없었다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.asset_scope import AssetScope, criteria_from, is_system_vm, save_rules
from asset_sync.services.diff_service import DiffService

CRITERIA = criteria_from(None)


def vm(key, name, **overrides):
    record = {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"vm-{key}",
        "vcenter": "VC1", "vm_name": name, "normalized_hostname": name.lower(),
        "primary_ip": "10.0.0.5", "cpus": 4, "memory_mb": 8192,
        "os_family": "Linux Redhat", "os_version": "8.6", "power_state": "poweredon",
        "datacenter": "DC1", "cluster_name": "CL1", "esxi_host": "esxi-01",
        "template_flag": False, "srm_placeholder": False, "raw": {"VM": name},
    }
    record.update(overrides)
    return record


# ── 어떤 VM 을 세지 않는가 ───────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("vCLS-8f3a21", True),
    ("vCLS (1)", True),
    ("VCLS-UPPER", True),          # 대소문자 상관없이
    ("NSX-Edge-01", True),
    ("web-prod-01", False),
    ("my-vclservice", True),       # 조각이 들어 있으면 걸린다
    ("", False),
    (None, False),
])
def test_system_vms_are_recognised_by_name(name, expected):
    assert is_system_vm(name, CRITERIA) is expected


def test_a_system_vm_is_not_counted(tmp_path):
    scope = AssetScope(CRITERIA)
    assert scope.decide_vcenter(vm("a", "web-01")).included is True

    decision = scope.decide_vcenter(vm("b", "vCLS-abc"))
    assert decision.included is False
    assert decision.reason == "SYSTEM_VM"


def test_a_template_is_not_counted():
    scope = AssetScope(CRITERIA)
    decision = scope.decide_vcenter(vm("c", "golden-image", template_flag=True))
    assert decision.included is False
    assert decision.reason == "TEMPLATE"


def test_the_pattern_list_can_be_changed(tmp_path):
    """설치마다 시스템 VM 이름이 다르다. 설정으로 바꿀 수 있어야 한다."""
    config = AppConfig(
        root_dir=tmp_path, sqlite_path=Path("data/x.db"),
        server_status={"system_vm_patterns": ["ZZ-SYS-"]},
    )
    scope = AssetScope(criteria_from(config))
    assert scope.decide_vcenter(vm("d", "ZZ-SYS-01")).included is False
    # 바꿨으면 기본 목록은 쓰지 않는다. 그래야 의도대로 동작한다.
    assert scope.decide_vcenter(vm("e", "vCLS-abc")).included is True


def test_a_system_vm_can_be_put_back_by_hand(tmp_path):
    """자동 판정이 틀릴 수 있다. 사람이 '이건 쓰는 VM' 이라고 되돌릴 수 있어야 한다."""
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/vmscope.db"))
    manager = create_manager(config)
    manager.initialize()
    with manager.connect() as conn:
        save_rules(AssetRepository(conn), "RVTOOLS", [{"asset_key": "b"}],
                   mode="INCLUDE", reason="이름만 비슷한 실제 서버")
    with manager.connect() as conn:
        scope = AssetScope.load(config, AssetRepository(conn))
    assert scope.decide_vcenter(vm("b", "vCLS-abc")).included is True


def test_a_normal_vm_can_be_excluded_by_hand(tmp_path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/vmscope2.db"))
    manager = create_manager(config)
    manager.initialize()
    with manager.connect() as conn:
        save_rules(AssetRepository(conn), "RVTOOLS", [{"asset_key": "a"}],
                   mode="EXCLUDE", reason="폐기 예정")
    with manager.connect() as conn:
        scope = AssetScope.load(config, AssetRepository(conn))
    decision = scope.decide_vcenter(vm("a", "web-01"))
    assert decision.included is False
    assert decision.reason == "MANUAL"
    assert decision.note == "폐기 예정"


def test_the_summary_says_how_many_were_dropped_and_why():
    scope = AssetScope(CRITERIA)
    records = [
        vm("a", "web-01"), vm("b", "web-02"),
        vm("c", "vCLS-1"), vm("d", "vCLS-2"), vm("e", "vCLS-3"),
        vm("f", "golden", template_flag=True),
    ]
    summary = scope.vcenter_summary(records)
    assert summary["snapshot_total"] == 6
    assert summary["selected"] == 2
    assert summary["by_reason"]["SYSTEM_VM"]["count"] == 3
    assert summary["by_reason"]["TEMPLATE"]["count"] == 1


def test_include_all_brings_them_back_with_the_reason_kept():
    scope = AssetScope(CRITERIA, include_all=True)
    decision = scope.decide_vcenter(vm("c", "vCLS-1"))
    assert decision.included is True
    assert decision.reason == "SYSTEM_VM"


# ── 키가 바뀐 것을 생성·삭제로 세지 않는다 ────────────────────────────────
def _diff(old: list[dict], current: list[dict]) -> list[dict]:
    config = AppConfig(root_dir=Path("/tmp"), sqlite_path=Path("x.db"))
    service = DiffService.__new__(DiffService)
    service.config = config
    return service._rv_events(
        {item["asset_key"]: item for item in old},
        {item["asset_key"]: item for item in current},
        1, "2026-10-02T07:00:00", set(),
    )


def _types(events: list[dict]) -> set[str]:
    return {event["event_type"] for event in events}


def test_a_key_that_flipped_is_not_a_creation_or_deletion():
    """uuid 를 못 읽은 날 키가 MoRef 로 떨어진다. 같은 VM 이다."""
    before = [vm("uuid-1", "web-01")]              # vm_id = "vm-uuid-1"
    # 다음 날: Config 를 못 읽어 uuid 가 비고 키가 vCenter|MoRef 로 떨어졌다.
    # MoRef(vm_id) 는 그대로다 -- 그게 같은 VM 이라는 근거다.
    after = [vm("VC1|vm-uuid-1", "web-01", vm_uuid=None, smbios_uuid=None, vm_id="vm-uuid-1")]
    events = _diff(before, after)

    assert "RV_NEW" not in _types(events)
    assert "RV_REMOVED" not in _types(events)
    # 무슨 일이 있었는지는 남겨야 한다. 조용히 넘기면 나중에 설명할 수 없다.
    assert "RV_KEY_CHANGED" in _types(events)
    changed = next(e for e in events if e["event_type"] == "RV_KEY_CHANGED")
    assert changed["old_value"] == "uuid-1"
    assert changed["new_value"] == "VC1|vm-uuid-1"


def test_a_change_during_the_key_flip_is_still_caught():
    """키가 바뀐 사이에 CPU 가 늘었을 수 있다. 그것도 잡아야 한다."""
    before = [vm("uuid-1", "web-01", cpus=4)]
    after = [vm("VC1|vm-uuid-1", "web-01", vm_uuid=None, smbios_uuid=None,
                vm_id="vm-uuid-1", cpus=8)]
    events = _diff(before, after)
    cpu = [e for e in events if e["event_type"] == "RV_CPU_CHANGED"]
    assert len(cpu) == 1
    assert cpu[0]["old_value"] == "4"
    assert cpu[0]["new_value"] == "8"


def test_a_real_creation_is_still_a_creation():
    """이어 붙이기가 과하면 실제 생성·삭제를 놓친다."""
    events = _diff([vm("uuid-1", "web-01")], [vm("uuid-1", "web-01"), vm("uuid-9", "web-09")])
    assert "RV_NEW" in _types(events)
    assert "RV_KEY_CHANGED" not in _types(events)


def test_a_real_deletion_is_still_a_deletion():
    events = _diff([vm("uuid-1", "web-01"), vm("uuid-9", "web-09")], [vm("uuid-1", "web-01")])
    assert "RV_REMOVED" in _types(events)
    assert "RV_KEY_CHANGED" not in _types(events)


def test_two_vms_with_no_shared_identifier_are_not_linked():
    """식별자가 전부 다르면 모르는 것이다. 모르는 것을 같다고 하면 안 된다."""
    before = [vm("uuid-1", "web-01")]
    after = [vm("uuid-2", "web-01", vm_uuid="uuid-2", smbios_uuid="sm-2", vm_id="vm-2")]
    events = _diff(before, after)
    assert _types(events) == {"RV_NEW", "RV_REMOVED"}


def test_a_vm_moved_to_another_vcenter_is_not_linked_by_moref():
    """MoRef 는 vCenter 안에서만 뜻이 있다. 다른 vCenter 의 같은 MoRef 는 남이다."""
    before = [vm("VC1|vm-7", "web-01", vm_uuid=None, smbios_uuid=None, vm_id="vm-7", vcenter="VC1")]
    after = [vm("VC2|vm-7", "web-01", vm_uuid=None, smbios_uuid=None, vm_id="vm-7", vcenter="VC2")]
    events = _diff(before, after)
    assert _types(events) == {"RV_NEW", "RV_REMOVED"}


def test_one_removed_vm_is_linked_to_only_one_added_vm():
    """한 쪽을 두 번 쓰면 대수가 맞지 않는다."""
    before = [vm("uuid-1", "web-01")]
    after = [
        vm("VC1|vm-uuid-1", "web-01", vm_uuid=None, smbios_uuid=None, vm_id="vm-uuid-1"),
        # 복제본도 같은 MoRef 를 들고 온 것처럼 꾸민다. 한 쪽만 이어 붙어야 한다.
        vm("VC1|vm-uuid-1-b", "web-01-clone", vm_uuid=None, smbios_uuid=None, vm_id="vm-uuid-1"),
    ]
    events = _diff(before, after)
    assert len([e for e in events if e["event_type"] == "RV_KEY_CHANGED"]) == 1
    assert len([e for e in events if e["event_type"] == "RV_NEW"]) == 1


# ── 변경 내역에서도 빠지는가 ─────────────────────────────────────────────
def test_a_system_vms_churn_does_not_flood_the_change_history(tmp_path):
    """vCLS 는 매일 다시 만들어진다. 그냥 두면 진짜 변경이 묻힌다."""
    from asset_sync.services.change_presenter import attach_identity

    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/churn.db"))
    manager = create_manager(config)
    manager.initialize()

    yesterday = datetime.now().replace(hour=7)
    today = datetime.now()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        ids = []
        for day, vms in (
            (yesterday, [vm("u1", "web-01"), vm("vcls-a", "vCLS-aaaa")]),
            (today, [vm("u1", "web-01", cpus=8), vm("vcls-b", "vCLS-bbbb")]),
        ):
            run = repo.start_collection_run("RVTOOLS", day.isoformat())
            repo.finish_collection_run(run, "SUCCESS", len(vms), day.isoformat(), ["VC1"])
            snapshot_id = repo.create_snapshot(
                "RVTOOLS", day.date().isoformat(), day.isoformat(), run, "SUCCESS", len(vms), "h")
            repo.insert_rv_records(snapshot_id, [dict(item, record_hash="h") for item in vms])
            ids.append(snapshot_id)
        DiffService(config, repo).compare_rvtools(ids[1])
        conn.commit()

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        stored = repo.changes(source="RVTOOLS", limit=100)
        shown = attach_identity(stored, repo, scope=AssetScope.load(config, repo))

    stored_types = {event["event_type"] for event in stored}
    shown_types = {event["event_type"] for event in shown}
    # vCLS 가 바뀐 것은 저장은 되지만 화면에는 올라오지 않는다.
    assert {"RV_NEW", "RV_REMOVED"} <= stored_types
    assert "RV_NEW" not in shown_types
    assert "RV_REMOVED" not in shown_types
    # 진짜 변경(실서버 CPU)은 그대로 보인다.
    assert "RV_CPU_CHANGED" in shown_types


# ── ITSM 자산과 vCenter VM 은 서로 섞이지 않는다 ─────────────────────────
def test_excluding_a_vm_does_not_change_the_server_count(tmp_path):
    """VM 을 뺐다고 서버 현황 대수가 변하면 안 된다.

    둘은 다른 목록이다. ITSM 은 자산번호(cm_id), vCenter 는 VM 식별자(uuid)로
    세고, 제외 규칙도 출처별로 따로 저장한다. 같은 서버가 양쪽에 있어도 키가
    다르므로 한쪽을 빼도 다른 쪽은 그대로다.
    """
    import json as _json

    from asset_sync.services.server_status_service import ServerStatusService

    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/sep.db"))
    manager = create_manager(config)
    manager.initialize()
    now = datetime.now()

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", now.isoformat())
        repo.finish_collection_run(run, "SUCCESS", 3, now.isoformat(), ["ALL"])
        itsm_id = repo.create_snapshot(
            "ITSM", now.date().isoformat(), now.isoformat(), run, "SUCCESS", 3, "h")
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [(itsm_id, f"CM000{i}", f"host-{i}", "10.0.0.5", 4, 8192, "Linux Redhat", "8.6",
              "CMSTA010", "CMSVRCATCD020", "CMOWNCATCD0010", "2030-12-31", "h",
              _json.dumps({"CM_ID": f"CM000{i}", "CM_OS": "CMCIOSCD010",
                           "CM_OS_VERSION": "8.6", "CM_EOL_DT": "2030-12-31",
                           "CM_PLACE": "CMPLACE010"}, ensure_ascii=False)) for i in range(3)],
        )
        vms = [dict(vm(f"u{i}", f"host-{i}", power_state="poweredoff"), record_hash="h")
               for i in range(3)]
        run2 = repo.start_collection_run("RVTOOLS", now.isoformat())
        repo.finish_collection_run(run2, "SUCCESS", len(vms), now.isoformat(), ["VC1"])
        rv_id = repo.create_snapshot(
            "RVTOOLS", now.date().isoformat(), now.isoformat(), run2, "SUCCESS", len(vms), "h")
        repo.insert_rv_records(rv_id, vms)
        conn.commit()

    def counts():
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            scope = AssetScope.load(config, repo)
            servers = ServerStatusService(config, repo).status(itsm_id)
            kept, _ = scope.split_vcenter(list(repo.load_rv_records(rv_id).values()))
        return servers["all"]["table"]["rows"]["계"]["소계"], len(kept)

    assert counts() == (3, 3)

    # 전원 꺼진 VM 을 전부 빼도 서버 현황은 그대로다.
    with manager.connect() as conn:
        save_rules(AssetRepository(conn), "RVTOOLS",
                   [{"asset_key": f"u{i}"} for i in range(3)],
                   mode="EXCLUDE", reason="전원 꺼짐")
    assert counts() == (3, 0)

    # 거꾸로 ITSM 자산을 빼도 VM 대수는 그대로다.
    with manager.connect() as conn:
        save_rules(AssetRepository(conn), "ITSM", [{"asset_key": "CM0000"}], mode="EXCLUDE")
    assert counts() == (2, 0)


def test_the_same_asset_key_in_both_sources_stays_separate(tmp_path):
    """키가 우연히 같아도 출처가 다르면 다른 규칙이다."""
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/sep2.db"))
    manager = create_manager(config)
    manager.initialize()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        save_rules(repo, "RVTOOLS", [{"asset_key": "SAME-KEY"}], mode="EXCLUDE", reason="VM 쪽")
    with manager.connect() as conn:
        scope = AssetScope.load(config, AssetRepository(conn))
    assert scope.rule_for("RVTOOLS", "SAME-KEY") is not None
    assert scope.rule_for("ITSM", "SAME-KEY") is None


def test_a_file_from_the_wrong_list_is_recognised():
    """VM 목록을 ITSM 창에서 올리는 실수를 미리 잡는다."""
    from asset_sync.services.asset_scope import detect_source

    vm_sheet = [["처리", "제외 사유", "VM 이름", "호스트명", "vCenter", "전원", "VM UUID", "자산키"]]
    itsm_sheet = [["처리", "제외 사유", "자산번호", "업무명", "물리/논리", "CM_ID", "CM_PLACE"]]
    assert detect_source(vm_sheet) == "RVTOOLS"
    assert detect_source(itsm_sheet) == "ITSM"
    # 어느 쪽인지 알 수 없으면 모른다고 해야 한다. 틀리게 막으면 더 나쁘다.
    assert detect_source([["자산번호", "메모"]]) is None
