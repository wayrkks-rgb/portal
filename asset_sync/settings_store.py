from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import load_config
from .utils.validation import validate_oracle_identifier

_ENV_KEYS = [
    "ORACLE_HOST", "ORACLE_PORT", "ORACLE_SERVICE_NAME", "ORACLE_SID",
    "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_CLIENT_LIB_DIR", "ORACLE_DSN",
]

_VALID_VCENTER_COLLECTION_MODES = {"DEMO", "FILE_ONLY", "POWERCLI"}
_VCENTER_COLLECTION_MODE_ALIASES = {
    "VCENTER": "POWERCLI",
    "VCENTER_ONLY": "POWERCLI",
    "POWERCLI_DIRECT": "POWERCLI",
    "DIRECT": "POWERCLI",
    "RVTOOLS": "FILE_ONLY",
    "RVTOOLS_ONLY": "FILE_ONLY",
}

_VALID_VCENTER_AUTH_MODES = {"PASS_THROUGH", "CREDENTIAL"}
_VCENTER_AUTH_MODE_ALIASES = {
    # Credential aliases used by earlier UI/config revisions.
    "ACCOUNT": "CREDENTIAL",
    "BASIC": "CREDENTIAL",
    "CREDENTIALS": "CREDENTIAL",
    "ID_PASSWORD": "CREDENTIAL",
    "PASSWORD": "CREDENTIAL",
    "USER_PASSWORD": "CREDENTIAL",
    "USERNAME_PASSWORD": "CREDENTIAL",
    "VCENTER": "CREDENTIAL",
    "VCENTER_ACCOUNT": "CREDENTIAL",
    "VCENTER_CREDENTIAL": "CREDENTIAL",
    "VCENTER_계정": "CREDENTIAL",
    "계정": "CREDENTIAL",
    # Windows integrated/pass-through aliases.
    "INTEGRATED": "PASS_THROUGH",
    "PASSTHROUGH": "PASS_THROUGH",
    "PASS_THRU": "PASS_THROUGH",
    "SSPI": "PASS_THROUGH",
    "WINDOWS": "PASS_THROUGH",
    "WINDOWS_ACCOUNT": "PASS_THROUGH",
    "WINDOWS_INTEGRATED": "PASS_THROUGH",
}

_VALID_VCENTER_AUTH_PROFILES = {"COMMON", "CUSTOM"}
_VCENTER_AUTH_PROFILE_ALIASES = {
    "DEFAULT": "COMMON",
    "GLOBAL": "COMMON",
    "SHARED": "COMMON",
    "공통": "COMMON",
    "INDIVIDUAL": "CUSTOM",
    "LOCAL": "CUSTOM",
    "PRIVATE": "CUSTOM",
    "VCENTER": "CUSTOM",
    "개별": "CUSTOM",
}


def _normalize_token(value: Any) -> str:
    return re.sub(r"_+", "_", str(value or "").strip().upper().replace("-", "_").replace(" ", "_"))


def _normalize_vcenter_auth_mode(value: Any, fallback: Any = "CREDENTIAL") -> str:
    """Normalize current and legacy authentication labels.

    Authentication labels are hidden from the simplified CRUD payload.  Old local
    YAML files can therefore contain values such as ``VCENTER_ACCOUNT`` or
    ``PASSTHROUGH``.  Those legacy labels must not block creation/deletion of an
    unrelated vCenter record.
    """
    normalized = _normalize_token(value)
    normalized = _VCENTER_AUTH_MODE_ALIASES.get(normalized, normalized)
    if normalized in _VALID_VCENTER_AUTH_MODES:
        return normalized
    # Be tolerant of descriptive labels from older localized screens.
    if any(token in normalized for token in ("PASS", "WINDOWS", "INTEGRATED", "SSPI")):
        return "PASS_THROUGH"
    if any(token in normalized for token in ("CREDENTIAL", "ACCOUNT", "PASSWORD", "VCENTER", "계정")):
        return "CREDENTIAL"
    fallback_mode = _normalize_token(fallback)
    fallback_mode = _VCENTER_AUTH_MODE_ALIASES.get(fallback_mode, fallback_mode)
    return fallback_mode if fallback_mode in _VALID_VCENTER_AUTH_MODES else "CREDENTIAL"


def _normalize_vcenter_auth_profile(item: dict[str, Any]) -> str:
    value = item.get("auth_profile")
    if value is None and any(key in item for key in ("auth_mode", "username", "password", "bypass_ssl_check")):
        return "CUSTOM"
    normalized = _normalize_token(value or "COMMON")
    normalized = _VCENTER_AUTH_PROFILE_ALIASES.get(normalized, normalized)
    return normalized if normalized in _VALID_VCENTER_AUTH_PROFILES else "CUSTOM"


