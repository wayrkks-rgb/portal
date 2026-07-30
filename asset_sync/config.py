from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(slots=True)
class AppConfig:
    """Runtime configuration loaded from safe defaults and local-only files."""

    root_dir: Path
    timezone: str = "Asia/Seoul"
    sqlite_path: Path = Path("data/asset_history.db")
    log_level: str = "INFO"
    database: dict[str, Any] = field(default_factory=dict)
    modules: dict[str, Any] = field(default_factory=dict)
    oracle: dict[str, Any] = field(default_factory=dict)
    itsm: dict[str, Any] = field(default_factory=dict)
    rvtools: dict[str, Any] = field(default_factory=dict)
    matching: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    retention: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    scheduler: dict[str, Any] = field(default_factory=dict)

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    @property
    def database_path(self) -> Path:
        return self.resolve(self.sqlite_path)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"YAML 최상위 값은 객체여야 합니다: {path}")
    return loaded


def load_module_registry_files(root: Path) -> list[dict[str, Any]]:
    """``config/modules/<id>.yaml`` 을 모아 대메뉴 정의 목록으로 돌려준다.

    담당자마다 ``config/app_config.yaml`` 의 ``modules.registry`` 를 고치면 branch
    병합에서 매번 같은 목록에서 충돌한다. 그래서 각자 파일 하나만 추가한다.

    ``<id>.local.yaml`` 은 같은 모듈에 대한 배포 환경별 덮어쓰기다(주로 ``base_url``).
    git 에 올리지 않으므로 담당자의 개발 주소가 저장소에 섞이지 않는다.
    """
    directory = root / "config" / "modules"
    if not directory.is_dir():
        return []
    definitions: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("_") or path.name.endswith(".local.yaml"):
            continue
        module_id = path.stem
        item = _load_yaml(path)
        item = _deep_merge(item, _load_yaml(directory / f"{module_id}.local.yaml"))
        declared = str(item.get("id") or "").strip()
        if declared and declared != module_id:
            raise ValueError(
                f"{path.name}: id 가 파일 이름과 다릅니다 ({declared!r} != {module_id!r}). "
                "파일 하나가 대메뉴 하나이므로 이름을 맞춰야 어느 파일이 무엇인지 알 수 있습니다."
            )
        item["id"] = module_id
        definitions.append(item)
    return definitions


