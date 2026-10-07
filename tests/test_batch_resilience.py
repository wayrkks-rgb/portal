"""부분 성공은 실패가 아니고, 통합기를 붙여도 배치를 다시 등록할 필요가 없다.

두 가지를 글자로 못 박는다.

1. **PARTIAL_SUCCESS 는 계속 진행한다.** 통합기 몇 대가 연결에 실패해도 성공한
   통합기의 추가·삭제는 반영되고, 정합성과 자원사용률 수집도 돈다. 윈도 작업
   스케줄러가 '실패' 로 표시하지 않도록 종료코드도 0 이어야 한다.
2. **통합기 등록은 매 실행마다 다시 읽는다.** ``config/vcenters.local.yaml`` 을
   배치가 돌 때마다 읽으므로, 통합기를 추가해도 BAT 재등록이 필요 없다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from asset_sync.config import AppConfig, load_config
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.collection_service import CollectionService

ROOT = Path(__file__).resolve().parents[1]


# ── 1. 부분 성공은 계속 진행한다 ────────────────────────────────────────


def vm(scope: str, index: int) -> dict:
    key = f"{scope}-vm{index:03d}"
    return {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}",
        "vcenter": scope, "vm_name": key, "dns_name": key, "normalized_hostname": key,
        "ip_addresses": ["10.0.0.5"], "primary_ip": "10.0.0.5", "cpus": 4, "memory_mb": 8192,
        "os_family": "Linux Redhat", "os_version": "8.6", "power_state": "poweredon",
        "datacenter": "DC1", "cluster": "CL1", "cluster_name": "CL1",
        "esxi_host": f"{scope}-esxi-01", "template_flag": False, "srm_placeholder": False,
        "raw": {"VM": key}, "record_hash": "h",
    }


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(
        root_dir=tmp_path, sqlite_path=Path("data/resilience.db"),
        quality={"rvtools_count_warning_ratio": 0.70, "rvtools_count_critical_ratio": 0.30,
                 "minimum_rvtools_records": 1},
    )
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def test_a_partial_failure_does_not_block_the_change_events(portal) -> None:
    """통합기 3대가 빠져도 성공한 7대의 추가·삭제는 만들어져야 한다."""
    config, manager = portal
    scopes = [f"vc{i:02d}" for i in range(10)]
    yesterday = datetime.now() - timedelta(days=1)
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("RVTOOLS", yesterday.isoformat())
        records = [vm(s, i) for s in scopes for i in range(20)]
        repo.finish_collection_run(run, "SUCCESS", len(records), yesterday.isoformat(), scopes, [])
        snapshot = repo.create_snapshot(
            "RVTOOLS", yesterday.date().isoformat(), yesterday.isoformat(), run,
            "SUCCESS", len(records), "h",
        )
        repo.insert_rv_records(snapshot, records)
        conn.commit()

    alive = scopes[:7]
    service = CollectionService(config, manager)
    with manager.connect() as conn:
        baseline = service._check_baseline(AssetRepository(conn), "RVTOOLS", 7 * 20, scopes=alive)
    # 성공한 통합기끼리는 줄지 않았으므로 critical 이 아니다 -- critical 이면
    # 변경 이벤트 생성이 보류되고 배치도 FAILED 로 떨어진다.
    assert baseline["critical"] is False
    assert baseline["warning"] is False


def test_the_batch_status_is_partial_not_failed(portal, monkeypatch) -> None:
    """부분 실패는 PARTIAL_SUCCESS 다. FAILED 면 뒤 단계가 전부 건너뛰어진다."""
    config, manager = portal
    service = CollectionService(config, manager)

    results = {
        "vcenter": {"status": "PARTIAL_SUCCESS", "snapshot_id": 1, "count": 140,
                    "failed_scopes": {"vc07": "연결 실패", "vc08": "연결 실패"},
                    "baseline": {"warning": False, "critical": False}},
        "itsm": {"status": "SUCCESS", "snapshot_id": 2, "count": 100, "baseline": {}},
        "reconciliation": {"status": "SUCCESS", "counts": {}},
    }
    reasons = service._status_reasons(results, {"status": "SUCCESS"})
    codes = {item["code"] for item in reasons}
    assert "SCOPE_FAILED" in codes, "어느 통합기가 실패했는지 남아야 한다"
    assert "COLLECTION_FAILED" not in codes, "부분 실패를 수집 실패로 적으면 안 된다"


def test_the_job_exits_zero_on_partial_success() -> None:
    """종료코드가 0 이 아니면 윈도 작업 스케줄러가 '실패' 로 표시한다.

    그러면 사람이 배치가 안 돈 줄 알고 설정을 다시 만지게 된다. 돌았고 일부만
    모자란 것이므로 0 이어야 하고, FAILED 일 때만 0 이 아니어야 한다.
    """
    lines = (ROOT / "jobs" / "daily_batch.py").read_text(encoding="utf-8").splitlines()
    nonzero = [index for index, line in enumerate(lines) if "SystemExit(" in line
               and "SystemExit(0)" not in line]
    assert len(nonzero) == 1, f"0 이 아닌 종료가 여러 곳입니다: {[lines[i].strip() for i in nonzero]}"
    # 그 한 곳은 FAILED 일 때만 걸린다.
    guard = next(line for line in reversed(lines[:nonzero[0]]) if line.strip().startswith("if "))
    assert 'status") == "FAILED"' in guard, f"FAILED 가 아닌 조건으로 실패 처리합니다: {guard.strip()}"


# ── 2. 통합기를 붙여도 배치 재등록이 필요 없다 ──────────────────────────


def test_a_new_vcenter_is_picked_up_without_touching_the_batch(tmp_path: Path, monkeypatch) -> None:
    """BAT 은 매 실행마다 설정 파일을 다시 읽는다. 그래서 재등록이 필요 없다."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "vcenter:\n  collection_mode: POWERCLI\n", encoding="utf-8"
    )
    registrations = tmp_path / "config" / "vcenters.local.yaml"
    registrations.write_text(
        yaml.safe_dump({"vcenters": [{"id": "vc01", "name": "통합기1", "host": "vc01.example"}]},
                       allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    first = load_config()
    assert [item["id"] for item in first.rvtools["vcenters"]] == ["vc01"]

    # 화면에서 통합기를 하나 더 붙였다. 파일만 바뀌고 BAT 은 그대로다.
    registrations.write_text(
        yaml.safe_dump({"vcenters": [
            {"id": "vc01", "name": "통합기1", "host": "vc01.example"},
            {"id": "vc02", "name": "통합기2", "host": "vc02.example"},
        ]}, allow_unicode=True),
        encoding="utf-8",
    )

    # 다음 실행이 부르는 바로 그 함수. 다시 읽어야 새 통합기가 보인다.
    again = load_config()
    assert [item["id"] for item in again.rvtools["vcenters"]] == ["vc01", "vc02"], (
        "설정을 다시 읽지 않으면 새로 붙인 통합기를 배치가 못 본다"
    )


def test_the_batch_entry_point_reloads_the_config_every_run() -> None:
    """``load_config()`` 을 모듈 가져올 때 한 번만 하면 재등록이 필요해진다."""
    source = (ROOT / "jobs" / "daily_batch.py").read_text(encoding="utf-8")
    body = source.split("def main()", 1)[1]
    assert "load_config()" in body, "main() 안에서 설정을 읽어야 매 실행마다 새로 읽힌다"
    # 수동 수집(화면) 쪽도 같아야 한다. 한쪽만 다시 읽으면 숫자가 어긋난다.
    routes = (ROOT / "asset_sync" / "routes" / "collection.py").read_text(encoding="utf-8")
    service = routes.split("def _service()", 1)[1].split("\ndef ", 1)[0]
    assert "load_config()" in service


def test_the_job_runs_end_to_end_in_demo_mode(tmp_path: Path) -> None:
    """실제로 돌려 본다. 종료코드와 요약 JSON 이 나와야 한다."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\nvcenter:\n  collection_mode: DEMO\n", encoding="utf-8"
    )
    env = {
        "ASSET_APP_ROOT": str(tmp_path),
        "FLASK_SECRET_KEY": "test-secret",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.run(
        [sys.executable, str(ROOT / "jobs" / "daily_batch.py"), "--demo"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = json.loads([line for line in proc.stdout.splitlines() if line.startswith("{")][0])
    assert payload["status"] in {"SUCCESS", "PARTIAL_SUCCESS"}, payload
    # 부분 성공이어도 뒤 단계가 돌아야 한다.
    assert payload["reconciliation"]["status"] != "SKIPPED", payload["reconciliation"]
