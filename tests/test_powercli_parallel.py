"""통합기를 동시에 수집하는지 확인한다.

한 대씩 순서대로 하면 통합기가 늘어나는 만큼 대기시간이 그대로 늘어난다.
대기시간의 대부분은 PowerCLI 모듈 로딩과 vCenter 응답 기다림이라, 동시에 돌리면
거의 한 대 시간에 끝난다.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from asset_sync.collectors.powercli_collector import PowerCLICollectionError, PowerCLICollector
from asset_sync.config import AppConfig

DELAY = 0.3


def make_collector(tmp_path: Path, count: int, **rvtools) -> PowerCLICollector:
    config = AppConfig(
        root_dir=tmp_path,
        rvtools={
            "collection_mode": "POWERCLI",
            "vcenters": [
                {"id": f"vc_{index:04d}", "name": f"통합기 {index}", "server": f"10.0.0.{index}",
                 "enabled": True}
                for index in range(1, count + 1)
            ],
            **rvtools,
        },
        security={},
    )
    return PowerCLICollector(config)


def fake_run_batch(records_per_vc: int = 2, delay: float = DELAY):
    """run_batch 를 가로채 프로세스 몇 개가 동시에 돌았는지 센다.

    한 호출이 PowerShell 프로세스 하나에 해당한다.
    """
    state = {"peak": 0, "current": 0, "calls": [], "sizes": []}
    lock = threading.Lock()

    def run_batch(entries):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            state["calls"].append([e["id"] for e in entries])
            state["sizes"].append(len(entries))
        time.sleep(delay)
        with lock:
            state["current"] -= 1
        return [{
            "id": entry["id"], "name": entry["name"], "status": "SUCCESS",
            "records": [{"VM": f"{entry['id']}-vm{n}"} for n in range(records_per_vc)],
            "row_count": records_per_vc, "json_file": None, "xlsx_file": None,
        } for entry in entries]

    return run_batch, state


def test_several_vcenters_are_collected_at_the_same_time(tmp_path, monkeypatch):
    collector = make_collector(tmp_path, 4, parallel_collections=4)
    run_batch, state = fake_run_batch()
    monkeypatch.setattr(collector, "run_batch", run_batch)

    started = time.perf_counter()
    records, metadata = collector.collect_all()
    elapsed = time.perf_counter() - started

    assert state["peak"] >= 2, f"동시에 돈 것이 {state['peak']}개뿐입니다. 순차 실행 중입니다."
    assert elapsed < DELAY * 4 * 0.8, f"4대를 동시에 돌렸는데 {elapsed:.2f}초 걸렸습니다."
    assert len(records) == 8
    assert metadata["success_scopes"] == ["vc_0001", "vc_0002", "vc_0003", "vc_0004"]


def test_more_vcenters_than_workers_share_processes(tmp_path, monkeypatch):
    """PowerCLI 모듈 로딩이 6~15초다. 통합기마다 프로세스를 띄우면 그만큼 곱해진다."""
    collector = make_collector(tmp_path, 10, parallel_collections=4)
    run_batch, state = fake_run_batch(delay=0.05)
    monkeypatch.setattr(collector, "run_batch", run_batch)

    collector.collect_all()

    assert len(state["calls"]) == 4, f"프로세스가 {len(state['calls'])}개입니다. 4개로 묶여야 합니다."
    assert sum(state["sizes"]) == 10
    # 한 프로세스에만 몰리면 그 프로세스가 끝날 때까지 기다려야 한다.
    assert max(state["sizes"]) - min(state["sizes"]) <= 1, f"고르지 않게 나눠졌습니다: {state['sizes']}"


def test_the_result_order_follows_the_configured_order(tmp_path, monkeypatch):
    """끝나는 순서는 제각각이다. 화면과 로그는 설정 순서대로 보여야 읽을 수 있다."""
    collector = make_collector(tmp_path, 4, parallel_collections=4)

    def run_batch(entries):
        # 뒤에 있는 통합기가 먼저 끝나게 만든다.
        time.sleep(0.05 * (4 - int(entries[0]["id"][-1])))
        return [{"id": e["id"], "name": e["name"], "status": "SUCCESS",
                 "records": [{"VM": e["id"]}], "row_count": 1,
                 "json_file": None, "xlsx_file": None} for e in entries]

    monkeypatch.setattr(collector, "run_batch", run_batch)
    _, metadata = collector.collect_all()
    assert [item["id"] for item in metadata["results"]] == [
        "vc_0001", "vc_0002", "vc_0003", "vc_0004",
    ]


def test_setting_one_keeps_the_old_sequential_behaviour(tmp_path, monkeypatch):
    collector = make_collector(tmp_path, 3, parallel_collections=1)
    run_batch, state = fake_run_batch(delay=0.05)
    monkeypatch.setattr(collector, "run_batch", run_batch)

    collector.collect_all()
    assert state["peak"] == 1
    assert len(state["calls"]) == 1, "순차면 프로세스 하나가 전부 맡는다"


def test_batching_can_be_turned_off(tmp_path, monkeypatch):
    """새 방식이 환경에 맞지 않으면 1대씩 따로 띄울 수 있어야 한다."""
    collector = make_collector(tmp_path, 4, parallel_collections=4, batch_collection=False)
    run_batch, state = fake_run_batch(delay=0.02)
    monkeypatch.setattr(collector, "run_batch", run_batch)

    collector.collect_all()
    assert state["sizes"] == [1, 1, 1, 1]


@pytest.mark.parametrize("configured,count,expected", [
    (4, 10, 4), (4, 2, 2), (0, 5, 1), (-3, 5, 1), ("bad", 5, 4), (None, 5, 4),
])
def test_the_parallel_limit_stays_sensible(tmp_path, configured, count, expected):
    """통합기 수보다 많이 띄울 이유가 없고, 잘못된 값으로 멈춰서도 안 된다."""
    collector = make_collector(tmp_path, 1, parallel_collections=configured)
    assert collector._parallel_limit(count) == expected


def test_one_vcenter_failing_does_not_stop_the_others(tmp_path, monkeypatch):
    collector = make_collector(tmp_path, 3, parallel_collections=3)

    def run_batch(entries):
        out = []
        for entry in entries:
            if entry["id"] == "vc_0002":
                out.append({"id": entry["id"], "name": entry["name"], "status": "FAILED",
                            "error": "vCenter 2 가 응답하지 않습니다"})
            else:
                out.append({"id": entry["id"], "name": entry["name"], "status": "SUCCESS",
                            "records": [{"VM": entry["id"]}], "row_count": 1,
                            "json_file": None, "xlsx_file": None})
        return out

    monkeypatch.setattr(collector, "run_batch", run_batch)
    records, metadata = collector.collect_all()

    assert len(records) == 2
    assert metadata["success_scopes"] == ["vc_0001", "vc_0003"]
    assert "vc_0002" in metadata["failed_scopes"]
    assert "응답하지 않습니다" in metadata["failed_scopes"]["vc_0002"]


def test_all_vcenters_failing_is_reported_as_an_error(tmp_path, monkeypatch):
    collector = make_collector(tmp_path, 2, parallel_collections=2)
    monkeypatch.setattr(
        collector, "run_batch",
        lambda entries: [{"id": e["id"], "name": e["name"], "status": "FAILED", "error": "실패"}
                         for e in entries],
    )
    with pytest.raises(PowerCLICollectionError, match="정상 수집된"):
        collector.collect_all()
