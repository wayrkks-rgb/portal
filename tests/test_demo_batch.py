from __future__ import annotations

from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.services.collection_service import CollectionService


def test_demo_batch_runs_without_oracle_or_rvtools(tmp_path: Path) -> None:
    cfg = AppConfig(
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
    manager = SQLiteManager(cfg.database_path)
    manager.initialize()
    result = CollectionService(cfg, manager).run_daily(demo=True)
    assert result["status"] == "SUCCESS"
    assert result["vcenter"]["count"] == 3
    assert result["itsm"]["count"] == 3
    assert result["reconciliation"]["counts"]["MATCHED"] == 2
