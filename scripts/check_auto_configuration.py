from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync import scheduler
from asset_sync.collectors.powercli_collector import PowerCLICollector
from asset_sync.config import load_config


def run_account() -> str:
    """지금 이 점검을 돌리는 계정. 배치 안에서 돌면 곧 작업 스케줄러의 실행 계정이다."""
    domain = os.environ.get("USERDOMAIN") or ""
    try:
        user = getpass.getuser()
    except Exception:                        # 계정 이름을 못 읽어도 점검은 계속한다
        user = os.environ.get("USERNAME") or "?"
    return f"{domain}\\{user}" if domain else user


def powercli_module(powershell: str) -> dict:
    """이 계정으로 PowerCLI 모듈이 보이는가, 보이면 어디에 있는가.

    관리자 계정으로 PowerCLI 를 깔면 그 사람 프로필(Documents) 아래에 들어가는
    일이 흔하다. 작업이 SYSTEM 으로 돌면 그 모듈을 못 보고, 사람이 직접
    check_powercli.ps1 을 돌리면 멀쩡하게 나온다 -- 그래서 원인을 못 찾는다.
    배치 안에서 같은 질문을 던져야 실행 계정 기준의 답이 나온다.
    """
    command = (
        "$m = Get-Module -ListAvailable VMware.VimAutomation.Core | "
        "Sort-Object Version -Descending | Select-Object -First 1; "
        "if ($m) { Write-Output $m.Path }"
    )
    try:
        proc = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"checked": False, "error": str(exc)}
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    return {"checked": True, "found": bool(lines), "path": lines[-1] if lines else ""}