def _normalize_vcenter_collection_mode(value: Any, fallback: Any = "POWERCLI") -> str:
    """Return a valid vCenter collection mode without blocking CRUD operations.

    Older local configuration files may contain a legacy alias or a blank value.
    vCenter create/update/delete must not fail because of that hidden technical field,
    so an invalid submitted value falls back to the existing valid mode and finally
    to POWERCLI.
    """
    normalized = str(value or "").strip().upper()
    normalized = _VCENTER_COLLECTION_MODE_ALIASES.get(normalized, normalized)
    if normalized in _VALID_VCENTER_COLLECTION_MODES:
        return normalized
    fallback_mode = str(fallback or "").strip().upper()
    fallback_mode = _VCENTER_COLLECTION_MODE_ALIASES.get(fallback_mode, fallback_mode)
    return fallback_mode if fallback_mode in _VALID_VCENTER_COLLECTION_MODES else "POWERCLI"


class SettingsValidationError(ValueError):
    pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, newline="\n") as stream:
        stream.write(text)
        temp_path = Path(stream.name)
    temp_path.replace(path)


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _read_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise SettingsValidationError(f"YAML 최상위 값은 객체여야 합니다: {path}")
    return loaded


def _quote_env(value: str) -> str:
    if not value:
        return ""
    if re.search(r"[\s#='\"]", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


class LocalSettingsStore:
    """Store local-only Oracle and PowerCLI/vCenter settings.

    Passwords are never returned through the public settings API. They are written
    only to local files excluded by .gitignore. Restrict those files with Windows ACLs.
    """

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root = (root_dir or Path(__file__).resolve().parents[1]).resolve()
        self.app_local_path = self.root / "config" / "app_config.local.yaml"
        self.vcenters_local_path = self.root / "config" / "vcenters.local.yaml"
        self.env_path = self.root / ".env"

    def public_settings(self) -> dict[str, Any]:
        cfg = load_config()
        vcenter = {
            "collection_mode": _normalize_vcenter_collection_mode(cfg.rvtools.get("collection_mode"), "POWERCLI"),
            "powershell_path": str(cfg.rvtools.get("powershell_path", "powershell.exe")),
            "script_path": str(cfg.rvtools.get("script_path", "scripts/collect_vcenter_inventory.ps1")),
            "incoming_dir": str(cfg.rvtools.get("incoming_dir", "data/incoming/vcenter")),
            "snapshot_dir": str(cfg.rvtools.get("snapshot_dir", "data/archive/vcenter")),
            "temp_dir": str(cfg.rvtools.get("temp_dir", "data/temp/powercli")),
            "export_xlsx": bool(cfg.rvtools.get("export_xlsx", True)),
            "hostname_suffixes": cfg.rvtools.get("hostname_suffixes", []),
            "timeout_seconds": int(cfg.rvtools.get("timeout_seconds", 1800)),
            "retry_count": int(cfg.rvtools.get("retry_count", 1)),
            "default_port": int(cfg.rvtools.get("default_port", 443)),
            "default_auth_mode": _normalize_vcenter_auth_mode(cfg.rvtools.get("default_auth_mode"), "CREDENTIAL"),
            "default_username": str(cfg.rvtools.get("default_username", "")),
            "default_password_configured": bool(cfg.rvtools.get("default_password")),
            "default_bypass_ssl_check": bool(cfg.rvtools.get("default_bypass_ssl_check", False)),
            "vcenters": [self._public_vcenter(item) for item in cfg.rvtools.get("vcenters", [])],
        }
        return {
            "oracle": {
                "enabled": bool(cfg.oracle.get("enabled", False)),
                "mode": str(cfg.oracle.get("mode", "thin")),
                "host": str(cfg.oracle.get("host", "")),
                "dsn": str(cfg.oracle.get("dsn", "")),
                "port": str(cfg.oracle.get("port", "")),
                "service_name": str(cfg.oracle.get("service_name", "")),
                "sid": str(cfg.oracle.get("sid", "")),
                "user": str(cfg.oracle.get("user", "")),
                "client_lib_dir": str(cfg.oracle.get("client_lib_dir", "")),
                "asset_source": str(cfg.oracle.get("asset_source", "")),
                "query_file": str(cfg.oracle.get("query_file", "config/oracle_query.local.sql")),
                "query_file_exists": cfg.resolve(
                    cfg.oracle.get("query_file", "config/oracle_query.local.sql")
                ).exists(),
                "password_configured": bool(cfg.oracle.get("password")),
            },
            "itsm": {
                "collection_mode": str(cfg.itsm.get("collection_mode", "ORACLE")),
                "incoming_dir": str(cfg.itsm.get("incoming_dir", "data/incoming/itsm")),
                "sheet_name": str(cfg.itsm.get("sheet_name", "Sheet1")),
                "header_row": int(cfg.itsm.get("header_row", 1)),
                "cpu_compare_field": str(cfg.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")),
                "memory_field": str(cfg.itsm.get("memory_field", "CM_MEMORY")),
                "memory_unit": str(cfg.itsm.get("memory_unit", "GB")),
                "os_eos_field": str(cfg.itsm.get("os_eos_field", "OS_EOS_DATE")),
            },
            "vcenter": vcenter,
            # Backward-compatible alias for older cached UI code.
            "rvtools": vcenter,
            "quality": {
                "rvtools_count_warning_ratio": float(cfg.quality.get("rvtools_count_warning_ratio", 0.70)),
                "rvtools_count_critical_ratio": float(cfg.quality.get("rvtools_count_critical_ratio", 0.30)),
                "itsm_count_warning_ratio": float(cfg.quality.get("itsm_count_warning_ratio", 0.70)),
                "itsm_count_critical_ratio": float(cfg.quality.get("itsm_count_critical_ratio", 0.30)),
            },
            "security": {
                "mask_ip_in_logs": bool(cfg.security.get("mask_ip_in_logs", True)),
                "mask_hostname_in_logs": bool(cfg.security.get("mask_hostname_in_logs", True)),
                "mask_user_id_in_logs": bool(cfg.security.get("mask_user_id_in_logs", True)),
                "allow_sensitive_export": bool(cfg.security.get("allow_sensitive_export", False)),
                "display_vcenter_server_in_logs": bool(cfg.security.get("display_vcenter_server_in_logs", False)),
            },
            "scheduler": {
                "daily_time": str(cfg.scheduler.get("daily_time", "07:00")),
                "task_name": str(cfg.scheduler.get("task_name", "AssetDailyCollection")),
            },
            "files": {
                "app_local_exists": self.app_local_path.exists(),
                "vcenters_local_exists": self.vcenters_local_path.exists(),
                "env_exists": self.env_path.exists(),
            },
        }

    @staticmethod
    def _public_vcenter(item: dict[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in item.items() if k not in {"password", "encrypted_password"}}
        result["auth_profile"] = _normalize_vcenter_auth_profile(item)
        result["auth_mode"] = _normalize_vcenter_auth_mode(result.get("auth_mode"), "CREDENTIAL")
        result["password_configured"] = bool(item.get("password") or item.get("encrypted_password"))
        return result

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        oracle = dict(payload.get("oracle") or {})
        itsm = dict(payload.get("itsm") or {})
        vcenter = dict(payload.get("vcenter") or payload.get("rvtools") or {})
        quality = dict(payload.get("quality") or {})
        security = dict(payload.get("security") or {})
        scheduler = dict(payload.get("scheduler") or {})
        vcenters = list(vcenter.pop("vcenters", payload.get("vcenters", [])) or [])

        current_cfg = load_config()
        existing_vcenters = {str(item.get("id")): item for item in current_cfg.rvtools.get("vcenters", []) if str(item.get("id", "")).strip()}
        submitted_default_password = str(vcenter.get("default_password", ""))
        if not submitted_default_password:
            submitted_default_password = str(current_cfg.rvtools.get("default_password", ""))
        vcenter["default_password"] = submitted_default_password
        vcenter["default_auth_mode"] = _normalize_vcenter_auth_mode(
            vcenter.get("default_auth_mode"), current_cfg.rvtools.get("default_auth_mode", "CREDENTIAL")
        )

        normalized_vcenters: list[dict[str, Any]] = []
        for item in vcenters:
            current = dict(item)
            vc_id = str(current.get("id", "")).strip()
            password = str(current.get("password", ""))
            if not password and vc_id in existing_vcenters:
                password = str(existing_vcenters[vc_id].get("password", existing_vcenters[vc_id].get("encrypted_password", "")))
            current["password"] = password
            current["auth_profile"] = _normalize_vcenter_auth_profile(current)
            current["auth_mode"] = _normalize_vcenter_auth_mode(current.get("auth_mode"), "CREDENTIAL")
            for key in ("password_configured", "encrypted_password_configured", "test_status", "test_message"):
                current.pop(key, None)
            normalized_vcenters.append(current)
        vcenters = normalized_vcenters

        self._validate_modes(itsm, vcenter)
        self._validate_vcenters(vcenters, vcenter)
        self._validate_time(str(scheduler.get("daily_time", "07:00")))

        local_yaml = {
            "oracle": {
                "enabled": bool(oracle.get("enabled", False)),
                "mode": str(oracle.get("mode", "thin")).lower(),
                "asset_source": str(oracle.get("asset_source", "")).strip(),
                "query_file": str(oracle.get("query_file", "config/oracle_query.local.sql")).strip(),
            },
            "itsm": {
                "collection_mode": str(itsm.get("collection_mode", "ORACLE")).upper(),
                "incoming_dir": str(itsm.get("incoming_dir", "data/incoming/itsm")).strip(),
                "sheet_name": str(itsm.get("sheet_name", "Sheet1")).strip(),
                "header_row": int(itsm.get("header_row", 1)),
                "cpu_compare_field": str(itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")).strip().upper(),
                "memory_field": str(itsm.get("memory_field", "CM_MEMORY")).strip().upper(),
                "memory_unit": str(itsm.get("memory_unit", "GB")).strip().upper(),
                "os_eos_field": str(itsm.get("os_eos_field", "OS_EOS_DATE")).strip().upper(),
            },
            "vcenter": {
                "collection_mode": str(vcenter.get("collection_mode", "POWERCLI")).upper(),
                "powershell_path": str(vcenter.get("powershell_path", "powershell.exe")).strip() or "powershell.exe",
                "script_path": str(vcenter.get("script_path", "scripts/collect_vcenter_inventory.ps1")).strip(),
                "incoming_dir": str(vcenter.get("incoming_dir", "data/incoming/vcenter")).strip(),
                "snapshot_dir": str(vcenter.get("snapshot_dir", "data/archive/vcenter")).strip(),
                "temp_dir": str(vcenter.get("temp_dir", "data/temp/powercli")).strip(),
                "export_xlsx": bool(vcenter.get("export_xlsx", True)),
                "hostname_suffixes": [str(v).strip().lower() for v in vcenter.get("hostname_suffixes", []) if str(v).strip()],
                "timeout_seconds": int(vcenter.get("timeout_seconds", 1800)),
                "retry_count": int(vcenter.get("retry_count", 1)),
                "default_port": int(vcenter.get("default_port", 443)),
                "default_auth_mode": _normalize_vcenter_auth_mode(vcenter.get("default_auth_mode"), "CREDENTIAL"),
                "default_username": str(vcenter.get("default_username", "")).strip(),
                "default_password": submitted_default_password,
                "default_bypass_ssl_check": bool(vcenter.get("default_bypass_ssl_check", False)),
            },
            "quality": {
                "rvtools_count_warning_ratio": float(quality.get("rvtools_count_warning_ratio", 0.70)),
                "rvtools_count_critical_ratio": float(quality.get("rvtools_count_critical_ratio", 0.30)),
                "itsm_count_warning_ratio": float(quality.get("itsm_count_warning_ratio", 0.70)),
                "itsm_count_critical_ratio": float(quality.get("itsm_count_critical_ratio", 0.30)),
            },
            "security": {
                "mask_ip_in_logs": bool(security.get("mask_ip_in_logs", True)),
                "mask_hostname_in_logs": bool(security.get("mask_hostname_in_logs", True)),
                "mask_user_id_in_logs": bool(security.get("mask_user_id_in_logs", True)),
                "allow_sensitive_export": bool(security.get("allow_sensitive_export", False)),
                "display_vcenter_server_in_logs": bool(security.get("display_vcenter_server_in_logs", False)),
            },
            "scheduler": {
                "daily_time": str(scheduler.get("daily_time", "07:00")),
                "task_name": str(scheduler.get("task_name", "AssetDailyCollection")).strip() or "AssetDailyCollection",
            },
        }
        _atomic_write(self.app_local_path, yaml.safe_dump(local_yaml, allow_unicode=True, sort_keys=False))
        _atomic_write(self.vcenters_local_path, yaml.safe_dump({"vcenters": vcenters}, allow_unicode=True, sort_keys=False))
        self._save_env(oracle)

        for relative in [
            local_yaml["itsm"]["incoming_dir"], local_yaml["vcenter"]["incoming_dir"],
            local_yaml["vcenter"]["snapshot_dir"], local_yaml["vcenter"]["temp_dir"],
            "data/export", "data/test/powercli", "logs",
        ]:
            path = Path(relative)
            if not path.is_absolute():
                path = self.root / path
            path.mkdir(parents=True, exist_ok=True)
        return self.public_settings()

    def save_oracle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save only Oracle/ITSM settings without validating vCenter settings."""
        oracle = dict(payload.get("oracle") or {})
        itsm = dict(payload.get("itsm") or {})
        current_cfg = load_config()
        validation_oracle = dict(current_cfg.oracle)
        validation_oracle.update(oracle)
        if not str(validation_oracle.get("password") or ""):
            validation_oracle["password"] = str(current_cfg.oracle.get("password") or "")
        validation_itsm = dict(current_cfg.itsm)
        validation_itsm.update(itsm)
        self._validate_oracle_section(
            validation_oracle,
            validation_itsm,
            require_connection=True,
            require_query=False,
        )

        local_yaml = _read_yaml_dict(self.app_local_path)
        local_yaml["oracle"] = {
            "enabled": bool(oracle.get("enabled", True)),
            "mode": str(oracle.get("mode", current_cfg.oracle.get("mode", "thin"))).lower(),
            # Query details are internal application settings. The UI only
            # updates connection values and must not erase these values.
            "asset_source": str(current_cfg.oracle.get("asset_source", "")).strip(),
            "query_file": str(current_cfg.oracle.get("query_file", "config/oracle_query.local.sql")).strip(),
        }
        local_yaml["itsm"] = {
            "collection_mode": "ORACLE",
            "incoming_dir": str(current_cfg.itsm.get("incoming_dir", "data/incoming/itsm")).strip(),
            "sheet_name": str(current_cfg.itsm.get("sheet_name", "Sheet1")).strip(),
            "header_row": int(current_cfg.itsm.get("header_row", 1)),
            "cpu_compare_field": str(current_cfg.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")).strip().upper(),
            "memory_field": str(current_cfg.itsm.get("memory_field", "CM_MEMORY")).strip().upper(),
            "memory_unit": str(current_cfg.itsm.get("memory_unit", "GB")).strip().upper(),
            "os_eos_field": str(current_cfg.itsm.get("os_eos_field", "OS_EOS_DATE")).strip().upper(),
        }
        _atomic_write(self.app_local_path, yaml.safe_dump(local_yaml, allow_unicode=True, sort_keys=False))
        self._save_env(oracle)
        incoming = Path(local_yaml["itsm"]["incoming_dir"])
        if not incoming.is_absolute():
            incoming = self.root / incoming
        incoming.mkdir(parents=True, exist_ok=True)
        return self.public_settings()

    def save_vcenter(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save only PowerCLI/vCenter settings without validating Oracle settings."""
        vcenter = dict(payload.get("vcenter") or payload.get("rvtools") or {})
        vcenters = list(vcenter.pop("vcenters", payload.get("vcenters", [])) or [])
        activate_powercli = bool(vcenter.pop("activate_powercli", False))
        current_cfg = load_config()
        current_vcenter = dict(current_cfg.rvtools or {})
        # The vCenter editor sends only the list of vCenters. Preserve hidden runtime
        # settings instead of resetting them or validating a stale legacy mode.
        runtime_keys = (
            "collection_mode", "powershell_path", "script_path", "incoming_dir",
            "snapshot_dir", "temp_dir", "export_xlsx", "hostname_suffixes",
            "timeout_seconds", "retry_count", "default_port", "default_auth_mode",
            "default_username", "default_password", "default_bypass_ssl_check",
        )
        for key in runtime_keys:
            if key not in vcenter and key in current_vcenter:
                vcenter[key] = current_vcenter[key]
        vcenter["collection_mode"] = (
            "POWERCLI"
            if activate_powercli
            else _normalize_vcenter_collection_mode(
                vcenter.get("collection_mode"), current_vcenter.get("collection_mode", "POWERCLI")
            )
        )
        vcenter["default_auth_mode"] = _normalize_vcenter_auth_mode(
            vcenter.get("default_auth_mode"), current_vcenter.get("default_auth_mode", "CREDENTIAL")
        )
        existing_vcenters = {
            str(item.get("id")): item
            for item in current_cfg.rvtools.get("vcenters", [])
            if str(item.get("id", "")).strip()
        }
        submitted_default_password = str(vcenter.get("default_password", "")) or str(current_cfg.rvtools.get("default_password", ""))
        vcenter["default_password"] = submitted_default_password

        normalized: list[dict[str, Any]] = []
        for raw in vcenters:
            item = dict(raw)
            vc_id = str(item.get("id", "")).strip()
            if not str(item.get("password", "")) and vc_id in existing_vcenters:
                item["password"] = str(existing_vcenters[vc_id].get("password") or existing_vcenters[vc_id].get("encrypted_password") or "")
            item["auth_profile"] = _normalize_vcenter_auth_profile(item)
            item["auth_mode"] = _normalize_vcenter_auth_mode(item.get("auth_mode"), "CREDENTIAL")
            for key in ("password_configured", "encrypted_password_configured", "test_status", "test_message"):
                item.pop(key, None)
            normalized.append(item)
        vcenters = normalized

        self._validate_modes({"collection_mode": "ORACLE"}, vcenter)
        self._validate_vcenters(vcenters, vcenter)
        local_yaml = _read_yaml_dict(self.app_local_path)
        local_yaml["vcenter"] = {
            "collection_mode": _normalize_vcenter_collection_mode(vcenter.get("collection_mode"), "POWERCLI"),
            "powershell_path": str(vcenter.get("powershell_path", "powershell.exe")).strip() or "powershell.exe",
            "script_path": str(vcenter.get("script_path", "scripts/collect_vcenter_inventory.ps1")).strip(),
            "incoming_dir": str(vcenter.get("incoming_dir", "data/incoming/vcenter")).strip(),
            "snapshot_dir": str(vcenter.get("snapshot_dir", "data/archive/vcenter")).strip(),
            "temp_dir": str(vcenter.get("temp_dir", "data/temp/powercli")).strip(),
            "export_xlsx": bool(vcenter.get("export_xlsx", True)),
            "hostname_suffixes": [str(v).strip().lower() for v in vcenter.get("hostname_suffixes", []) if str(v).strip()],
            "timeout_seconds": int(vcenter.get("timeout_seconds", 1800)),
            "retry_count": int(vcenter.get("retry_count", 1)),
            "default_port": int(vcenter.get("default_port", 443)),
            "default_auth_mode": _normalize_vcenter_auth_mode(vcenter.get("default_auth_mode"), "CREDENTIAL"),
            "default_username": str(vcenter.get("default_username", "")).strip(),
            "default_password": submitted_default_password,
            "default_bypass_ssl_check": bool(vcenter.get("default_bypass_ssl_check", False)),
        }
        _atomic_write(self.app_local_path, yaml.safe_dump(local_yaml, allow_unicode=True, sort_keys=False))
        _atomic_write(self.vcenters_local_path, yaml.safe_dump({"vcenters": vcenters}, allow_unicode=True, sort_keys=False))
        for relative in (
            local_yaml["vcenter"]["incoming_dir"],
            local_yaml["vcenter"]["snapshot_dir"],
            local_yaml["vcenter"]["temp_dir"],
            "data/test/powercli",
            "logs",
        ):
            path = Path(relative)
            if not path.is_absolute():
                path = self.root / path
            path.mkdir(parents=True, exist_ok=True)
        return self.public_settings()

    def save_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save scheduler/security settings independently."""
        scheduler = dict(payload.get("scheduler") or {})
        security = dict(payload.get("security") or {})
        self._validate_time(str(scheduler.get("daily_time", "07:00")))
        local_yaml = _read_yaml_dict(self.app_local_path)
        local_yaml["scheduler"] = {
            "daily_time": str(scheduler.get("daily_time", "07:00")),
            "task_name": str(scheduler.get("task_name", "AssetDailyCollection")).strip() or "AssetDailyCollection",
        }
        local_yaml["security"] = {
            "mask_ip_in_logs": bool(security.get("mask_ip_in_logs", True)),
            "mask_hostname_in_logs": bool(security.get("mask_hostname_in_logs", True)),
            "mask_user_id_in_logs": bool(security.get("mask_user_id_in_logs", True)),
            "allow_sensitive_export": bool(security.get("allow_sensitive_export", False)),
            "display_vcenter_server_in_logs": bool(security.get("display_vcenter_server_in_logs", False)),
        }
        _atomic_write(self.app_local_path, yaml.safe_dump(local_yaml, allow_unicode=True, sort_keys=False))
        return self.public_settings()

    def save_asset_source(
        self,
        asset_source: str,
        *,
        query_sql: str | None = None,
        query_file: str | None = None,
    ) -> dict[str, Any]:
        """Register the Oracle asset table and optionally write its query file.

        The connection editor deliberately never touches these two values, so this
        is the only path that lets the table browser install a working asset query.
        """
        source = str(asset_source or "").strip().upper()
        if not source:
            raise SettingsValidationError("자산 조회 대상 테이블을 선택하세요.")
        try:
            source = validate_oracle_identifier(source, "asset_source")
        except ValueError as exc:
            raise SettingsValidationError(str(exc)) from exc

        current_cfg = load_config()
        target_file = str(
            query_file or current_cfg.oracle.get("query_file") or "config/oracle_query.local.sql"
        ).strip()
        if Path(target_file).is_absolute():
            raise SettingsValidationError("자산 조회 SQL 경로는 프로젝트 상대경로여야 합니다.")
        resolved = (self.root / target_file).resolve()
        if not resolved.is_relative_to(self.root):
            raise SettingsValidationError("자산 조회 SQL 경로가 프로젝트 밖을 가리킵니다.")

        local_yaml = _read_yaml_dict(self.app_local_path)
        oracle_section = dict(local_yaml.get("oracle") or {})
        oracle_section.setdefault("enabled", bool(current_cfg.oracle.get("enabled", True)))
        oracle_section.setdefault("mode", str(current_cfg.oracle.get("mode", "thin")).lower())
        oracle_section["asset_source"] = source
        oracle_section["query_file"] = target_file
        local_yaml["oracle"] = oracle_section
        if query_sql is not None:
            _atomic_write(resolved, query_sql if query_sql.endswith("\n") else query_sql + "\n")
        _atomic_write(self.app_local_path, yaml.safe_dump(local_yaml, allow_unicode=True, sort_keys=False))
        return self.public_settings()

    def oracle_test_config(self, payload: dict[str, Any], *, require_query: bool = True):
        """Build an in-memory Oracle config from unsaved form input."""
        import copy

        oracle = dict(payload.get("oracle") or {})
        itsm = dict(payload.get("itsm") or {})
        current = load_config()
        cfg = copy.deepcopy(current)
        if not str(oracle.get("password") or ""):
            oracle["password"] = str(current.oracle.get("password") or "")
        cfg.oracle.update(oracle)
        cfg.itsm.update(itsm)
        self._validate_oracle_section(
            cfg.oracle,
            cfg.itsm,
            require_connection=True,
            require_query=require_query,
        )
        return cfg

    @staticmethod
    def _validate_oracle_section(
        oracle: dict[str, Any],
        itsm: dict[str, Any],
        *,
        require_connection: bool,
        require_query: bool,
    ) -> None:
        mode = str(oracle.get("mode", "thin")).lower()
        if mode not in {"thin", "thick"}:
            raise SettingsValidationError("Oracle 모드는 thin 또는 thick이어야 합니다.")
        collection_mode = str(itsm.get("collection_mode", "ORACLE")).upper()
        if collection_mode not in {"DEMO", "FILE_ONLY", "ORACLE"}:
            raise SettingsValidationError("ITSM 수집모드는 DEMO, FILE_ONLY, ORACLE 중 하나여야 합니다.")
        try:
            port = int(oracle.get("port") or 1521)
        except (TypeError, ValueError) as exc:
            raise SettingsValidationError("Oracle Port는 숫자여야 합니다.") from exc
        if not 1 <= port <= 65535:
            raise SettingsValidationError("Oracle Port 범위가 올바르지 않습니다.")
        if mode == "thick" and require_connection and not str(oracle.get("client_lib_dir") or "").strip():
            raise SettingsValidationError("Oracle Thick 모드에는 Client 경로가 필요합니다.")
        if not require_connection:
            return
        if not str(oracle.get("user") or "").strip():
            raise SettingsValidationError("Oracle 조회 계정을 입력하세요.")
        if not str(oracle.get("password") or ""):
            raise SettingsValidationError("Oracle 비밀번호를 입력하거나 저장된 비밀번호가 있어야 합니다.")
        dsn = str(oracle.get("dsn") or "").strip()
        host = str(oracle.get("host") or "").strip()
        service = str(oracle.get("service_name") or "").strip()
        sid = str(oracle.get("sid") or "").strip()
        if not dsn and not host:
            raise SettingsValidationError("Oracle Host/VIP 또는 DSN을 입력하세요.")
        if not dsn and not (service or sid):
            raise SettingsValidationError("Service Name 또는 SID를 입력하세요.")
        if not require_query:
            return
        if not str(oracle.get("query_file") or "").strip():
            raise SettingsValidationError("서버 내부의 Oracle 자산 조회 설정이 준비되지 않았습니다.")
        for label, key in (("CPU 컬럼", "cpu_compare_field"), ("Memory 컬럼", "memory_field"), ("EOS 컬럼", "os_eos_field")):
            if not str(itsm.get(key) or "").strip():
                raise SettingsValidationError(f"서버 내부 {label} 설정이 준비되지 않았습니다.")

    def _save_env(self, oracle: dict[str, Any]) -> None:
        existing = _read_env(self.env_path)
        mapping = {
            "ORACLE_HOST": str(oracle.get("host", "")).strip(),
            "ORACLE_PORT": str(oracle.get("port", "")).strip(),
            "ORACLE_SERVICE_NAME": str(oracle.get("service_name", "")).strip(),
            "ORACLE_SID": str(oracle.get("sid", "")).strip(),
            "ORACLE_USER": str(oracle.get("user", "")).strip(),
            "ORACLE_CLIENT_LIB_DIR": str(oracle.get("client_lib_dir", "")).strip(),
        }
        password = str(oracle.get("password", ""))
        mapping["ORACLE_PASSWORD"] = password or existing.get("ORACLE_PASSWORD", "")
        if "dsn" in oracle:
            mapping["ORACLE_DSN"] = str(oracle.get("dsn", "")).strip()
        else:
            mapping["ORACLE_DSN"] = existing.get("ORACLE_DSN", "")
        lines = ["# Local-only connection values. Do not copy outside the closed network."]
        for key in _ENV_KEYS:
            lines.append(f"{key}={_quote_env(mapping.get(key, existing.get(key, '')))}")
        _atomic_write(self.env_path, "\n".join(lines) + "\n")

    @staticmethod
    def _validate_modes(itsm: dict[str, Any], vcenter: dict[str, Any]) -> None:
        if str(itsm.get("collection_mode", "ORACLE")).upper() not in {"DEMO", "FILE_ONLY", "ORACLE"}:
            raise SettingsValidationError("ITSM collection_mode는 DEMO, FILE_ONLY, ORACLE 중 하나여야 합니다.")
        if str(vcenter.get("collection_mode", "POWERCLI")).upper() not in {"DEMO", "FILE_ONLY", "POWERCLI"}:
            raise SettingsValidationError("vCenter collection_mode는 DEMO, FILE_ONLY, POWERCLI 중 하나여야 합니다.")

    @staticmethod
    def _validate_vcenters(vcenters: list[dict[str, Any]], vcenter: dict[str, Any]) -> None:
        ids: set[str] = set()
        servers: set[str] = set()
        default_auth_mode = _normalize_vcenter_auth_mode(vcenter.get("default_auth_mode"), "CREDENTIAL")
        default_port = int(vcenter.get("default_port", 443))
        if not 1 <= default_port <= 65535:
            raise SettingsValidationError("vCenter 공통 포트 범위가 올바르지 않습니다.")
        for index, item in enumerate(vcenters, start=1):
            vc_id = str(item.get("id", "")).strip()
            if not vc_id:
                raise SettingsValidationError(f"{index}번째 vCenter id가 없습니다.")
            if vc_id in ids:
                raise SettingsValidationError(f"vCenter id가 중복되었습니다: {vc_id}")
            ids.add(vc_id)
            server = str(item.get("server", "")).strip()
            if bool(item.get("enabled", True)) and not server:
                raise SettingsValidationError(f"활성 vCenter 주소가 없습니다: {vc_id}")
            if server.lower() in servers:
                raise SettingsValidationError(f"vCenter 주소가 중복되었습니다: {server}")
            if server:
                servers.add(server.lower())
            profile = _normalize_vcenter_auth_profile(item)
            auth_mode = default_auth_mode if profile == "COMMON" else _normalize_vcenter_auth_mode(item.get("auth_mode"), "CREDENTIAL")
            username = str(vcenter.get("default_username", "")) if profile == "COMMON" else str(item.get("username", ""))
            password = str(vcenter.get("default_password", "")) if profile == "COMMON" else str(item.get("password", ""))
            if auth_mode not in {"PASS_THROUGH", "CREDENTIAL"}:
                raise SettingsValidationError(f"지원하지 않는 vCenter 인증방식입니다: {vc_id}")
            if auth_mode == "CREDENTIAL" and (not username.strip() or not password):
                raise SettingsValidationError(f"계정 인증 방식의 사용자명/비밀번호가 없습니다: {vc_id}")
            port = int(item.get("port") or default_port)
            if not 1 <= port <= 65535:
                raise SettingsValidationError(f"vCenter 포트 범위가 올바르지 않습니다: {vc_id}")

    @staticmethod
    def _validate_time(value: str) -> None:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise SettingsValidationError("실행 시각은 HH:MM 형식이어야 합니다.")
