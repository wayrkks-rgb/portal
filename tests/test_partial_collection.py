"""통합기 일부가 실패해도 성공한 것들의 추가·삭제는 반영되어야 한다.

통합기 10 대 중 3 대가 연결에 실패하면 수집 건수는 당연히 줄어든다. 그걸 전체
건수와 견주면 임계값 미만으로 떨어져 '수집 이상' 으로 판정되고, 그러면 변경
이벤트 생성이 보류된다. 그 결과 **성공한 통합기의 추가·삭제까지** 화면에 안
나온다. 실제로 그랬다.

성공한 통합기끼리만 견주면 그런 일이 없다. 그 성질을 여기서 지킨다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.collection_service import CollectionService
from asset_sync.services.diff_service import DiffService

SCOPES = [f"vc{index:02d}" for index in range(10)]
PER_SCOPE = 20


def vm(scope: str, index: int, **overrides):
    key = f"{scope}-vm{index:03d}"
    record = {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}",
        "vcenter": scope, "vm_name": key, "dns_name": key, "normalized_hostname": key,
        "ip_addresses": ["10.0.0.5"], "primary_ip": "10.0.0.5", "cpus": 4, "memory_mb": 8192,
        "os_family": "Linux Redhat", "os_version": "8.6", "power_state": "poweredon",
        "datacenter": "DC1", "cluster": "CL1", "cluster_name": "CL1", "esxi_host": f"{scope}-esxi-01",
        "template_flag": False, "srm_placeholder": False, "raw": {"VM": key}, "record_hash": "h",
    }
    record.update(overrides)
    return record


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(
        root_dir=tmp_path, sqlite_path=Path("data/partial.db"),
        quality={"rvtools_count_warning_ratio": 0.70, "rvtools_count_critical_ratio": 0.30,
                 "minimum_rvtools_records": 1},
    )
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def seed(manager, day: datetime, records: list[dict], scopes: list[str], failed: list[str] | None = None):
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("RVTOOLS", day.isoformat())
        repo.finish_collection_run(
            run, "PARTIAL_SUCCESS" if failed else "SUCCESS", len(records),
            day.isoformat(), scopes, failed or [],
        )
        snapshot_id = repo.create_snapshot(
            "RVTOOLS", day.date().isoformat(), day.isoformat(), run,
            "PARTIAL_SUCCESS" if failed else "SUCCESS", len(records), "h",
        )
        repo.insert_rv_records(snapshot_id, records)
        conn.commit()
    return snapshot_id


def test_a_partial_failure_is_not_judged_against_the_whole_fleet(portal):
    """통합기 3 대가 빠져 건수가 30% 줄어도 '이상' 이 아니다."""
    config, manager = portal
    yesterday = datetime.now() - timedelta(days=1)
    full = [vm(scope, index) for scope in SCOPES for index in range(PER_SCOPE)]
    seed(manager, yesterday, full, SCOPES)

    # 오늘: 앞 7 대만 성공. 건수는 200 → 140 (70%).
    alive = SCOPES[:7]
    today_records = [vm(scope, index) for scope in alive for index in range(PER_SCOPE)]

    with manager.connect() as conn:
        service = CollectionService(config, manager)
        baseline = service._check_baseline(
            AssetRepository(conn), "RVTOOLS", len(today_records), scopes=alive
        )
    # 성공한 7 대끼리 견주면 140 대 140 이라 이상이 없다.
    assert baseline["previous_count"] == 7 * PER_SCOPE
    assert baseline["critical"] is False
    assert baseline["warning"] is False
    assert baseline["compared_scopes"] == sorted(alive)

    # 예전처럼 전체와 견주면 140/200 = 70% 로 경고, 더 많이 실패하면 이상이 된다.
    with manager.connect() as conn:
        whole = service._check_baseline(AssetRepository(conn), "RVTOOLS", len(today_records))
    assert whole["previous_count"] == len(SCOPES) * PER_SCOPE


def test_half_the_fleet_failing_no_longer_suppresses_the_diff(portal):
    """통합기 절반이 실패하면 예전에는 '이상' 으로 보고 변경을 만들지 않았다."""
    config, manager = portal
    yesterday = datetime.now() - timedelta(days=1)
    full = [vm(scope, index) for scope in SCOPES for index in range(PER_SCOPE)]
    seed(manager, yesterday, full, SCOPES)

    alive = SCOPES[:4]                       # 200 → 80 (40%)
    today_records = [vm(scope, index) for scope in alive for index in range(PER_SCOPE)]
    service = CollectionService(config, manager)

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        whole = service._check_baseline(repo, "RVTOOLS", len(today_records))
        scoped = service._check_baseline(repo, "RVTOOLS", len(today_records), scopes=alive)
    assert whole["warning"] is True, "전체와 견주면 경고가 뜬다"
    assert scoped["warning"] is False, "성공한 통합기끼리는 줄지 않았다"
    assert scoped["critical"] is False


def test_additions_and_deletions_in_the_live_scopes_still_show_up(portal):
    """성공한 통합기에서 생긴 추가·삭제는 그대로 잡혀야 한다."""
    config, manager = portal
    yesterday = datetime.now() - timedelta(days=1)
    full = [vm(scope, index) for scope in SCOPES for index in range(PER_SCOPE)]
    seed(manager, yesterday, full, SCOPES)

    alive = SCOPES[:7]
    today_records = [vm(scope, index) for scope in alive for index in range(PER_SCOPE)]
    # vc00 에서 1 대 지우고 1 대 새로 만든다.
    today_records = [r for r in today_records if r["asset_key"] != "vc00-vm000"]
    today_records.append(vm("vc00", 999))
    today_id = seed(manager, datetime.now(), today_records, alive, failed=SCOPES[7:])

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        result = DiffService(config, repo).compare_rvtools(today_id)
        conn.commit()

    kinds = {}
    for event in result["events"]:
        kinds.setdefault(event["event_type"], []).append(event["asset_key"])

    # 성공한 통합기의 추가·삭제는 잡힌다.
    assert kinds.get("RV_NEW") == ["vc00-vm999"]
    assert kinds.get("RV_REMOVED") == ["vc00-vm000"]
    # 실패한 통합기의 VM 은 삭제로 세지 않는다. 지워진 게 아니라 못 읽은 것이다.
    gaps = kinds.get("COLLECTION_GAP") or []
    assert len(gaps) == 3 * PER_SCOPE
    assert all(key.startswith(("vc07", "vc08", "vc09")) for key in gaps)


def test_the_change_events_are_saved_so_screens_need_no_manual_refresh(portal):
    """배치가 저장해 두어야 화면을 열 때마다 다시 계산하지 않는다."""
    config, manager = portal
    yesterday = datetime.now() - timedelta(days=1)
    seed(manager, yesterday, [vm("vc00", index) for index in range(PER_SCOPE)], ["vc00"])

    today = [vm("vc00", index) for index in range(PER_SCOPE)]
    today.append(vm("vc00", 500))
    today_id = seed(manager, datetime.now(), today, ["vc00"])

    with manager.connect() as conn:
        DiffService(config, AssetRepository(conn)).compare_rvtools(today_id)
        conn.commit()

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        stored = repo.changes(source="RVTOOLS", limit=100)
    assert any(event["event_type"] == "RV_NEW" for event in stored), "저장된 이벤트가 없습니다"


# ── 화면이 '왜 적은지' 알 수 있어야 한다 ─────────────────────────────────


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    yield create_app()
    reset_database_manager()


def test_the_screens_can_see_which_scopes_failed(web):
    """통합기 3 대가 빠졌으면 화면이 그 이름을 받아야 한다.

    이걸 안 알려주면 사람은 줄어든 대수를 보고 '삭제됐다' 고 읽는다.
    """
    from asset_sync.db.manager import create_manager
    from asset_sync.config import load_config

    config = load_config()
    manager = create_manager(config)
    now = datetime.now()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("RVTOOLS", now.isoformat())
        repo.finish_collection_run(run, "PARTIAL_SUCCESS", 140, now.isoformat(), SCOPES[:7], SCOPES[7:])
        batch = repo.start_daily_batch(now.date().isoformat(), now.isoformat())
        repo.finish_daily_batch(
            batch, status="PARTIAL_SUCCESS", ended_at=now.isoformat(),
            itsm_run_id=None, vcenter_run_id=run,
            metadata={"status_reasons": [
                {"area": "vCenter", "code": "SCOPE_FAILED", "message": "통합기 3개 연결 실패"},
            ]},
        )
        conn.commit()

    client = web.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    payload = client.get("/api/asset-sync/collection-health").get_json()

    assert payload["status"] == "PARTIAL_SUCCESS"
    assert payload["scopes"]["failed"] == SCOPES[7:]
    assert len(payload["scopes"]["success"]) == 7
    assert any(item["code"] == "SCOPE_FAILED" for item in payload["reasons"])


def test_the_three_check_screens_show_the_notice() -> None:
    """일간·주간·월간 모두에 떠야 한다. 한 군데만 띄우면 나머지에서 또 오해한다."""
    root = Path(__file__).resolve().parents[1] / "templates"
    screens = {
        "daily-collection-notice": ("pages/daily_check.html", "partials/js/checks.html"),
        "weekly-collection-notice": ("pages/weekly_check.html", "partials/js/checks.html"),
        "monthly-collection-notice": ("pages/monthly_check.html", "partials/js/monthly_check.html"),
    }
    for element_id, (page, script) in screens.items():
        assert f'id="{element_id}"' in (root / page).read_text(encoding="utf-8"), f"{page} 에 자리가 없다"
        text = (root / script).read_text(encoding="utf-8")
        assert f"showCollectionNotice('{element_id}'" in text, f"{script} 가 알림을 안 부른다"
    common = (root / "partials/js/common.html").read_text(encoding="utf-8")
    assert "/api/asset-sync/collection-health" in common
    assert "수집공백" in common, "삭제가 아니라 수집공백이라는 설명이 있어야 한다"
