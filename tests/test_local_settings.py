from __future__ import annotations

from pathlib import Path

from asset_sync.config import load_config
from asset_sync.settings_store import LocalSettingsStore


def test_local_settings_are_loaded_without_password_exposure(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\nvcenter:\n  collection_mode: FILE_ONLY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    store = LocalSettingsStore(tmp_path)
    saved = store.save(
        {
            "oracle": {
                "enabled": True, "mode": "thin", "host": "db.example.invalid", "port": "1521",
                "service_name": "DEMO", "user": "user001", "password": "secret-value",
                "asset_source": "V_ASSET_DEMO", "query_file": "config/oracle_query.local.sql",
            },
            "itsm": {
                "collection_mode": "ORACLE", "incoming_dir": "data/incoming/itsm", "sheet_name": "Sheet1",
                "header_row": 1, "cpu_compare_field": "CM_CPU_CORE_CNT", "memory_field": "CM_MEMORY",
                "memory_unit": "GB", "os_eos_field": "OS_EOS_DATE",
            },
            "vcenter": {
                "collection_mode": "FILE_ONLY", "powershell_path": "powershell.exe",
                "script_path": "scripts/collect_vcenter_inventory.ps1",
                "incoming_dir": "data/incoming/vcenter", "snapshot_dir": "data/archive/vcenter",
                "temp_dir": "data/temp/powercli", "export_xlsx": True,
                "hostname_suffixes": [".example.invalid"], "timeout_seconds": 1800,
                "retry_count": 1, "vcenters": [],
            },
            "quality": {}, "security": {}, "scheduler": {"daily_time": "07:00", "task_name": "DemoTask"},
        }
    )
    assert saved["oracle"]["password_configured"] is True
    assert "password" not in saved["oracle"]
    cfg = load_config()
    assert cfg.oracle["password"] == "secret-value"
    assert cfg.itsm["cpu_compare_field"] == "CM_CPU_CORE_CNT"