def _load_dotenv(path: Path) -> dict[str, str]:
    """Small .env reader to avoid an additional dependency in the closed network."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _environment(root: Path) -> dict[str, str]:
    local_values = _load_dotenv(root / ".env")
    # Real process environment always has precedence over the local file.
    return {**local_values, **dict(os.environ)}


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load safe base config plus local-only overrides.

    Order:
      1. built-in defaults
      2. config/app_config.yaml (safe distributable defaults)
      3. config/app_config.local.yaml (closed-network values)
      4. config/vcenters.local.yaml
      5. .env / process environment for Oracle secrets
    """

    root = Path(os.getenv("ASSET_APP_ROOT", Path(__file__).resolve().parents[1])).resolve()
    base_config_path = Path(path or os.getenv("ASSET_APP_CONFIG", root / "config" / "app_config.yaml"))
    if not base_config_path.is_absolute():
        base_config_path = root / base_config_path

    defaults: dict[str, Any] = {
        "app": {
            "timezone": "Asia/Seoul",
            "sqlite_path": "data/asset_history.db",
            "log_level": "INFO",
        },
        "database": {
            # sqlite: 단일 호스트/데모용. mysql: 여러 WAS가 하나의 DB를 공유할 때.
            "engine": "sqlite",
            "mysql": {
                "host": "",
                "port": 3306,
                "database": "",
                "user": "",
                "password": "",
                "charset": "utf8mb4",
                "connect_timeout_seconds": 10,
            },
        },
        "modules": {
            # 통합 웹이 각 도메인 WAS 를 호출할 때 쓰는 공통값
            "request_timeout_seconds": 5,
            "retry_count": 1,
            "dashboard_budget_seconds": 12,
            "max_response_bytes": 4 * 1024 * 1024,
            "verify_tls": True,
            "auth": {
                # 각 WAS 가 같은 값을 갖고 토큰을 검증한다. 비워두면 토큰을 붙이지 않는다.
                "shared_secret": "",
                "token_ttl_seconds": 60,
                "header": "X-Portal-Token",
            },
            # 대메뉴 정의. base_url 이 비어 있으면 통합 웹 내부 모듈이다.
            "registry": [
                {
                    "id": "asset_sync",
                    "name": "자산 정합성",
                    "icon": "🔗",
                    "base_url": "",
                    "enabled": True,
                    "required_role": "user",
                    "menu_section": "운영",
                    "page": "asset_sync",
                },
            ],
        },
        "oracle": {
            "enabled": True,
            "mode": "thin",
            "asset_source": "",
            "query_file": "config/oracle_query.local.sql",
            "fetch_size": 1000,
            "connect_timeout_seconds": 10,
            "port": 1521,
        },
        "itsm": {
            "collection_mode": "ORACLE",
            "incoming_dir": "data/incoming/itsm",
            "sheet_name": "Sheet1",
            "header_row": 1,
            "cpu_compare_field": "CM_CPU_CORE_CNT",
            "memory_field": "CM_MEMORY",
            "memory_unit": "GB",
            "os_eos_field": "OS_EOS_DATE",
            "os_eos_display_name": "OS 밴더지원종료일",
            "owner_field": "CM_WOR_MNG_EMP_ID",
            "department_field": "CM_OWN_DPT_ID",
            "tracked_fields": [
                "CM_NAME", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_OS", "CM_OS_VERSION",
                "CM_CPU_CORE_CNT", "CM_CPU_CNT", "CM_MEMORY", "CM_OWN_CAT_CD", "CM_SVR_CAT_CD",
                "CM_CAT_CD", "CM_STA_CD", "CM_NET_CD", "CM_DR_YN", "CM_MTN_YN", "CM_FMTN_YN",
                "CM_OWN_EMP_ID", "CM_OWN_DPT_ID", "CM_USER_EMP_ID", "CM_USER_DPT_ID",
                "CM_WOR_MNG_EMP_ID", "CM_PLACE", "CM_RACK_LOC", "CM_SERIAL_NO", "CM_MAKE_NAME",
                "CM_MODEL_NAME", "CM_TAKIN_DTTM", "CM_DESCR", "CM_DESCR2",
            ],
            "ignore_fields": ["CM_MOD_DTTM", "CM_REG_DTTM"],
        },
        "vcenter": {
            "collection_mode": "POWERCLI",
            "powershell_path": "powershell.exe",
            "script_path": "scripts/collect_vcenter_inventory.ps1",
            "incoming_dir": "data/incoming/vcenter",
            "snapshot_dir": "data/archive/vcenter",
            "temp_dir": "data/temp/powercli",
            "export_xlsx": True,
            "power_on_value": "poweredon",
            "cpu_compare_field": "CPUs",
            "memory_compare_field": "Memory",
            "exclude_templates": True,
            "exclude_srm_placeholders": True,
            "hostname_suffixes": [".korealife.dom"],
            "timeout_seconds": 1800,
            "retry_count": 1,
            "default_port": 443,
            "default_auth_mode": "CREDENTIAL",
            "default_username": "",
            "default_password": "",
            "default_bypass_ssl_check": False,
            "vcenters": [],
            "resource_usage": {
                "enabled": True,
                "script_name": "VM_ResourceUsageExport",
                "script_path": "scripts/collect_vcenter_resource_usage.ps1",
                "temp_dir": "data/temp/powercli_resource",
                "interval_minutes": 120,
                "timeout_seconds": 3600,
                "output_dir": "data/archive/vcenter_resource",
                "source_format": "JSON",
            },
        },
        "matching": {
            "use_identity_map_first": True,
            "auto_remember_exact_match": True,
            "allow_vm_name_auto_match": False,
            "memory_tolerance_mb": 1,
            "sync_tolerance_days": 1,
        },
        "quality": {
            "minimum_itsm_records": 1,
            "minimum_rvtools_records": 1,
            "rvtools_count_warning_ratio": 0.70,
            "rvtools_count_critical_ratio": 0.30,
            "itsm_count_warning_ratio": 0.70,
            "itsm_count_critical_ratio": 0.30,
            "baseline_method": "PREVIOUS_SUCCESS",
            "eos_near_days": 180,
        },
        "retention": {
            "raw_snapshot_days": 365,
            "change_event_days": 1095,
            "source_excel_days": 365,
        },
        "security": {
            "mask_ip_in_logs": True,
            "mask_hostname_in_logs": True,
            "mask_user_id_in_logs": True,
            "allow_sensitive_export": False,
            "sensitive_export_role": "ADMIN",
            "display_vcenter_server_in_logs": False,
            "allow_password_in_command": False,
        },
        "scheduler": {
            "daily_time": "07:00",
            "task_name": "AssetDailyCollection",
        },
    }

    base_yaml = _load_yaml(base_config_path)
    local_yaml = _load_yaml(root / "config" / "app_config.local.yaml")

    # vcenter is the canonical external configuration section. The old rvtools
    # section is accepted only as a migration alias for previously saved files.
    for loaded in (base_yaml, local_yaml):
        if "rvtools" in loaded:
            legacy = loaded.pop("rvtools") or {}
            loaded["vcenter"] = _deep_merge(dict(legacy), loaded.get("vcenter") or {})

    merged = _deep_merge(defaults, base_yaml)
    merged = _deep_merge(merged, local_yaml)

    vcenters_local = _load_yaml(root / "config" / "vcenters.local.yaml")
    if vcenters_local.get("vcenters") is not None:
        merged.setdefault("vcenter", {})["vcenters"] = vcenters_local.get("vcenters") or []

    env = _environment(root)
    oracle = merged.setdefault("oracle", {})
    env_mapping = {
        "host": "ORACLE_HOST",
        "port": "ORACLE_PORT",
        "service_name": "ORACLE_SERVICE_NAME",
        "sid": "ORACLE_SID",
        "user": "ORACLE_USER",
        "password": "ORACLE_PASSWORD",
        "client_lib_dir": "ORACLE_CLIENT_LIB_DIR",
        "dsn": "ORACLE_DSN",
    }
    for config_key, env_key in env_mapping.items():
        if env.get(env_key):
            oracle[config_key] = env[env_key]

    database = merged.setdefault("database", {})
    mysql_cfg = database.setdefault("mysql", {})
    if env.get("ASSET_DB_ENGINE"):
        database["engine"] = env["ASSET_DB_ENGINE"].strip().lower()
    mysql_env = {
        "host": "MYSQL_HOST",
        "port": "MYSQL_PORT",
        "database": "MYSQL_DATABASE",
        "user": "MYSQL_USER",
        "password": "MYSQL_PASSWORD",
        "charset": "MYSQL_CHARSET",
    }
    for config_key, env_key in mysql_env.items():
        if env.get(env_key):
            mysql_cfg[config_key] = env[env_key]

    modules_cfg = merged.setdefault("modules", {})
    modules_auth = modules_cfg.setdefault("auth", {})
    if env.get("MODULE_SHARED_SECRET"):
        modules_auth["shared_secret"] = env["MODULE_SHARED_SECRET"]

    # 대메뉴는 config/modules/<id>.yaml 로도 추가할 수 있다. 같은 id 가 양쪽에 있으면
    # 파일 쪽이 이긴다. app_config.yaml 의 목록은 기존 항목 호환을 위해 남겨 둔다.
    file_modules = load_module_registry_files(root)
    if file_modules:
        existing = [item for item in (modules_cfg.get("registry") or []) if isinstance(item, Mapping)]
        from_files = {str(item.get("id") or "").strip() for item in file_modules}
        modules_cfg["registry"] = [
            item for item in existing if str(item.get("id") or "").strip() not in from_files
        ] + file_modules

    if env.get("ASSET_DEMO_MODE", "0") == "1":
        merged.setdefault("itsm", {})["collection_mode"] = "DEMO"
        merged.setdefault("vcenter", {})["collection_mode"] = "DEMO"

    app_cfg = merged["app"]
    return AppConfig(
        root_dir=root,
        timezone=str(app_cfg["timezone"]),
        sqlite_path=Path(app_cfg["sqlite_path"]),
        log_level=str(app_cfg["log_level"]),
        database=merged["database"],
        modules=merged["modules"],
        oracle=merged["oracle"],
        itsm=merged["itsm"],
        # Internal field name is retained to avoid a database/service migration.
        rvtools=merged["vcenter"],
        matching=merged["matching"],
        quality=merged["quality"],
        retention=merged["retention"],
        security=merged["security"],
        scheduler=merged["scheduler"],
    )
