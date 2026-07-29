from __future__ import annotations

import json
import logging
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from ..config import AppConfig
from .powercli_collector import PowerCLICollector

LOGGER = logging.getLogger(__name__)


class PowerCLIResourceUsageCollector:
    """Collect ESXi and VM resource usage with the same vCenter registrations.

    One PowerShell process is executed for each enabled vCenter. Credentials are
    inherited from :class:`PowerCLICollector` and are supplied only through the
    child-process environment.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.inventory = PowerCLICollector(config)
        self.settings = config.rvtools.get("resource_usage", {}) or {}

    def _script_path(self) -> Path:
        return self.config.resolve(
            self.settings.get("script_path", "scripts/collect_vcenter_resource_usage.ps1")
        )

    def run_one(self, entry: dict[str, Any], start_date: date, end_date: date) -> dict[str, Any]:
        resolved = self.inventory._effective_entry(entry)
        vc_id = str(resolved.get("id") or resolved.get("name") or "UNKNOWN")
        vc_name = str(resolved.get("name") or vc_id)
        network = self.inventory.test_network(resolved)
        if network.get("status") != "SUCCESS":
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "FAILED",
                "stage": "NETWORK",
                "error": network.get("error"),
            }

        executable = self.inventory._resolve_executable()
        if not executable:
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "FAILED",
                "stage": "POWERSHELL",
                "error": "PowerShell 실행파일을 찾을 수 없습니다.",
            }
        script = self._script_path()
        if not script.exists():
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "FAILED",
                "stage": "SCRIPT",
                "error": f"자원사용률 수집 스크립트가 없습니다: {script}",
            }

        temp_dir = self.config.resolve(
            self.settings.get("temp_dir", "data/temp/powercli_resource")
        )
        temp_dir.mkdir(parents=True, exist_ok=True)
        output = temp_dir / f"{vc_id}_{start_date.isoformat()}_{end_date.isoformat()}.json"
        try:
            env = self.inventory._build_environment(resolved, output)
            env["VCENTER_RESOURCE_INTERVAL_MINS"] = str(
                int(self.settings.get("interval_minutes", 120))
            )
        except Exception as exc:  # noqa: BLE001 - configuration errors belong to this scope
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "FAILED",
                "stage": "CONFIG",
                "error": str(exc),
            }

        command = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-OutputPath",
            str(output),
            "-StartDate",
            start_date.isoformat(),
            "-EndDate",
            end_date.isoformat(),
        ]
        timeout = int(self.settings.get("timeout_seconds") or self.config.rvtools.get("timeout_seconds", 1800))
        try:
            proc = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.config.root_dir),
                env=env,
            )
            if proc.returncode != 0:
                message = (proc.stderr or proc.stdout or f"PowerShell exit={proc.returncode}").strip()
                return {
                    "id": vc_id,
                    "name": vc_name,
                    "status": "FAILED",
                    "stage": self.inventory._classify_failure_stage(message),
                    "error": message,
                }
            if not output.exists():
                raise RuntimeError("자원사용률 JSON 결과가 생성되지 않았습니다.")
            payload = json.loads(output.read_text(encoding="utf-8-sig"))
            hosts = payload.get("hosts", []) if isinstance(payload, dict) else []
            vms = payload.get("vms", []) if isinstance(payload, dict) else []
            if not isinstance(hosts, list) or not isinstance(vms, list):
                raise RuntimeError("자원사용률 JSON은 hosts/vms 배열을 포함해야 합니다.")
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "SUCCESS",
                "hosts": [dict(row) for row in hosts if isinstance(row, dict)],
                "vms": [dict(row) for row in vms if isinstance(row, dict)],
                "metadata": payload.get("metadata", {}),
            }
        except subprocess.TimeoutExpired:
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "FAILED",
                "stage": "TIMEOUT",
                "error": f"자원사용률 수집 제한시간({timeout}초)을 초과했습니다.",
            }
        except Exception as exc:  # noqa: BLE001 - stage is returned to the batch log
            LOGGER.exception("vCenter resource usage collection failed: %s", vc_id)
            return {
                "id": vc_id,
                "name": vc_name,
                "status": "FAILED",
                "stage": "RESOURCE_USAGE",
                "error": str(exc),
            }
        finally:
            output.unlink(missing_ok=True)

    def collect_all(self, start_date: date, end_date: date) -> dict[str, Any]:
        entries = self.inventory.enabled_vcenters()
        if not entries:
            raise RuntimeError("활성 vCenter가 없습니다.")
        results = [self.run_one(entry, start_date, end_date) for entry in entries]
        hosts: list[dict[str, Any]] = []
        vms: list[dict[str, Any]] = []
        success_scopes: list[str] = []
        failed_scopes: dict[str, str] = {}
        for result in results:
            vc_id = str(result.get("id") or "UNKNOWN")
            if result.get("status") == "SUCCESS":
                success_scopes.append(vc_id)
                hosts.extend(result.get("hosts", []))
                vms.extend(result.get("vms", []))
            else:
                failed_scopes[vc_id] = str(result.get("error") or "unknown error")
        status = "SUCCESS" if not failed_scopes else "PARTIAL_SUCCESS" if success_scopes else "FAILED"
        return {
            "status": status,
            "hosts": hosts,
            "vms": vms,
            "success_scopes": success_scopes,
            "failed_scopes": failed_scopes,
            "results": results,
        }
