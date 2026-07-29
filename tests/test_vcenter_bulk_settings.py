from __future__ import annotations

from pathlib import Path

from asset_sync.collectors.powercli_collector import PowerCLICollector
from asset_sync.config import load_config
from asset_sync.settings_store import LocalSettingsStore


def test_common_vcenter_profile_is_saved_and_resolved(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    store = LocalSettingsStore(tmp_path)
    saved = store.save({
        "oracle": {"enabled": False, "mode": "thin"},
        "itsm": {"collection_mode": "DEMO", "cpu_compare_field": "CM_CPU_CORE_CNT", "memory_field": "CM_MEMORY", "memory_unit": "GB", "os_eos_field": "OS_EOS_DATE"},
        "vcenter": {
            "collection_mode": "POWERCLI", "powershell_path": "powershell.exe", "script_path": "scripts/collect_vcenter_inventory.ps1",
            "incoming_dir": "data/incoming/vcenter", "snapshot_dir": "data/archive/vcenter", "temp_dir": "data/temp/powercli", "export_xlsx": True,
            "hostname_suffixes": [".example.invalid"], "timeout_seconds": 1800, "retry_count": 1, "default_port": 443,
            "default_auth_mode": "CREDENTIAL", "default_username": "operator001", "default_password": "synthetic-password", "default_bypass_ssl_check": True,
            "vcenters": [
                {"id": "vc_001", "name": "VC 1", "server": "vc1.example.invalid", "enabled": True, "auth_profile": "COMMON"},
                {"id": "vc_002", "name": "VC 2", "server": "vc2.example.invalid", "enabled": True, "auth_profile": "COMMON"},
            ],
        },
        "quality": {}, "security": {}, "scheduler": {"daily_time": "07:00", "task_name": "DemoTask"},
    })
    assert saved["vcenter"]["default_password_configured"] is True
    assert len(saved["vcenter"]["vcenters"]) == 2
    assert "default_password" not in saved["vcenter"]
    cfg = load_config()
    resolved = PowerCLICollector(cfg)._effective_entry(cfg.rvtools["vcenters"][0])
    assert resolved["username"] == "operator001"
    assert resolved["password"] == "synthetic-password"
    assert resolved["bypass_ssl_check"] is True
