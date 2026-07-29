from datetime import datetime
from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.reconciliation_service import ReconciliationService
from asset_sync.services.snapshot_service import SnapshotService


def test_ip_hostname_matching_and_memory_conversion(tmp_path: Path) -> None:
    cfg=AppConfig(root_dir=tmp_path,sqlite_path=Path("db.sqlite"))
    cfg.quality={"minimum_itsm_records":1,"minimum_rvtools_records":1}
    cfg.itsm={"memory_unit":"GB","os_eos_field":"OS_EOS_DATE","tracked_fields":[],"ignore_fields":[]}
    cfg.rvtools={"hostname_suffixes":[".korealife.dom"],"power_on_value":"poweredon","exclude_templates":True,"exclude_srm_placeholders":True}
    cfg.matching={"memory_tolerance_mb":1,"allow_vm_name_auto_match":False}
    manager=SQLiteManager(cfg.database_path);manager.initialize();now=datetime(2026,7,1,1)
    itsm=[{"CM_ID":"CM0001-1","CM_HOSTNAME":"APP01.KOREALIFE.DOM","CM_IP":"10.0.0.1","CM_CPU_CORE_CNT":4,"CM_MEMORY":16,"CM_STA_CD":"CMSTA010","CM_SVR_CAT_CD":"CMSVRCATCD020","CM_OS":"CMCIOSCD010","CM_OS_VERSION":"8.10"}]
    rv=[{"VM":"APP01","Powerstate":"poweredOn","DNS Name":"app01.korealife.dom","Primary IP Address":"10.0.0.1","CPUs":4,"Memory":16384,"OS according to the VMware Tools":"Red Hat Enterprise Linux 8.10","VM UUID":"UUID-1","VI SDK Server":"VC01"}]
    with manager.connect() as conn:
        repo=AssetRepository(conn);svc=SnapshotService(cfg,repo)
        ri=repo.start_collection_run("ITSM",now.isoformat());ni,_=svc.normalize_itsm(itsm);si=svc.save_itsm_snapshot(ri,ni,now);repo.finish_collection_run(ri,"SUCCESS",1,now.isoformat(),["ALL"])
        rr=repo.start_collection_run("RVTOOLS",now.isoformat());nr,_=svc.normalize_rvtools(rv);sr=svc.save_rv_snapshot(rr,nr,now);repo.finish_collection_run(rr,"SUCCESS",1,now.isoformat(),["VC01"])
        result=ReconciliationService(cfg,repo).reconcile(si,sr)
        assert result["counts"]["MATCHED"] == 1
        assert repo.identity_maps()["CM0001-1"] == "uuid-1"
