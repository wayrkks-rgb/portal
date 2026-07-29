from __future__ import annotations

import logging
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig

LOGGER = logging.getLogger(__name__)


class RVToolsCommandError(RuntimeError):
    pass


class RVToolsCommandRunner:
    """Execute RVTools once per enabled vCenter.

    The actual vCenter address comes from the registered vCenter row. Common
    authentication settings are resolved centrally and can be overridden per row.
    shell=True is never used.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run_all(self) -> dict[str, Any]:
        enabled = [entry for entry in self.config.rvtools.get("vcenters", []) if bool(entry.get("enabled", True))]
        if not enabled:
            raise RVToolsCommandError("활성화된 vCenter 설정이 없습니다. 연동정보 관리에서 먼저 등록하세요.")
        results = [self.run_one(entry) for entry in enabled]
        return {
            "results": results,
            "success_scopes": [item["id"] for item in results if item["status"] == "SUCCESS"],
            "failed_scopes": [item["id"] for item in results if item["status"] != "SUCCESS"],
        }

    def test_all(self) -> dict[str, Any]:
        enabled = [entry for entry in self.config.rvtools.get("vcenters", []) if bool(entry.get("enabled", True))]
        if not enabled:
            raise RVToolsCommandError("테스트할 활성 vCenter가 없습니다.")
        results = [self.test_one(entry) for entry in enabled]
        success = sum(1 for item in results if item.get("status") == "SUCCESS")
        return {
            "status": "SUCCESS" if success == len(results) else "PARTIAL_SUCCESS" if success else "FAILED",
            "total": len(results),
            "success": success,
            "failed": len(results) - success,
            "results": results,
        }

    @staticmethod
    def test_network(entry: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        server = str(entry.get("server") or "").strip()
        port = int(entry.get("port") or 443)
        if not server:
            return {"status": "FAILED", "server": "", "port": port, "error": "vCenter 주소가 없습니다."}
        try:
            with socket.create_connection((server, port), timeout=timeout):
                return {"status": "SUCCESS", "server": server, "port": port}
        except OSError as exc:
            return {"status": "FAILED", "server": server, "port": port, "error": str(exc)}

    def _effective_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(entry)
        if entry.get("auth_profile") is None and any(key in entry for key in ("auth_mode", "username", "encrypted_password", "bypass_ssl_check")):
            profile = "CUSTOM"
        else:
            profile = str(entry.get("auth_profile", "COMMON")).upper()
        default_port = int(self.config.rvtools.get("default_port", 443))
        resolved["port"] = int(entry.get("port") or default_port)
        if profile == "COMMON":
            resolved["auth_mode"] = str(self.config.rvtools.get("default_auth_mode", "PASS_THROUGH")).upper()
            resolved["username"] = str(self.config.rvtools.get("default_username", ""))
            resolved["encrypted_password"] = str(self.config.rvtools.get("default_encrypted_password", ""))
            resolved["bypass_ssl_check"] = bool(self.config.rvtools.get("default_bypass_ssl_check", False))
        else:
            resolved["auth_mode"] = str(entry.get("auth_mode", "PASS_THROUGH")).upper()
            resolved["bypass_ssl_check"] = bool(entry.get("bypass_ssl_check", False))
        resolved["auth_profile"] = profile
        return resolved

    def _build_command(self, entry: dict[str, Any], executable: Path, incoming: Path, output_file: Path) -> list[str]:
        resolved = self._effective_entry(entry)
        server = str(resolved.get("server") or "").strip()
        if not server:
            raise RVToolsCommandError("vCenter server 주소가 없습니다.")

        command = [str(executable), "-s", server]
        auth_mode = str(resolved.get("auth_mode") or "PASS_THROUGH").upper()
        if auth_mode == "PASS_THROUGH":
            command.append("-passthroughAuth")
        elif auth_mode == "ENCRYPTED_PASSWORD":
            username = str(resolved.get("username") or "").strip()
            encrypted_password = str(resolved.get("encrypted_password") or "").strip()
            if not username or not encrypted_password:
                raise RVToolsCommandError("암호화 계정 방식에는 사용자명과 RVTools 암호화 비밀번호가 필요합니다.")
            command.extend(["-u", username, "-p", encrypted_password])
        else:
            raise RVToolsCommandError(f"지원하지 않는 RVTools 인증방식: {auth_mode}")

        if bool(resolved.get("bypass_ssl_check", False)):
            command.append("-BypassSSLCheck")
        command.extend(["-c", "ExportAll2xlsx", "-d", str(incoming), "-f", output_file.name])
        return command

    def run_one(self, entry: dict[str, Any], output_dir: Path | None = None, test_run: bool = False) -> dict[str, Any]:
        resolved = self._effective_entry(entry)
        vc_id = str(resolved.get("id") or resolved.get("name") or "UNKNOWN")
        display_name = str(resolved.get("name") or vc_id)
        executable_value = str(self.config.rvtools.get("executable_path") or "")
        executable = Path(executable_value)
        if not executable_value or not executable.exists():
            return {"id": vc_id, "name": display_name, "status": "FAILED", "stage": "EXECUTABLE", "stderr": "RVTools 실행 파일 경로를 확인하세요."}

        network = self.test_network(resolved)
        if network["status"] != "SUCCESS":
            return {"id": vc_id, "name": display_name, "status": "FAILED", "stage": "NETWORK", "network": network, "stderr": network.get("error", "vCenter 통신 실패")}

        incoming = output_dir or self.config.resolve(self.config.rvtools.get("incoming_dir", "data/incoming/rvtools"))
        incoming.mkdir(parents=True, exist_ok=True)
        prefix = "TEST" if test_run else "VMList"
        output_file = incoming / f"{prefix}_{vc_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.xlsx"
        try:
            command = self._build_command(resolved, executable, incoming, output_file)
        except RVToolsCommandError as exc:
            return {"id": vc_id, "name": display_name, "status": "FAILED", "stage": "CONFIG", "network": network, "stderr": str(exc)}

        log_scope = vc_id if not self.config.security.get("display_vcenter_server_in_logs", False) else str(resolved.get("server", vc_id))
        LOGGER.info("RVTools start: vcenter=%s auth_profile=%s auth_mode=%s", log_scope, resolved.get("auth_profile"), resolved.get("auth_mode"))
        retry_count = int(resolved.get("retry_count", self.config.rvtools.get("retry_count", 1)))
        timeout_seconds = int(resolved.get("timeout_seconds", self.config.rvtools.get("timeout_seconds", 1800)))
        last_error = ""
        for attempt in range(retry_count + 1):
            try:
                proc = subprocess.run(
                    command,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    cwd=str(executable.parent),
                )
                minimum_size = int(self.config.rvtools.get("minimum_file_size_bytes", 1024))
                if proc.returncode == 0 and output_file.exists() and output_file.stat().st_size >= minimum_size:
                    return {
                        "id": vc_id,
                        "name": display_name,
                        "status": "SUCCESS",
                        "stage": "COLLECTED",
                        "network": network,
                        "returncode": 0,
                        "file": str(output_file),
                        "size": output_file.stat().st_size,
                        "attempt": attempt + 1,
                    }
                last_error = (proc.stderr or proc.stdout or "정상 결과 파일이 생성되지 않았습니다.")[-2000:]
            except subprocess.TimeoutExpired:
                last_error = "RVTools 실행시간 초과"
            except OSError as exc:
                last_error = str(exc)
            if attempt < retry_count:
                time.sleep(3)
        return {"id": vc_id, "name": display_name, "status": "FAILED", "stage": "RVTOOLS", "network": network, "stderr": last_error}

    def test_one(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Run RVTools and validate that the generated workbook can be parsed."""
        from .rvtools_file_collector import RVToolsFileCollector

        test_dir = self.config.resolve("data/test/rvtools")
        result = self.run_one(entry, output_dir=test_dir, test_run=True)
        if result.get("status") != "SUCCESS":
            return result
        path = Path(str(result["file"]))
        try:
            records, metadata = RVToolsFileCollector(self.config).collect([path])
            file_meta = (metadata.get("files") or [{}])[0]
            result.update(
                {
                    "stage": "VALIDATED",
                    "row_count": len(records),
                    "sheet": str(self.config.rvtools.get("sheet_name", "vInfo")),
                    "scope": file_meta.get("scope"),
                    "optional_columns": file_meta.get("optional_columns", []),
                    "xlsx_readable": True,
                }
            )
            return result
        except Exception as exc:
            result.update({"status": "FAILED", "stage": "XLSX_VALIDATION", "stderr": str(exc), "xlsx_readable": False})
            return result
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("RVTools test file cleanup failed: %s", path)
