"""스냅샷을 이벤트 수만큼 다시 읽지 않는지 확인한다.

``cache.setdefault(sid, load(sid))`` 는 이미 값이 있어도 두 번째 인자를 **먼저**
계산한다. 캐시처럼 보이지만 매번 스냅샷 전체를 다시 읽었고, 자산이 늘수록
대시보드가 느려졌다. 눈에 보이는 오류가 없어 테스트로만 잡을 수 있다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services import IntegratedDashboardService

ASSETS = 40
EVENTS = 25


@pytest.fixture()
def seed(tmp_path: Path):
    """이벤트 건수를 바꿔가며 같은 데이터를 만들 수 있게 한다."""
    def build(event_count: int = EVENTS, name: str = "test"):
        return _seed(tmp_path / name, event_count)
    return build


@pytest.fixture()
def seeded(seed):
    return seed()


def _seed(root: Path, event_count: int):
    root.mkdir(parents=True, exist_ok=True)
    config = AppConfig(root_dir=root, sqlite_path=Path("data/test.db"))
    manager = create_manager(config)
    manager.initialize()
    now = datetime.now().isoformat()
    today = datetime.now().date().isoformat()
    raw = json.dumps({"CM_NAME": "srv", "CM_HOSTNAME": "host"}, ensure_ascii=False)

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        snapshots = []
        for _ in range(2):
            run = repo.start_collection_run("ITSM", now)
            repo.finish_collection_run(run, "SUCCESS", ASSETS, now, ["ALL"])
            snapshot_id = repo.create_snapshot("ITSM", today, now, run, "SUCCESS", ASSETS, "hash")
            conn.executemany(
                "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
                "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
                "environment_code,eos_value,record_hash,raw_json) "
                "VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
                [(snapshot_id, f"CM{index:04d}", f"host{index}", f"10.0.0.{index}", 4, 8192,
                  "Linux", "8.6", "CMSTA010", "CMSVRCATCD020", "CMOWNCATCD0010", "2030-01-01",
                  f"h{index}", raw) for index in range(ASSETS)],
            )
            snapshots.append(snapshot_id)
        repo.replace_change_events(snapshots[1], [
            {"source": "ITSM", "asset_key": f"CM{index:04d}", "event_type": "ITSM_CPU_CHANGED",
             "previous_snapshot_id": snapshots[0], "detected_at": now,
             "field_name": "CM_CPU_CORE_CNT", "old_value": "4", "new_value": "8",
             "group_key": None, "metadata": {}}
            for index in range(event_count)
        ])
        conn.commit()
    return manager, snapshots


def count_loads(repo: AssetRepository, monkeypatch) -> list[int]:
    calls: list[int] = []
    original = repo.load_itsm_records

    def counted(snapshot_id: int, with_raw: bool = True):
        calls.append(int(snapshot_id))
        return original(snapshot_id, with_raw)

    monkeypatch.setattr(repo, "load_itsm_records", counted)
    return calls


def test_snapshot_reads_do_not_grow_with_the_number_of_events(seed, monkeypatch):
    """읽기 횟수가 이벤트 건수를 따라 늘면, 자산이 늘수록 화면이 느려진다."""
    def reads(event_count: int, name: str) -> int:
        manager, _ = seed(event_count, name)
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            calls = count_loads(repo, monkeypatch)
            IntegratedDashboardService(repo).summary()
        return len(calls)

    few = reads(2, "few")
    many = reads(EVENTS, "many")

    assert few, "스냅샷을 한 번도 읽지 않았다면 이 테스트는 아무것도 지키지 못한다"
    assert many == few, (
        f"이벤트 2건일 때 {few}번, {EVENTS}건일 때 {many}번 읽었다. "
        "이벤트마다 스냅샷 전체를 다시 읽고 있다."
    )


def test_summary_still_returns_the_expected_shape(seeded):
    manager, _ = seeded
    with manager.connect() as conn:
        result = IntegratedDashboardService(AssetRepository(conn)).summary()
    assert result["asset_status"]["total"] == ASSETS
    assert result["itsm_changes"]["total_events"] == EVENTS


def test_records_can_be_loaded_without_parsing_the_original_row(seeded):
    """집계는 정규화된 컬럼만 쓴다. 원본까지 풀면 행마다 비용이 든다."""
    manager, snapshots = seeded
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        light = repo.load_itsm_records(snapshots[0], with_raw=False)
        full = repo.load_itsm_records(snapshots[0])
    assert len(light) == len(full) == ASSETS
    assert light["CM0000"]["raw"] == {}
    assert full["CM0000"]["raw"]["CM_NAME"] == "srv"
    # 원본 말고는 똑같아야 한다.
    assert light["CM0000"]["normalized_hostname"] == full["CM0000"]["normalized_hostname"]
