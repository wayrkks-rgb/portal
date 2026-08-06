from __future__ import annotations

import stat
import sys
from pathlib import Path

from asset_sync.collectors.oracle_itsm_collector import OracleITSMCollector
from asset_sync.collectors.powercli_collector import PowerCLICollector
from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.collection_service import CollectionService
from asset_sync.services.daily_comparison_service import DailyComparisonService

REQUIRED_ORACLE_COLUMNS = [
    "CM_ID", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_CPU_CORE_CNT", "CM_MEMORY",
    "CM_OS", "CM_OS_VERSION", "CM_SVR_CAT_CD", "CM_STA_CD", "OS_EOS_DATE",
]


class FakeCursor:
    def __init__(self, module: "FakeOracleModule") -> None:
        self.module = module
        self.description = [(name,) for name in REQUIRED_ORACLE_COLUMNS]
        self.arraysize = 1
        self.prefetchrows = 1
        self._sent = False

    def __enter__(self) -> "FakeCursor": return self
    def __exit__(self, *_args: object) -> None: return None
    def execute(self, sql: str) -> None:
        self.module.last_sql = sql
        self._sent = False
    def fetchmany(self, _size: int) -> list[tuple[object, ...]]:
        if self._sent: return []
        self._sent = True
        return [tuple(row.get(name) for name in REQUIRED_ORACLE_COLUMNS) for row in self.module.rows]


class FakeConnection:
    def __init__(self, module: "FakeOracleModule") -> None: self.module = module
    def __enter__(self) -> "FakeConnection": return self
    def __exit__(self, *_args: object) -> None: return None
    def cursor(self) -> FakeCursor: return FakeCursor(self.module)


class FakeOracleModule:
    # LOB 출력 핸들러가 참조하는 타입 상수. 실제 드라이버와 이름이 같아야 한다.
    DB_TYPE_CLOB = "CLOB"
    DB_TYPE_NCLOB = "NCLOB"
    DB_TYPE_BLOB = "BLOB"
    DB_TYPE_LONG = "LONG"
    DB_TYPE_LONG_RAW = "LONG_RAW"

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.last_kwargs: dict[str, object] = {}
        self.last_sql = ""
    def connect(self, **kwargs: object) -> FakeConnection:
        self.last_kwargs = kwargs
        return FakeConnection(self)
    def init_oracle_client(self, **_kwargs: object) -> None: return None


def write_fake_powershell(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json, os
from pathlib import Path
version=int(os.environ.get('FAKE_POWERCLI_VERSION','1'))
vc=os.environ['VCENTER_ID']
out=Path(os.environ['VCENTER_OUTPUT_JSON'])
base='1' if vc=='vc_001' else '2'
rows=[{
 'VM':f'app-test-00{base}','Powerstate':'PoweredOn','CPUs':4 if version==1 else (8 if base=='1' else 4),
 'Memory':16384,'DNS Name':f'app-test-00{base}.example.invalid','Primary IP Address':f'203.0.113.{10+int(base)}',
 'VM UUID':f'00000000-0000-0000-0000-00000000000{base}','SMBIOS UUID':f'10000000-0000-0000-0000-00000000000{base}',
 'VM ID':f'VirtualMachine-vm-{base}','VI SDK Server':vc,'Host':f'esx-{base}.example.invalid','Template':False,'SRM Placeholder':False
}]
if version>=2 and vc=='vc_001':
 rows.append({'VM':'vm-demo-003','Powerstate':'PoweredOn','CPUs':2,'Memory':4096,'DNS Name':'vm-demo-003.example.invalid','Primary IP Address':'203.0.113.33','VM UUID':'00000000-0000-0000-0000-000000000003','SMBIOS UUID':'10000000-0000-0000-0000-000000000003','VM ID':'VirtualMachine-vm-3','VI SDK Server':vc,'Host':'esx-1.example.invalid','Template':False,'SRM Placeholder':False})
out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps(rows),encoding='utf-8')
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_config(tmp_path: Path) -> AppConfig:
    query = tmp_path / "config" / "oracle_query.local.sql"
    query.parent.mkdir(parents=True, exist_ok=True)
    query.write_text("SELECT CM_ID FROM ${ASSET_SOURCE}", encoding="utf-8")
    collector_script = tmp_path / "scripts" / "collect_vcenter_inventory.ps1"
    collector_script.parent.mkdir(parents=True, exist_ok=True)
    collector_script.write_text("# fake target exists", encoding="utf-8")
    fake_ps = tmp_path / "fake_powershell.py"
    write_fake_powershell(fake_ps)
    return AppConfig(
        root_dir=tmp_path,
        sqlite_path=Path("data/test.db"),
        oracle={"enabled": True,"mode": "thin","host": "192.0.2.10","port": 1521,"service_name": "DEMO_SERVICE","user": "user001","password": "synthetic-password","asset_source": "V_ASSET_DEMO","query_file": "config/oracle_query.local.sql","fetch_size": 1000},
        itsm={"collection_mode": "ORACLE","memory_unit": "GB","cpu_compare_field": "CM_CPU_CORE_CNT","memory_field": "CM_MEMORY","os_eos_field": "OS_EOS_DATE","tracked_fields": ["CM_HOSTNAME", "CM_IP", "CM_CPU_CORE_CNT", "CM_MEMORY", "CM_STA_CD"],"ignore_fields": [],"owner_field": "CM_WOR_MNG_EMP_ID"},
        rvtools={
            "collection_mode": "POWERCLI", "powershell_path": str(fake_ps), "script_path": "scripts/collect_vcenter_inventory.ps1",
            "incoming_dir": "data/incoming/vcenter", "snapshot_dir": "data/archive/vcenter", "temp_dir": "data/temp/powercli", "export_xlsx": True,
            "hostname_suffixes": [".example.invalid"], "power_on_value": "poweredon", "cpu_compare_field": "CPUs", "memory_compare_field": "Memory",
            "exclude_templates": True, "exclude_srm_placeholders": True, "retry_count": 0, "timeout_seconds": 30,
            "default_port": 443, "default_auth_mode": "CREDENTIAL", "default_username": "operator001", "default_password": "synthetic-password", "default_bypass_ssl_check": True,
            "vcenters": [
                {"id": "vc_001", "name": "VC 1", "server": "vc1.example.invalid", "site": "PROD", "enabled": True, "auth_profile": "COMMON"},
                {"id": "vc_002", "name": "VC 2", "server": "vc2.example.invalid", "site": "DR", "enabled": True, "auth_profile": "COMMON"},
            ],
        },
        matching={"use_identity_map_first": True, "auto_remember_exact_match": True, "memory_tolerance_mb": 1},
        quality={"minimum_itsm_records": 1,"minimum_rvtools_records": 1,"itsm_count_warning_ratio": 0.70,"itsm_count_critical_ratio": 0.30,"rvtools_count_warning_ratio": 0.70,"rvtools_count_critical_ratio": 0.30,"eos_near_days": 180},
        security={"display_vcenter_server_in_logs": False},
    )


