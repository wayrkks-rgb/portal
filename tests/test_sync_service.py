from datetime import datetime, timedelta
from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.sync_service import ChangeSyncService


def test_change_sync_cpu_values(tmp_path: Path) -> None:
    cfg=AppConfig(root_dir=tmp_path,sqlite_path=Path('db.sqlite'));cfg.matching={'sync_tolerance_days':1}
    manager=SQLiteManager(cfg.database_path);manager.initialize();now=datetime(2026,7,1,1)
    with manager.connect() as conn:
        repo=AssetRepository(conn)
        # latest reconciliation supplies rv asset key -> cm id identity
        run1=repo.start_collection_run('ITSM',now.isoformat());s1=repo.create_snapshot('ITSM','2026-07-01',now.isoformat(),run1,'SUCCESS',0,'x');repo.finish_collection_run(run1,'SUCCESS',0,now.isoformat(),['ALL'])
        run2=repo.start_collection_run('RVTOOLS',now.isoformat());s2=repo.create_snapshot('RVTOOLS','2026-07-01',now.isoformat(),run2,'SUCCESS',0,'y');repo.finish_collection_run(run2,'SUCCESS',0,now.isoformat(),['VC1'])
        repo.replace_reconciliation(s1,s2,[{'cm_id':'CM1','rv_asset_key':'uuid1','match_status':'MATCHED','match_method':'IDENTITY_MAP','score':100,'drifts':[],'reason':None,'created_at':now.isoformat()}])
        conn.execute("INSERT INTO change_event(source,snapshot_id,asset_key,event_type,field_name,old_value,new_value,detected_at,metadata_json) VALUES ('RVTOOLS',?,'uuid1','RV_CPU_CHANGED','cpus','4','8',?,'{}')",(s2,now.isoformat()))
        conn.execute("INSERT INTO change_event(source,snapshot_id,asset_key,event_type,field_name,old_value,new_value,detected_at,metadata_json) VALUES ('ITSM',?,'CM1','ITSM_CPU_CHANGED','CM_CPU_CORE_CNT','4','8',?,'{}')",(s1,(now+timedelta(hours=2)).isoformat()))
        result=ChangeSyncService(cfg,repo).evaluate(now.isoformat(),(now+timedelta(days=1)).isoformat())
        assert result['counts']['SYNCED']==1
