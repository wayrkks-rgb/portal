from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

from ..config import AppConfig

LOGGER = logging.getLogger(__name__)

_REQUIRED = {"VM", "Powerstate", "CPUs", "Memory"}


class PowerCLICollectionError(RuntimeError):
    pass


class PowerCLICollector:
    """Collect vCenter VM inventory through PowerCLI.

    One PowerShell process is executed per enabled vCenter. Credentials are passed
    through the child-process environment, never command-line arguments. The
    PowerShell script returns JSON; this class optionally stores a daily XLSX copy
    for audit/inspection while SQLite remains the comparison source of truth.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def enabled_vcenters(self) -> list[dict[str, Any]]:
        return [item for item in self.config.rvtools.get("vcenters", []) if bool(item.get("enabled", True))]

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
        if entry.get("auth_profile") is None and any(key in entry for key in ("auth_mode", "username", "password", "bypass_ssl_check")):
            profile = "CUSTOM"
        else:
            profile = str(entry.get("auth_profile", "COMMON")).upper()
        resolved["auth_profile"] = profile
        resolved["port"] = int(entry.get("port") or self.config.rvtools.get("default_port", 443))
        if profile == "COMMON":
            resolved["auth_mode"] = str(self.config.rvtools.get("default_auth_mode", "CREDENTIAL")).upper()
            resolved["username"] = str(self.config.rvtools.get("default_username", ""))
            resolved["password"] = str(self.config.rvtools.get("default_password", ""))
            resolved["bypass_ssl_check"] = bool(self.config.rvtools.get("default_bypass_ssl_check", False))
        else:
            resolved["auth_mode"] = str(entry.get("auth_mode", "CREDENTIAL")).upper()
            resolved["bypass_ssl_check"] = bool(entry.get("bypass_ssl_check", False))
        return resolved

    def _resolve_executable(self) -> str | None:
        configured = str(self.config.rvtools.get("powershell_path") or "powershell.exe").strip()
        path = Path(configured)
        if path.is_absolute() and path.exists():
            return str(path)
        return shutil.which(configured)

    def _resolve_script(self) -> Path:
        return self.config.resolve(self.config.rvtools.get("script_path", "scripts/collect_vcenter_inventory.ps1"))

    def _build_environment(self, entry: dict[str, Any], output_json: Path) -> dict[str, str]:
        resolved = self._effective_entry(entry)
        server = str(resolved.get("server") or "").strip()
        if not server:
            raise PowerCLICollectionError("vCenter 주소가 없습니다.")
        auth_mode = str(resolved.get("auth_mode") or "CREDENTIAL").upper()
        if auth_mode not in {"CREDENTIAL", "PASS_THROUGH"}:
            raise PowerCLICollectionError(f"지원하지 않는 인증방식입니다: {auth_mode}")
        username = str(resolved.get("username") or "")
        password = str(resolved.get("password") or "")
        if auth_mode == "CREDENTIAL" and (not username.strip() or not password):
            raise PowerCLICollectionError("계정 인증 방식에는 vCenter 사용자명과 비밀번호가 필요합니다.")

        env = dict(os.environ)
        env.update(
            {
                "VCENTER_SERVER": server,
                "VCENTER_PORT": str(resolved.get("port") or 443),
                "VCENTER_ID": str(resolved.get("id") or resolved.get("name") or "UNKNOWN"),
                "VCENTER_NAME": str(resolved.get("name") or resolved.get("id") or "UNKNOWN"),
                "VCENTER_AUTH_MODE": auth_mode,
                "VCENTER_USERNAME": username,
                "VCENTER_PASSWORD": password,
                "VCENTER_IGNORE_CERT": "true" if bool(resolved.get("bypass_ssl_check", False)) else "false",
                "VCENTER_OUTPUT_JSON": str(output_json),
                # BULK(기본)은 Get-View 로 한 번에 받는다. 새 방식이 환경에 맞지 않으면
                # 설정에서 COMPAT 으로 바꿔 예전 방식으로 돌릴 수 있다.
                "VCENTER_COLLECT_MODE": str(self.config.rvtools.get("collect_mode", "BULK")).upper(),
            }
        )
        return env

    def _load_records(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise PowerCLICollectionError(f"PowerCLI 결과 JSON이 생성되지 않았습니다: {path}")
        raw = path.read_text(encoding="utf-8-sig").strip()
        if not raw:
            raise PowerCLICollectionError("PowerCLI 결과 JSON이 비어 있습니다.")
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise PowerCLICollectionError("PowerCLI 결과 JSON 최상위 값은 배열이어야 합니다.")
        records = [dict(item) for item in data if isinstance(item, dict)]
        if not records:
            raise PowerCLICollectionError("PowerCLI에서 조회된 VM이 없습니다.")
        missing = sorted(_REQUIRED - set(records[0]))
        if missing:
            raise PowerCLICollectionError(f"PowerCLI 필수 필드 누락: {', '.join(missing)}")
        return records

    def _write_xlsx(self, records: list[dict[str, Any]], target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        headers: list[str] = []
        seen: set[str] = set()
        for row in records:
            for key in row:
                if key not in seen:
                    headers.append(key)
                    seen.add(key)
        wb = Workbook()
        ws = wb.active
        ws.title = "vInfo"
        ws.append(headers)
        for row in records:
            ws.append([row.get(key) for key in headers])
        wb.save(target)
        wb.close()
        return target

    @staticmethod
    def _classify_failure_stage(message: str) -> str:
        lowered = message.lower()
        if "vmware.vimautomation.core" in lowered or "import-module" in lowered or "module" in lowered and "not" in lowered:
            return "POWERCLI_MODULE"
        if "connect-viserver" in lowered or "authentication" in lowered or "credential" in lowered or "login" in lowered:
            return "VCENTER_AUTH"
        if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
            return "CERTIFICATE"
        if "permission" in lowered or "privilege" in lowered or "not authorized" in lowered:
            return "VCENTER_PERMISSION"
        return "POWERCLI"

    def run_one(self, entry: dict[str, Any], *, test_run: bool = False) -> dict[str, Any]:
        resolved = self._effective_entry(entry)
        vc_id = str(resolved.get("id") or resolved.get("name") or "UNKNOWN")
        name = str(resolved.get("name") or vc_id)
        network = self.test_network(resolved)
        if network["status"] != "SUCCESS":
            return {"id": vc_id, "name": name, "status": "FAILED", "stage": "NETWORK", "network": network, "error": network.get("error")}

        executable = self._resolve_executable()
        if not executable:
            return {"id": vc_id, "name": name, "status": "FAILED", "stage": "POWERSHELL", "network": network, "error": "PowerShell 실행파일을 찾을 수 없습니다."}
        script = self._resolve_script()
        if not script.exists():
            return {"id": vc_id, "name": name, "status": "FAILED", "stage": "SCRIPT", "network": network, "error": f"PowerCLI 수집 스크립트가 없습니다: {script}"}

        temp_dir = self.config.resolve(self.config.rvtools.get("temp_dir", "data/temp/powercli"))
        temp_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_json = temp_dir / f"{'TEST_' if test_run else ''}{vc_id}_{stamp}.json"
        try:
            env = self._build_environment(resolved, output_json)
        except PowerCLICollectionError as exc:
            return {"id": vc_id, "name": name, "status": "FAILED", "stage": "CONFIG", "network": network, "error": str(exc)}

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
            str(output_json),
        ]
        timeout = int(resolved.get("timeout_seconds") or self.config.rvtools.get("timeout_seconds", 1800))
        retry_count = int(resolved.get("retry_count") or self.config.rvtools.get("retry_count", 1))
        log_scope = vc_id if not self.config.security.get("display_vcenter_server_in_logs", False) else str(resolved.get("server", vc_id))
        LOGGER.info("PowerCLI collection start: vcenter=%s auth_profile=%s auth_mode=%s", log_scope, resolved.get("auth_profile"), resolved.get("auth_mode"))

        last_error = ""
        proc: subprocess.CompletedProcess[str] | None = None
        for attempt in range(retry_count + 1):
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
                if proc.returncode == 0:
                    records = self._load_records(output_json)
                    snapshot_path: Path | None = None
                    if bool(self.config.rvtools.get("export_xlsx", True)):
                        if test_run:
                            snapshot_dir = self.config.resolve("data/test/powercli")
                        else:
                            date_dir = datetime.now().strftime("%Y%m%d")
                            snapshot_dir = self.config.resolve(self.config.rvtools.get("snapshot_dir", "data/archive/vcenter")) / date_dir
                        snapshot_path = self._write_xlsx(records, snapshot_dir / f"vcenter_{vc_id}_{stamp}.xlsx")
                    return {
                        "id": vc_id,
                        "name": name,
                        "status": "SUCCESS",
                        "stage": "VALIDATED",
                        "network": network,
                        "records": records,
                        "row_count": len(records),
                        "json_file": str(output_json),
                        "xlsx_file": str(snapshot_path) if snapshot_path else None,
                        "attempt": attempt + 1,
                        "stdout": (proc.stdout or "")[-2000:],
                    }
                last_error = (proc.stderr or proc.stdout or "PowerCLI 실행에 실패했습니다.")[-4000:]
            except subprocess.TimeoutExpired:
                last_error = "PowerCLI 실행시간 초과"
            except (OSError, ValueError, json.JSONDecodeError, PowerCLICollectionError) as exc:
                last_error = str(exc)
            if attempt < retry_count:
                time.sleep(3)
        return {"id": vc_id, "name": name, "status": "FAILED", "stage": self._classify_failure_stage(last_error), "network": network, "error": last_error, "returncode": None if proc is None else proc.returncode}

    def _batch_environment(self, entries: list[dict[str, Any]]) -> dict[str, str]:
        """여러 통합기의 접속정보를 번호를 붙여 환경변수에 담는다.

        비밀번호를 명령행에 두면 작업관리자와 감사로그에 그대로 보인다. 파일에 쓰면
        디스크에 남는다. 자식 프로세스 환경변수가 둘 다 피하는 길이다.
        """
        env = dict(os.environ)
        env["VCENTER_COUNT"] = str(len(entries))
        env["VCENTER_COLLECT_MODE"] = str(self.config.rvtools.get("collect_mode", "BULK")).upper()
        for index, entry in enumerate(entries, 1):
            resolved = self._effective_entry(entry)
            prefix = f"VCENTER_{index}_"
            env.update({
                prefix + "ID": str(resolved.get("id") or resolved.get("name") or "UNKNOWN"),
                prefix + "NAME": str(resolved.get("name") or resolved.get("id") or "UNKNOWN"),
                prefix + "SERVER": str(resolved.get("server") or "").strip(),
                prefix + "PORT": str(resolved.get("port") or 443),
                prefix + "AUTH_MODE": str(resolved.get("auth_mode") or "CREDENTIAL").upper(),
                prefix + "USERNAME": str(resolved.get("username") or ""),
                prefix + "PASSWORD": str(resolved.get("password") or ""),
                prefix + "IGNORE_CERT": "true" if bool(resolved.get("bypass_ssl_check", False)) else "false",
            })
        return env

    _RESULT_PREFIX = "RESULT="

    def _parse_batch_output(self, stdout: str) -> dict[str, dict[str, str]]:
        """스크립트가 통합기별로 낸 RESULT= 줄을 읽는다.

        형식: RESULT=<id>|SUCCESS|<건수>|<모드>|<단계별시간>
              RESULT=<id>|FAILED|0||<오류>
        """
        parsed: dict[str, dict[str, str]] = {}
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith(self._RESULT_PREFIX):
                continue
            parts = line[len(self._RESULT_PREFIX):].split("|")
            if len(parts) < 5:
                continue
            vc_id, status, count, mode, detail = parts[0], parts[1], parts[2], parts[3], "|".join(parts[4:])
            parsed[vc_id] = {"status": status, "count": count, "mode": mode, "detail": detail}
        return parsed

    def run_batch(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """한 PowerShell 프로세스로 여러 통합기를 수집한다."""
        if not entries:
            return []
        if len(entries) == 1 and not bool(self.config.rvtools.get("batch_collection", True)):
            return [self.run_one(entries[0])]

        results: list[dict[str, Any]] = []
        reachable: list[dict[str, Any]] = []
        networks: dict[str, dict[str, Any]] = {}
        for entry in entries:
            resolved = self._effective_entry(entry)
            vc_id = str(resolved.get("id") or resolved.get("name") or "UNKNOWN")
            network = self.test_network(resolved)
            networks[vc_id] = network
            if network["status"] != "SUCCESS":
                # 닿지 않는 통합기를 PowerShell 까지 보내 기다릴 이유가 없다.
                results.append({**self._failed(entry, "NETWORK", str(network.get("error") or "")),
                                "network": network})
            else:
                reachable.append(entry)
        if not reachable:
            return results

        executable = self._resolve_executable()
        script = self._resolve_script()
        if not executable:
            return results + [self._failed(e, "POWERSHELL", "PowerShell 실행파일을 찾을 수 없습니다.") for e in reachable]
        if not script.exists():
            return results + [self._failed(e, "SCRIPT", f"PowerCLI 수집 스크립트가 없습니다: {script}") for e in reachable]

        temp_dir = self.config.resolve(self.config.rvtools.get("temp_dir", "data/temp/powercli"))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_dir = temp_dir / f"batch_{stamp}"
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            env = self._batch_environment(reachable)
        except PowerCLICollectionError as exc:
            return results + [self._failed(e, "CONFIG", str(exc)) for e in reachable]

        command = [
            executable, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-OutputDir", str(output_dir),
        ]
        # 여러 대를 한 프로세스가 맡으므로 제한시간도 그만큼 늘린다.
        per_vcenter = int(self.config.rvtools.get("timeout_seconds", 1800))
        timeout = per_vcenter * len(reachable)
        ids = [str(self._effective_entry(e).get("id") or e.get("name")) for e in reachable]
        LOGGER.info("PowerCLI 수집 시작: 통합기 %d대 %s", len(reachable), ids)

        try:
            proc = subprocess.run(
                command, shell=False, capture_output=True, text=True,
                timeout=timeout, cwd=str(self.config.root_dir), env=env,
            )
        except subprocess.TimeoutExpired:
            return results + [self._failed(e, "POWERCLI", f"PowerCLI 실행시간 초과({timeout}초)") for e in reachable]
        except OSError as exc:
            return results + [self._failed(e, "POWERSHELL", str(exc)) for e in reachable]

        stdout = proc.stdout or ""
        parsed = self._parse_batch_output(stdout)
        if stdout:
            for line in stdout.splitlines():
                if line.startswith(("MODULE_SECONDS=", "TIMING=", "BULK_FALLBACK=")):
                    LOGGER.info("PowerCLI %s", line.strip())

        if not parsed:
            # 스크립트가 통합기별 결과를 내지 못했다. 전체 실패로 본다.
            message = (proc.stderr or stdout or "PowerCLI 실행에 실패했습니다.")[-4000:]
            stage = self._classify_failure_stage(message)
            return results + [self._failed(e, stage, message) for e in reachable]

        for entry in reachable:
            resolved = self._effective_entry(entry)
            vc_id = str(resolved.get("id") or resolved.get("name") or "UNKNOWN")
            name = str(resolved.get("name") or vc_id)
            info = parsed.get(vc_id)
            if info is None:
                results.append(self._failed(entry, "POWERCLI", "이 통합기의 수집 결과가 없습니다."))
                continue
            if info["status"] != "SUCCESS":
                results.append({**self._failed(entry, self._classify_failure_stage(info["detail"]), info["detail"]),
                                "network": networks.get(vc_id)})
                continue
            json_path = output_dir / f"{vc_id}.json"
            try:
                records = self._load_records(json_path)
            except (PowerCLICollectionError, OSError, json.JSONDecodeError) as exc:
                results.append(self._failed(entry, "POWERCLI", str(exc)))
                continue
            snapshot_path: Path | None = None
            if bool(self.config.rvtools.get("export_xlsx", True)):
                date_dir = datetime.now().strftime("%Y%m%d")
                snapshot_dir = self.config.resolve(
                    self.config.rvtools.get("snapshot_dir", "data/archive/vcenter")
                ) / date_dir
                snapshot_path = self._write_xlsx(records, snapshot_dir / f"vcenter_{vc_id}_{stamp}.xlsx")
            LOGGER.info("PowerCLI 수집 완료: vcenter=%s %d건 (%s)", vc_id, len(records), info["detail"])
            results.append({
                "id": vc_id, "name": name, "status": "SUCCESS", "stage": "VALIDATED",
                "network": networks.get(vc_id), "records": records, "row_count": len(records),
                "json_file": str(json_path), "xlsx_file": str(snapshot_path) if snapshot_path else None,
                "collect_mode": info["mode"], "timing": info["detail"], "attempt": 1,
            })
        return results

    def _parallel_limit(self, count: int) -> int:
        """동시에 수집할 통합기 수.

        한 대씩 순서대로 하면 통합기가 늘어나는 만큼 그대로 늘어난다. 대기시간의
        대부분이 PowerCLI 모듈 로딩과 vCenter 응답 기다림이라, 동시에 돌리면
        거의 한 대 시간에 끝난다.

        다만 무제한으로 띄우면 WAS 메모리와 vCenter 쪽 부담이 커진다. PowerShell
        프로세스 하나가 수백 MB 를 쓰므로 기본을 4 로 두고 설정으로 바꿀 수 있게 한다.
        1 로 두면 예전처럼 순서대로 돈다.
        """
        raw = self.config.rvtools.get("parallel_collections", 4)
        # 값이 없으면 기본값, 숫자가 아니면 기본값. 0 이나 음수는 '순차' 로 본다
        # (설정에 0 을 적은 사람은 병렬을 끄고 싶은 것이다).
        if raw in (None, ""):
            configured = 4
        else:
            try:
                configured = int(raw)
            except (TypeError, ValueError):
                LOGGER.warning("parallel_collections 값이 숫자가 아닙니다(%r). 기본값 4 를 씁니다.", raw)
                configured = 4
        return max(1, min(configured, count))

    def _batches(self, entries: list[dict[str, Any]], workers: int) -> list[list[dict[str, Any]]]:
        """통합기를 프로세스별로 나눈다.

        PowerCLI 모듈 로딩이 6~15초다. 통합기마다 프로세스를 띄우면 그 비용을 통합기
        수만큼 낸다. 한 프로세스가 여러 대를 맡으면 한 번만 낸다.

        돌아가며 나눠 담아(round-robin) 앞쪽 프로세스에만 몰리지 않게 한다.
        """
        groups: list[list[dict[str, Any]]] = [[] for _ in range(workers)]
        for index, entry in enumerate(entries):
            groups[index % workers].append(entry)
        return [group for group in groups if group]

    def _run_all(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        workers = self._parallel_limit(len(entries))
        batched = bool(self.config.rvtools.get("batch_collection", True))
        if not batched:
            groups = [[entry] for entry in entries]
        else:
            groups = self._batches(entries, workers)

        if len(groups) <= 1:
            collected = [self.run_batch(group) for group in groups]
        else:
            LOGGER.info(
                "PowerCLI 수집: 통합기 %d대를 프로세스 %d개로 나눠 동시에 진행합니다",
                len(entries), len(groups),
            )
            collected: list[list[dict[str, Any]] | None] = [None] * len(groups)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="powercli") as pool:
                futures = {pool.submit(self.run_batch, group): index for index, group in enumerate(groups)}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        collected[index] = future.result()
                    except Exception as exc:  # 한 묶음이 깨져도 나머지는 살려야 한다.
                        LOGGER.exception("PowerCLI 수집 중 예외")
                        collected[index] = [self._failed(entry, "COLLECTOR", str(exc)) for entry in groups[index]]

        # 결과 순서는 설정 순서를 따라야 한다. 로그와 화면이 들쑥날쑥하면 읽기 어렵다.
        by_id: dict[str, dict[str, Any]] = {}
        for group_results in collected:
            for item in group_results or []:
                by_id[str(item.get("id"))] = item
        ordered: list[dict[str, Any]] = []
        for entry in entries:
            vc_id = str(self._effective_entry(entry).get("id") or entry.get("name") or "UNKNOWN")
            ordered.append(by_id.get(vc_id) or self._failed(entry, "COLLECTOR", "수집 결과가 없습니다."))
        return ordered

    def _failed(self, entry: dict[str, Any], stage: str, error: str) -> dict[str, Any]:
        vc_id = str(entry.get("id") or entry.get("name") or "UNKNOWN")
        return {
            "id": vc_id,
            "name": str(entry.get("name") or vc_id),
            "status": "FAILED",
            "stage": stage,
            "error": error,
        }

    def collect_all(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        enabled = self.enabled_vcenters()
        if not enabled:
            raise PowerCLICollectionError("활성화된 vCenter가 없습니다. 연동정보 관리에서 먼저 등록하세요.")
        results = self._run_all(enabled)
        records: list[dict[str, Any]] = []
        success_scopes: list[str] = []
        failed_scopes: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        for result in results:
            vc_id = str(result.get("id") or "UNKNOWN")
            if result.get("status") == "SUCCESS":
                success_scopes.append(vc_id)
                records.extend(result.get("records") or [])
                files.append({
                    "scope": vc_id,
                    "rows": int(result.get("row_count") or 0),
                    "json_file": result.get("json_file"),
                    "xlsx_file": result.get("xlsx_file"),
                })
            else:
                failed_scopes[vc_id] = str(result.get("error") or "PowerCLI collection failed")
        if not records:
            raise PowerCLICollectionError("정상 수집된 vCenter VM 데이터가 없습니다.")
        return records, {
            "mode": "POWERCLI",
            "success_scopes": success_scopes,
            "failed_scopes": failed_scopes,
            "results": [{k: v for k, v in item.items() if k != "records"} for item in results],
            "files": files,
            "failed_files": [],
        }

    def test_one(self, entry: dict[str, Any]) -> dict[str, Any]:
        result = self.run_one(entry, test_run=True)
        result.pop("records", None)
        result["json_created"] = bool(result.get("json_file"))
        result["xlsx_created"] = bool(result.get("xlsx_file"))
        for key in ("json_file", "xlsx_file"):
            value = result.get(key)
            if value:
                try:
                    Path(str(value)).unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("PowerCLI test artifact cleanup failed: %s", value)
            result[key] = None
        return result

    def test_all(self) -> dict[str, Any]:
        enabled = self.enabled_vcenters()
        if not enabled:
            raise PowerCLICollectionError("테스트할 활성 vCenter가 없습니다.")
        results = [self.test_one(entry) for entry in enabled]
        success = sum(1 for item in results if item.get("status") == "SUCCESS")
        return {
            "status": "SUCCESS" if success == len(results) else "PARTIAL_SUCCESS" if success else "FAILED",
            "total": len(results),
            "success": success,
            "failed": len(results) - success,
            "results": results,
        }


class VCenterSnapshotFileCollector:
    """Fallback loader for PowerCLI-exported JSON/XLSX snapshots."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def collect(self, files: list[Path] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        incoming = self.config.resolve(self.config.rvtools.get("incoming_dir", "data/incoming/vcenter"))
        files = files or sorted([*incoming.glob("*.json"), *incoming.glob("*.xlsx")])
        if not files:
            raise PowerCLICollectionError(f"vCenter 스냅샷 파일이 없습니다: {incoming}")
        records: list[dict[str, Any]] = []
        metadata: list[dict[str, Any]] = []
        failed: dict[str, str] = {}
        for path in files:
            try:
                if path.suffix.lower() == ".json":
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    rows = [data] if isinstance(data, dict) else data
                    rows = [dict(item) for item in rows if isinstance(item, dict)]
                else:
                    wb = load_workbook(path, read_only=True, data_only=True)
                    ws = wb["vInfo"] if "vInfo" in wb.sheetnames else wb.active
                    headers = [str(value).strip() if value is not None else "" for value in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
                    rows = []
                    for values in ws.iter_rows(min_row=2, values_only=True):
                        if not any(value is not None and str(value).strip() for value in values):
                            continue
                        rows.append({header: values[index] if index < len(values) else None for index, header in enumerate(headers) if header})
                    wb.close()
                if not rows:
                    raise PowerCLICollectionError("데이터 행이 없습니다.")
                missing = sorted(_REQUIRED - set(rows[0]))
                if missing:
                    raise PowerCLICollectionError(f"필수 필드 누락: {', '.join(missing)}")
                scope = str(rows[0].get("VI SDK Server") or path.stem)
                for row in rows:
                    row.setdefault("_source_file", str(path))
                    row.setdefault("_vcenter_scope", str(row.get("VI SDK Server") or scope))
                records.extend(rows)
                metadata.append({"file": str(path), "rows": len(rows), "scope": scope})
            except Exception as exc:
                failed[path.stem] = str(exc)
        if not records:
            raise PowerCLICollectionError("정상 처리된 vCenter 스냅샷 파일이 없습니다.")
        return records, {"mode": "FILE_ONLY", "success_scopes": [item["scope"] for item in metadata], "failed_scopes": failed, "files": metadata, "failed_files": []}
