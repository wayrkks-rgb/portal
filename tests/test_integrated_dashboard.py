from __future__ import annotations

from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.collection_service import CollectionService
from asset_sync.services.exception_service import ReconciliationExceptionService
from asset_sync.services.integrated_dashboard_service import IntegratedDashboardService


def _config(tmp_path: Path) -> AppConfig:
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
            "resource_usage": {"enabled": False, "script_path": "scripts/VM_ResourceUsageExport.ps1"},
        },
        matching={"use_identity_map_first": True, "auto_remember_exact_match": True, "memory_tolerance_mb": 1},
        quality={"minimum_itsm_records": 1, "minimum_rvtools_records": 1, "eos_near_days": 180},
    )


def test_dashboard_and_exception_projection(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    manager = SQLiteManager(cfg.database_path)
    manager.initialize()
    result = CollectionService(cfg, manager).run_daily(demo=True)
    assert result["batch_id"] > 0
    assert result["resource_usage"]["status"] == "SUCCESS"
    assert result["resource_usage"]["host_count"] >= 1
    assert result["resource_usage"]["vm_count"] >= 1

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        dashboard = IntegratedDashboardService(repo).summary()
        assert dashboard["automation"]["status"] == "SUCCESS"
        assert dashboard["asset_status"]["total"] == 2
        assert dashboard["comparison"]["counts"]["RVTOOLS_ONLY"] == 1
        assert "vcenter_changes" in dashboard
        candidate = next(row for row in IntegratedDashboardService(repo).exception_candidates() if row["match_status"] == "RVTOOLS_ONLY")
        created = ReconciliationExceptionService(repo).create_many(
            [{
                "exception_type": "RVTOOLS_ONLY",
                "rv_asset_key": candidate["rv_asset_key"],
                "server_name": candidate["server_name"],
                "reason": "관리대상 외",
            }],
            "tester",
        )
        assert created["created_count"] == 1

    with manager.connect() as conn:
        dashboard = IntegratedDashboardService(AssetRepository(conn)).summary()
        assert dashboard["comparison"]["exception_count"] == 1
        assert "RVTOOLS_ONLY" not in dashboard["comparison"]["counts"]


def test_dashboard_exposes_vm_cpu_memory_and_host_changes(tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    from asset_sync.collectors.synthetic_collectors import SyntheticRVToolsCollector
    from asset_sync.services.diff_service import DiffService
    from asset_sync.services.snapshot_service import SnapshotService

    cfg = _config(tmp_path)
    manager = SQLiteManager(cfg.database_path)
    manager.initialize()
    CollectionService(cfg, manager).run_daily(demo=True)

    raw, metadata = SyntheticRVToolsCollector(cfg).collect()
    raw[0]["CPUs"] = 6
    raw[0]["Memory"] = 12288
    raw[0]["Host"] = "esxi-demo-002.example.invalid"
    collected_at = datetime.now() + timedelta(seconds=1)
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run_id = repo.start_collection_run("RVTOOLS", collected_at.isoformat())
        service = SnapshotService(cfg, repo)
        records, _ = service.normalize_rvtools(raw)
        snapshot_id = service.save_rv_snapshot(run_id, records, collected_at)
        diff = DiffService(cfg, repo).compare_rvtools(snapshot_id)
        repo.finish_collection_run(
            run_id, "SUCCESS", len(records), collected_at.isoformat(), metadata["success_scopes"], metadata=metadata
        )
        event_types = {event["event_type"] for event in diff["events"]}
        assert {"RV_CPU_CHANGED", "RV_MEMORY_CHANGED", "RV_HOST_CHANGED"}.issubset(event_types)

    with manager.connect() as conn:
        dashboard = IntegratedDashboardService(AssetRepository(conn)).summary()
        counts = dashboard["vcenter_changes"]["counts"]
        assert counts["CPU변경"] == 1
        assert counts["MEM변경"] == 1
        assert counts["통합기이동"] == 1
        row = next(item for item in dashboard["vcenter_changes"]["details"] if item["event_type"] == "RV_CPU_CHANGED")
        assert row["current_cpu_cores"] == 6
        assert row["current_memory_mb"] == 12288
