from datetime import datetime, timedelta
from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.diff_service import DiffService
from asset_sync.services.snapshot_service import SnapshotService


def config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(root_dir=tmp_path, sqlite_path=Path("db.sqlite"))
    cfg.quality = {"minimum_itsm_records": 1, "minimum_rvtools_records": 1}
    cfg.itsm = {
        "memory_unit": "GB", "os_eos_field": "OS_EOS_DATE",
        "tracked_fields": ["CM_HOSTNAME", "CM_IP", "CM_CPU_CORE_CNT", "CM_MEMORY", "CM_STA_CD"],
        "ignore_fields": [],
    }
    cfg.rvtools = {"hostname_suffixes": [".korealife.dom"], "power_on_value": "poweredon"}
    return cfg


def test_itsm_status_and_field_changes(tmp_path: Path) -> None:
    cfg = config(tmp_path); manager = SQLiteManager(cfg.database_path); manager.initialize()
    t1 = datetime(2026, 7, 1, 1); t2 = t1 + timedelta(days=1)
    old = [{"CM_ID":"CM0001-1","CM_HOSTNAME":"APP01","CM_IP":"10.0.0.1","CM_CPU_CORE_CNT":4,"CM_MEMORY":16,"CM_STA_CD":"CMSTA010","CM_SVR_CAT_CD":"CMSVRCATCD020","CM_OS":"CMCIOSCD010"}]
    new = [{"CM_ID":"CM0001-1","CM_HOSTNAME":"APP01","CM_IP":"10.0.0.2","CM_CPU_CORE_CNT":8,"CM_MEMORY":16,"CM_STA_CD":"CMSTA020","CM_SVR_CAT_CD":"CMSVRCATCD020","CM_OS":"CMCIOSCD010"}]
    with manager.connect() as conn:
        repo=AssetRepository(conn); svc=SnapshotService(cfg,repo)
        r1=repo.start_collection_run("ITSM",t1.isoformat()); n1,_=svc.normalize_itsm(old); s1=svc.save_itsm_snapshot(r1,n1,t1); repo.finish_collection_run(r1,"SUCCESS",1,t1.isoformat(),["ALL"])
        r2=repo.start_collection_run("ITSM",t2.isoformat()); n2,_=svc.normalize_itsm(new); s2=svc.save_itsm_snapshot(r2,n2,t2); repo.finish_collection_run(r2,"SUCCESS",1,t2.isoformat(),["ALL"])
        result=DiffService(cfg,repo).compare_itsm(s2)
        types={e["event_type"] for e in result["events"]}
        assert "ITSM_STATUS_TO_UNUSED" in types
        assert "ITSM_IP_CHANGED" in types
        assert "ITSM_CPU_CHANGED" in types