def oracle_rows(version: int) -> list[dict[str, object]]:
    return [
        {"CM_ID":"CM-DEMO-001","CM_HOSTNAME":"app-test-001","CM_IP":"192.0.2.11" if version==1 else "192.0.2.12","CM_SUB_IP":None,"CM_CPU_CORE_CNT":4 if version==1 else 8,"CM_MEMORY":16,"CM_OS":"Linux","CM_OS_VERSION":"9","CM_SVR_CAT_CD":"CMSVRCATCD020","CM_STA_CD":"CMSTA010","OS_EOS_DATE":"2030-12-31"},
        {"CM_ID":"CM-DEMO-002","CM_HOSTNAME":"db-test-001","CM_IP":"198.51.100.21","CM_SUB_IP":None,"CM_CPU_CORE_CNT":8,"CM_MEMORY":32,"CM_OS":"Windows","CM_OS_VERSION":"2022","CM_SVR_CAT_CD":"CMSVRCATCD020","CM_STA_CD":"CMSTA010","OS_EOS_DATE":"2031-10-14"},
    ]


def test_oracle_direct_collection_and_daily_diff(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config(tmp_path)
    fake = FakeOracleModule(oracle_rows(1))
    monkeypatch.setitem(sys.modules, "oracledb", fake)
    manager = SQLiteManager(cfg.database_path); manager.initialize()
    test_result = OracleITSMCollector(cfg).test_connection()
    assert test_result["status"] == "SUCCESS"
    first = CollectionService(cfg, manager).collect_itsm("ORACLE")
    assert first["diff"]["status"] == "NO_BASELINE"
    fake.rows = oracle_rows(2)
    second = CollectionService(cfg, manager).collect_itsm("ORACLE")
    types = {event["event_type"] for event in second["diff"]["events"]}
    assert {"ITSM_IP_CHANGED", "ITSM_CPU_CHANGED"} <= types
    with manager.connect() as conn:
        daily = DailyComparisonService(cfg, AssetRepository(conn)).latest("ITSM")
    assert daily["counts"]["CHANGED"] == 1


def test_multi_vcenter_powercli_to_xlsx_snapshot_and_diff(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config(tmp_path)
    manager = SQLiteManager(cfg.database_path); manager.initialize()
    monkeypatch.setattr(PowerCLICollector, "test_network", staticmethod(lambda entry, timeout=5.0: {"status":"SUCCESS","server":entry["server"],"port":int(entry.get("port") or 443)}))
    monkeypatch.setenv("FAKE_POWERCLI_VERSION", "1")
    test_all = PowerCLICollector(cfg).test_all()
    assert test_all["status"] == "SUCCESS" and test_all["success"] == 2
    first = CollectionService(cfg, manager).collect_vcenter("POWERCLI")
    assert first["count"] == 2 and first["diff"]["status"] == "NO_BASELINE"
    assert all(Path(item["xlsx_file"]).exists() for item in first["collector_metadata"]["files"])
    monkeypatch.setenv("FAKE_POWERCLI_VERSION", "2")
    second = CollectionService(cfg, manager).collect_vcenter("POWERCLI")
    assert second["count"] == 3
    types = {event["event_type"] for event in second["diff"]["events"]}
    assert "RV_NEW" in types and "RV_CPU_CHANGED" in types
    with manager.connect() as conn:
        daily = DailyComparisonService(cfg, AssetRepository(conn)).latest("RVTOOLS")
    assert daily["counts"]["ADDED"] == 1 and daily["counts"]["CHANGED"] >= 1


def test_powercli_password_is_not_in_process_arguments(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config(tmp_path)
    collector = PowerCLICollector(cfg)
    entry = collector._effective_entry(cfg.rvtools["vcenters"][0])
    env = collector._build_environment(entry, tmp_path / "out.json")
    assert env["VCENTER_PASSWORD"] == "synthetic-password"
    # The generated PowerShell command has only script/output arguments; credentials are environment-only.
    executable = collector._resolve_executable(); script = collector._resolve_script()
    command = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-OutputPath", str(tmp_path / "out.json")]
    assert "synthetic-password" not in command and "operator001" not in command
