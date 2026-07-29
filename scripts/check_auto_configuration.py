from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors.powercli_collector import PowerCLICollector
from asset_sync.config import load_config


def main() -> None:
    cfg = load_config()
    errors: list[str] = []
    warnings: list[str] = []

    itsm_mode = str(cfg.itsm.get("collection_mode", "ORACLE")).upper()
    vc_mode = str(cfg.rvtools.get("collection_mode", "POWERCLI")).upper()

    if itsm_mode != "ORACLE":
        warnings.append(f"ITSM mode is {itsm_mode}; operating mode should normally be ORACLE.")
    if vc_mode != "POWERCLI":
        warnings.append(f"vCenter mode is {vc_mode}; operating mode should normally be POWERCLI.")

    if itsm_mode == "ORACLE":
        if not cfg.oracle.get("enabled", False):
            errors.append("Oracle integration is disabled.")
        if not cfg.oracle.get("user") or not cfg.oracle.get("password"):
            errors.append("Oracle user/password are not configured.")
        if not cfg.oracle.get("dsn") and not (cfg.oracle.get("host") and (cfg.oracle.get("service_name") or cfg.oracle.get("sid"))):
            errors.append("Oracle DSN or host + service_name/SID is required.")
        query = cfg.resolve(cfg.oracle.get("query_file", "config/oracle_query.local.sql"))
        if not query.exists():
            errors.append(f"Oracle query file not found: {query}")

    if vc_mode == "POWERCLI":
        powershell = str(cfg.rvtools.get("powershell_path") or "powershell.exe")
        if not (Path(powershell).exists() if Path(powershell).is_absolute() else shutil.which(powershell)):
            errors.append("PowerShell executable path is invalid.")
        script = cfg.resolve(cfg.rvtools.get("script_path", "scripts/collect_vcenter_inventory.ps1"))
        if not script.exists():
            errors.append(f"PowerCLI collection script not found: {script}")
        enabled = [v for v in cfg.rvtools.get("vcenters", []) if v.get("enabled", True)]
        if not enabled:
            errors.append("No enabled vCenter is configured.")
        collector = PowerCLICollector(cfg)
        for vc in enabled:
            vc_id = vc.get("id", "UNKNOWN")
            if not str(vc.get("server") or "").strip():
                errors.append(f"vCenter server is missing: {vc_id}")
            resolved = collector._effective_entry(vc)
            auth_mode = str(resolved.get("auth_mode") or "CREDENTIAL").upper()
            if auth_mode == "CREDENTIAL" and (not resolved.get("username") or not resolved.get("password")):
                errors.append(f"vCenter credential is incomplete: {vc_id}")
            if auth_mode not in {"PASS_THROUGH", "CREDENTIAL"}:
                errors.append(f"Unsupported vCenter auth mode: {vc_id}")

    result = {
        "status": "READY" if not errors else "NOT_READY",
        "itsm_mode": itsm_mode,
        "vcenter_mode": vc_mode,
        "schedule": cfg.scheduler.get("daily_time", "07:00"),
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
