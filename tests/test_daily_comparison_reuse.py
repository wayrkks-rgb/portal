"""전일 대비가 저장된 변경 이벤트를 다시 계산하지 않는지 확인한다.

수집할 때 같은 스냅샷 짝을 이미 비교해 저장해 둔다. 그런데도 화면을 열 때마다
스냅샷 두 개를 전부 읽어 파이썬으로 재비교하면 그 시간이 그대로 대기시간이 된다.
눈에 보이는 오류가 없으므로 테스트로만 지킬 수 있다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services import DailyComparisonService
from asset_sync.services.diff_service import DiffService

ASSETS = 30
CHANGED = 10

ITSM_CFG = {
    "collection_mode": "ORACLE",
    "cpu_compare_field": "CM_CPU_CORE_CNT",
    "memory_field": "CM_MEMORY",
    "os_eos_field": "OS_EOS_DATE",
    "tracked_fields": ["CM_HOSTNAME", "CM_IP", "CM_OS", "CM_CPU_CORE_CNT", "CM_STA_CD"],
}


def raw_record(index: int, cpu: int) -> str:
    return json.dumps({
        "CM_ID": f"CM{index:04d}", "CM_NAME": "srv", "CM_HOSTNAME": f"host{index}",
        "CM_IP": f"10.0.0.{index}", "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6",
        "CM_CPU_CORE_CNT": str(cpu), "CM_MEMORY": "8", "CM_STA_CD": "CMSTA010",
        "CM_SVR_CAT_CD": "CMSVRCATCD020", "OS_EOS_DATE": "2030-01-01",
    }, ensure_ascii=False)


@pytest.fixture()
def seeded(tmp_path: Path):
    """어제/오늘 스냅샷을 만든다. 오늘 앞쪽 CHANGED 건의 CPU 가 바뀐다."""
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/test.db"), itsm=dict(ITSM_CFG))
    manager = create_manager(config)
    manager.initialize()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        snapshots = []
        for day_offset in (1, 0):
            day = datetime.now() - timedelta(days=day_offset)
            run = repo.start_collection_run("ITSM", day.isoformat())
            repo.finish_collection_run(run, "SUCCESS", ASSETS, day.isoformat(), ["ALL"])
            snapshot_id = repo.create_snapshot(
                "ITSM", day.date().isoformat(), day.isoformat(), run, "SUCCESS", ASSETS, "hash"
            )
            snapshots.append(snapshot_id)
            conn.executemany(
                "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
                "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
                "environment_code,eos_value,record_hash,raw_json) "
                "VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
                [(snapshot_id, f"CM{i:04d}", f"host{i}", f"10.0.0.{i}",
                  8 if (day_offset == 0 and i < CHANGED) else 4, 8192, "Linux", "8.6",
                  "CMSTA010", "CMSVRCATCD020", "CMOWNCATCD0010", "2030-01-01", f"h{i}",
                  raw_record(i, 8 if (day_offset == 0 and i < CHANGED) else 4))
                 for i in range(ASSETS)],
            )
        conn.commit()
    return config, manager, snapshots


def count_snapshot_reads(repo: AssetRepository, monkeypatch) -> list[int]:
    calls: list[int] = []
    original = repo.load_itsm_records

    def counted(snapshot_id: int, with_raw: bool = True):
        calls.append(int(snapshot_id))
        return original(snapshot_id, with_raw)

    monkeypatch.setattr(repo, "load_itsm_records", counted)
    return calls


def test_without_stored_events_it_recomputes(seeded, monkeypatch):
    config, manager, _ = seeded
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        calls = count_snapshot_reads(repo, monkeypatch)
        result = DailyComparisonService(config, repo).latest("ITSM")

    assert result["events_from"] == "RECOMPUTED"
    assert calls, "재계산이면 스냅샷을 읽어야 한다"
    assert result["counts"]["CHANGED"] == CHANGED


def test_with_stored_events_it_reads_them_instead(seeded, monkeypatch):
    config, manager, snapshots = seeded
    # 수집할 때 하는 일과 같다.
    with manager.connect() as conn:
        DiffService(config, AssetRepository(conn)).compare_itsm(snapshots[1])
        conn.commit()

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        calls = count_snapshot_reads(repo, monkeypatch)
        result = DailyComparisonService(config, repo).latest("ITSM")

    assert result["events_from"] == "STORED"
    # 저장된 이벤트를 쓰므로 재비교는 하지 않는다. 다만 자산코드만으로는 어느
    # 서버인지 알 수 없어, 호스트명·IP·업무명을 붙이려고 스냅샷을 읽는다.
    # 읽는 횟수는 **이벤트 수와 무관하게** 짝(현재·이전) 수를 넘지 않아야 한다.
    # 예전에 여기서 이벤트마다 읽어 21초가 걸린 적이 있다.
    assert len(set(calls)) <= 2, f"스냅샷을 {len(set(calls))}종류나 읽었다"
    assert len(calls) <= 2, f"같은 스냅샷을 {len(calls)}번 읽었다(캐시가 동작하지 않음)"


def test_identity_lookup_does_not_grow_with_the_number_of_events(seeded, monkeypatch):
    """이름을 붙이는 비용이 이벤트 수를 따라 늘면 안 된다.

    예전에 ``cache.setdefault(sid, load(sid))`` 로 적었다가, 기본값이 먼저
    계산되는 바람에 캐시가 아무 일도 못 하고 이벤트마다 스냅샷을 읽은 적이 있다.
    """
    config, manager, snapshots = seeded
    with manager.connect() as conn:
        DiffService(config, AssetRepository(conn)).compare_itsm(snapshots[1])
        conn.commit()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        calls = count_snapshot_reads(repo, monkeypatch)
        result = DailyComparisonService(config, repo).latest("ITSM")

    assert len(result["events"]) >= CHANGED, "이벤트가 있어야 비교가 된다"
    assert len(calls) <= 2, (
        f"이벤트 {len(result['events'])}건에 스냅샷을 {len(calls)}번 읽었다"
    )
    # 붙인 값이 실제로 들어 있어야 의미가 있다.
    assert any(event.get("hostname") for event in result["events"])


def test_both_paths_give_the_same_answer(seeded):
    """빠른 길이 다른 결과를 내면 고친 의미가 없다."""
    config, manager, snapshots = seeded
    with manager.connect() as conn:
        recomputed = DailyComparisonService(config, AssetRepository(conn)).latest("ITSM")
    with manager.connect() as conn:
        DiffService(config, AssetRepository(conn)).compare_itsm(snapshots[1])
        conn.commit()
    with manager.connect() as conn:
        stored = DailyComparisonService(config, AssetRepository(conn)).latest("ITSM")

    assert recomputed["events_from"] == "RECOMPUTED"
    assert stored["events_from"] == "STORED"
    assert stored["counts"] == recomputed["counts"]
    assert stored["event_type_counts"] == recomputed["event_type_counts"]

    def shape(result):
        return sorted(
            (e["asset_key"], e["event_type"], e.get("field_name"),
             e.get("old_value"), e.get("new_value"))
            for e in result["events"]
        )

    assert shape(stored) == shape(recomputed)


def test_the_displayed_values_survive_the_fast_path(seeded):
    """저장된 이벤트도 라벨·단위가 붙어 나와야 한다."""
    config, manager, snapshots = seeded
    with manager.connect() as conn:
        DiffService(config, AssetRepository(conn)).compare_itsm(snapshots[1])
        conn.commit()
    with manager.connect() as conn:
        result = DailyComparisonService(config, AssetRepository(conn)).latest("ITSM")

    cpu = next(e for e in result["events"] if e.get("field_name") == "CM_CPU_CORE_CNT")
    assert cpu["field_label"] == "CPU 코어"
    assert cpu["old_display"] == "4" and cpu["new_display"] == "8"
