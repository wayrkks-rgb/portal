from datetime import datetime, timedelta
from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.diff_service import DiffService
from asset_sync.services.snapshot_service import SnapshotService


def test_partial_vcenter_failure_does_not_create_removed(tmp_path: Path) -> None:
    cfg=AppConfig(root_dir=tmp_path,sqlite_path=Path("db.sqlite"));cfg.quality={"minimum_rvtools_records":1};cfg.rvtools={"hostname_suffixes":[]}
    manager=SQLiteManager(cfg.database_path);manager.initialize();t1=datetime(2026,7,1);t2=t1+timedelta(days=1)
    old=[{"VM":"A","Powerstate":"poweredOn","CPUs":2,"Memory":2048,"VM UUID":"A","VI SDK Server":"VC1"},{"VM":"B","Powerstate":"poweredOn","CPUs":2,"Memory":2048,"VM UUID":"B","VI SDK Server":"VC2"}]
    new=[{"VM":"A","Powerstate":"poweredOn","CPUs":2,"Memory":2048,"VM UUID":"A","VI SDK Server":"VC1"}]
    with manager.connect() as conn:
        repo=AssetRepository(conn);svc=SnapshotService(cfg,repo)
        r1=repo.start_collection_run("RVTOOLS",t1.isoformat());n1,_=svc.normalize_rvtools(old);s1=svc.save_rv_snapshot(r1,n1,t1);repo.finish_collection_run(r1,"SUCCESS",2,t1.isoformat(),["VC1","VC2"])
        r2=repo.start_collection_run("RVTOOLS",t2.isoformat());n2,_=svc.normalize_rvtools(new);s2=svc.save_rv_snapshot(r2,n2,t2,"PARTIAL_SUCCESS");repo.finish_collection_run(r2,"PARTIAL_SUCCESS",1,t2.isoformat(),["VC1"],["VC2"])
        events=DiffService(cfg,repo).compare_rvtools(s2)["events"]
        assert any(e["event_type"]=="COLLECTION_GAP" and e["asset_key"]=="b" for e in events)
        assert not any(e["event_type"]=="RV_REMOVED" and e["asset_key"]=="b" for e in events)
