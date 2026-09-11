from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync import scheduler
from asset_sync.collectors.powercli_collector import PowerCLICollector
from asset_sync.config import load_config


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
