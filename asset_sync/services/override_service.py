from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from ..config import AppConfig
from ..normalization import memory_to_mb, normalize_hostname, normalize_int, normalize_os, split_ips
from ..normalization.code_maps import OS_CODES
from ..repositories import AssetRepository


class OverrideService:
    """Apply approved SQLite overrides without updating the Oracle source."""

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def apply(self, records: dict[str, dict[str, Any]], as_of: date | None = None) -> dict[str, dict[str, Any]]:
        as_of = as_of or date.today()
        rows = self.repo.conn.execute(
            """
            SELECT * FROM manual_asset_override
             WHERE approval_status='APPROVED'
               AND (valid_from IS NULL OR valid_from='' OR date(valid_from) <= date(?))
               AND (valid_to IS NULL OR valid_to='' OR date(valid_to) >= date(?))
             ORDER BY approved_at, id
            """,
            (as_of.isoformat(), as_of.isoformat()),
        ).fetchall()
        result = {key: deepcopy(value) for key, value in records.items()}
        for row in rows:
            cm_id = row["cm_id"]
            if cm_id not in result:
                continue
            field = row["field_name"]
            value = row["override_value"]
            result[cm_id]["raw"][field] = value
            self._refresh_normalized(result[cm_id], field)
        return result

    def _refresh_normalized(self, record: dict[str, Any], field: str) -> None:
        raw = record["raw"]
        suffixes = self.config.rvtools.get("hostname_suffixes", [])
        if field == "CM_HOSTNAME":
            record["normalized_hostname"] = normalize_hostname(raw.get(field), suffixes)
        elif field in {"CM_IP", "CM_SUB_IP"}:
            ips = split_ips([raw.get("CM_IP"), raw.get("CM_SUB_IP")])
            record["ip_addresses"] = ips
            record["primary_ip"] = ips[0] if ips else None
        elif field in {str(self.config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")), "CM_CPU_CNT"}:
            cpu_field = str(self.config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT"))
            record["cpu_cores"] = normalize_int(raw.get(cpu_field))
        elif field == str(self.config.itsm.get("memory_field", "CM_MEMORY")):
            record["memory_mb"] = memory_to_mb(raw.get(field), str(self.config.itsm.get("memory_unit", "GB")))
        elif field in {"CM_OS", "CM_OS_VERSION"}:
            raw_os = OS_CODES.get(str(raw.get("CM_OS") or "").strip(), raw.get("CM_OS"))
            family, version = normalize_os(f"{raw_os or ''} {raw.get('CM_OS_VERSION') or ''}")
            record["os_family"] = family
            record["os_version"] = version
        elif field == "CM_STA_CD":
            record["status_code"] = value_or_none(raw.get(field))
        elif field == "CM_SVR_CAT_CD":
            record["server_category_code"] = value_or_none(raw.get(field))
        elif field == "CM_OWN_CAT_CD":
            record["environment_code"] = value_or_none(raw.get(field))
        elif field == str(self.config.itsm.get("os_eos_field", "OS_EOS_DATE")):
            record["eos_value"] = value_or_none(raw.get(field))


def value_or_none(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None