def main() -> None:
    cfg = load_config()
    errors: list[str] = []
    warnings: list[str] = []

    itsm_mode = str(cfg.itsm.get("collection_mode", "ORACLE")).upper()
    vc_mode = str(cfg.rvtools.get("collection_mode", "POWERCLI")).upper()

    # 수집기는 아래 값만 받는다. 그 밖의 값이면 배치가 시작하자마자 실패하므로
    # 여기서 오류로 잡는다 -- 경고로 두면 07시 배치가 실패한 뒤에야 알게 된다.
    valid_itsm = {"DEMO", "FILE_ONLY", "ORACLE"}
    valid_vcenter = {"DEMO", "FILE_ONLY", "POWERCLI"}
    if itsm_mode not in valid_itsm:
        errors.append(
            f"ITSM collection_mode 값이 잘못되었습니다: {itsm_mode} "
            f"(가능한 값: {', '.join(sorted(valid_itsm))})"
        )
    elif itsm_mode != "ORACLE":
        warnings.append(f"ITSM 수집모드가 {itsm_mode} 입니다. 운영에서는 보통 ORACLE 이어야 합니다. → 관리 → 연계 설정")
    if vc_mode not in valid_vcenter:
        errors.append(
            f"vCenter collection_mode 값이 잘못되었습니다: {vc_mode} "
            f"(가능한 값: {', '.join(sorted(valid_vcenter))}) "
            "· 관리 → 연동정보 관리 에서 수집모드를 다시 저장하면 정상값으로 맞춰집니다."
        )
    elif vc_mode != "POWERCLI":
        warnings.append(f"vCenter 수집모드가 {vc_mode} 입니다. 운영에서는 보통 POWERCLI 이어야 합니다. → 관리 → 연계 설정")

    if itsm_mode == "ORACLE":
        if not cfg.oracle.get("enabled", False):
            errors.append("Oracle 연동이 꺼져 있습니다. → 관리 → 연계 설정에서 Oracle 사용을 켜세요.")
        if not cfg.oracle.get("user") or not cfg.oracle.get("password"):
            errors.append("Oracle 조회 계정/비밀번호가 없습니다. → 관리 → 연계 설정 또는 .env 의 ORACLE_USER/ORACLE_PASSWORD")
        if not cfg.oracle.get("dsn") and not (cfg.oracle.get("host") and (cfg.oracle.get("service_name") or cfg.oracle.get("sid"))):
            errors.append("Oracle 접속 주소가 없습니다. DSN 또는 HOST + SERVICE_NAME/SID 가 필요합니다. → 관리 → 연계 설정")
        query = cfg.resolve(cfg.oracle.get("query_file", "config/oracle_query.local.sql"))
        if not query.exists():
            errors.append(f"Oracle 자산 조회 SQL 이 없습니다: {query} → 관리 → 연계 설정 → [자산 테이블 찾기] 에서 대상 테이블을 적용하세요.")

    if vc_mode == "POWERCLI":
        powershell = str(cfg.rvtools.get("powershell_path") or "powershell.exe")
        if not (Path(powershell).exists() if Path(powershell).is_absolute() else shutil.which(powershell)):
            errors.append(f"PowerShell 경로가 올바르지 않습니다: {powershell} → 관리 → 연계 설정")
        resolved_ps = powershell if Path(powershell).is_absolute() else shutil.which(powershell)
        if resolved_ps:
            module = powercli_module(powershell)
            if module.get("checked") and not module.get("found"):
                errors.append(
                    f"실행 계정({run_account()})에서 PowerCLI 모듈(VMware.VimAutomation.Core)을 찾지 못했습니다. "
                    "→ 관리자 PowerShell 로 모듈 폴더를 C:\\Program Files\\WindowsPowerShell\\Modules 에 "
                    "복사하세요(작업이 SYSTEM 으로 돌면 사용자 프로필의 모듈은 안 보입니다)."
                )
            elif module.get("found") and "\\users\\" in str(module.get("path")).lower():
                warnings.append(
                    f"PowerCLI 모듈이 사용자 프로필 아래에 있습니다: {module['path']} "
                    "→ 작업이 SYSTEM 이나 다른 계정으로 돌면 이 모듈을 못 봅니다. "
                    "C:\\Program Files\\WindowsPowerShell\\Modules 로 옮기세요."
                )
        script = cfg.resolve(cfg.rvtools.get("script_path", "scripts/collect_vcenter_inventory.ps1"))
        if not script.exists():
            errors.append(f"PowerCLI 수집 스크립트가 없습니다: {script} → 소스 반입이 누락되었는지 확인하세요.")
        enabled = [v for v in cfg.rvtools.get("vcenters", []) if v.get("enabled", True)]
        if not enabled:
            errors.append("사용 중인 vCenter 가 없습니다. → 관리 → 연계 설정에서 vCenter 를 1개 이상 등록하고 [사용] 으로 두세요.")
        collector = PowerCLICollector(cfg)
        for vc in enabled:
            vc_id = vc.get("id", "UNKNOWN")
            if not str(vc.get("server") or "").strip():
                errors.append(f"vCenter 주소가 비어 있습니다: {vc_id} → 관리 → 연계 설정")
            resolved = collector._effective_entry(vc)
            auth_mode = str(resolved.get("auth_mode") or "CREDENTIAL").upper()
            if auth_mode == "CREDENTIAL" and (not resolved.get("username") or not resolved.get("password")):
                errors.append(f"vCenter 계정/비밀번호가 비어 있습니다: {vc_id} → 관리 → 연계 설정")
            if auth_mode not in {"PASS_THROUGH", "CREDENTIAL"}:
                errors.append(f"지원하지 않는 vCenter 인증 방식입니다: {vc_id}")

    schedule = scheduler.settings(cfg.scheduler)
    if not schedule["enabled"]:
        warnings.append(
            "일일 배치가 꺼져 있어 예정 시각에 수집이 돌지 않습니다. "
            "→ 관리 → 연계 설정 → 실행 설정에서 [일일 배치]를 사용으로 바꾸세요."
        )
    task = scheduler.describe(schedule["task_name"])
    if task.get("supported") and not task.get("registered"):
        warnings.append(
            f"Windows 작업 '{schedule['task_name']}' 이 등록되어 있지 않습니다. "
            "→ scripts\\register_daily_task.bat 을 관리자 권한으로 한 번 실행하세요."
        )

    result = {
        "status": "READY" if not errors else "NOT_READY",
        "account": run_account(),
        "itsm_mode": itsm_mode,
        "vcenter_mode": vc_mode,
        "schedule": schedule,
        "task": task,
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
