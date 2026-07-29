from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from ..config import AppConfig
from ..normalization import memory_to_mb, normalize_bool, normalize_hostname, normalize_int, normalize_os, split_ips
from ..normalization.code_maps import OS_CODES
from ..repositories import AssetRepository
from ..utils.hashing import stable_hash

LOGGER = logging.getLogger(__name__)


class SnapshotValidationError(RuntimeError):
    pass


class SnapshotService:
    """Normalize and persist ITSM/vCenter source records."""

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def normalize_itsm(self, raw_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        eos_field = str(self.config.itsm.get("os_eos_field", "OS_EOS_DATE")).upper()
        cpu_field = str(self.config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")).upper()
        memory_field = str(self.config.itsm.get("memory_field", "CM_MEMORY")).upper()
        suffixes = self.config.rvtools.get("hostname_suffixes", [])
        memory_unit = str(self.config.itsm.get("memory_unit", "GB"))
        normalized: list[dict[str, Any]] = []
        duplicates: set[str] = set()
        seen: set[str] = set()
        empty_ids = 0

        for raw in raw_records:
            row = {str(k).upper(): v for k, v in raw.items()}
            cm_id = str(row.get("CM_ID") or "").strip()
            if not cm_id:
                empty_ids += 1
                continue
            if cm_id in seen:
                duplicates.add(cm_id)
                continue
            seen.add(cm_id)
            ips = split_ips([row.get("CM_IP"), row.get("CM_SUB_IP")])
            raw_os = OS_CODES.get(str(row.get("CM_OS") or "").strip(), row.get("CM_OS"))
            os_text = " ".join(str(v) for v in [raw_os, row.get("CM_OS_VERSION")] if v not in (None, ""))
            os_family, os_version = normalize_os(os_text)
            item: dict[str, Any] = dict(row)
            item.update(
                {
                    "CM_ID": cm_id,
                    "normalized_hostname": normalize_hostname(row.get("CM_HOSTNAME"), suffixes),
                    "ip_addresses": ips,
                    "primary_ip": ips[0] if ips else None,
                    "cpu_cores": normalize_int(row.get(cpu_field)),
                    "memory_mb": memory_to_mb(row.get(memory_field), memory_unit),
                    "os_family": os_family,
                    "os_version_normalized": os_version,
                    "eos_value": None if row.get(eos_field) is None else str(row.get(eos_field)).strip(),
                    "raw": row,
                }
            )
            item["record_hash"] = stable_hash({k: v for k, v in item.items() if k not in {"record_hash", "raw"}})
            normalized.append(item)

        minimum = int(self.config.quality.get("minimum_itsm_records", 1))
        if len(normalized) < minimum:
            raise SnapshotValidationError(f"ITSM 정상 레코드가 최소 기준({minimum}) 미만입니다: {len(normalized)}")
        if duplicates:
            raise SnapshotValidationError(f"ITSM CM_ID 중복이 있습니다: {', '.join(sorted(duplicates)[:20])}")
        return normalized, {"empty_ids": empty_ids, "duplicates": sorted(duplicates)}

    def normalize_rvtools(self, raw_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        suffixes = self.config.rvtools.get("hostname_suffixes", [])
        rv_cpu_field = str(self.config.rvtools.get("cpu_compare_field", "CPUs"))
        rv_memory_field = str(self.config.rvtools.get("memory_compare_field", "Memory"))
        normalized: list[dict[str, Any]] = []
        duplicates: dict[str, list[str]] = {}
        seen: dict[str, str] = {}

        for raw in raw_records:
            vcenter = str(raw.get("VI SDK Server") or raw.get("_vcenter_scope") or "UNKNOWN").strip()
            vm_uuid = self._clean_id(raw.get("VM UUID"))
            smbios_uuid = self._clean_id(raw.get("SMBIOS UUID"))
            vm_id = self._clean_id(raw.get("VM ID"))
            vm_name = str(raw.get("VM") or "").strip()
            asset_key = vm_uuid or smbios_uuid or (f"{vcenter}|{vm_id}" if vm_id else None) or (f"{vcenter}|{vm_name.lower()}" if vm_name else None)
            if not asset_key:
                continue
            if asset_key in seen:
                duplicates.setdefault(asset_key, [seen[asset_key]]).append(str(raw.get("_source_file", "")))
                continue
            seen[asset_key] = str(raw.get("_source_file", ""))

            ips = split_ips([raw.get("Primary IP Address"), *[raw.get(f"Network #{i}") for i in range(1, 9)]])
            dns_name = raw.get("DNS Name")
            hostname = normalize_hostname(dns_name, suffixes) or normalize_hostname(vm_name, suffixes)
            os_source = raw.get("OS according to the VMware Tools") or raw.get("OS according to the configuration file")
            os_family, os_version = normalize_os(os_source)
            item = {
                "asset_key": asset_key,
                "vm_uuid": vm_uuid,
                "smbios_uuid": smbios_uuid,
                "vm_id": vm_id,
                "vcenter": vcenter,
                "vm_name": vm_name or None,
                "dns_name": None if dns_name is None else str(dns_name).strip(),
                "normalized_hostname": hostname,
                "ip_addresses": ips,
                "primary_ip": ips[0] if ips else None,
                "cpus": normalize_int(raw.get(rv_cpu_field)),
                "memory_mb": memory_to_mb(raw.get(rv_memory_field), "MB"),
                "os_family": os_family,
                "os_version": os_version,
                "power_state": str(raw.get("Powerstate") or "").strip().lower(),
                "datacenter": self._text(raw.get("Datacenter")),
                "cluster": self._text(raw.get("Cluster")),
                "esxi_host": self._text(raw.get("Host")),
                "template_flag": bool(normalize_bool(raw.get("Template"))),
                "srm_placeholder": bool(normalize_bool(raw.get("SRM Placeholder"))),
                "raw": raw,
            }
            item["record_hash"] = stable_hash({k: v for k, v in item.items() if k not in {"record_hash", "raw"}})
            normalized.append(item)

        minimum = int(self.config.quality.get("minimum_rvtools_records", 1))
        if len(normalized) < minimum:
            raise SnapshotValidationError(f"vCenter 정상 레코드가 최소 기준({minimum}) 미만입니다: {len(normalized)}")
        return normalized, {"duplicates": duplicates}

    def save_itsm_snapshot(self, run_id: int, records: list[dict[str, Any]], collected_at: datetime, status: str = "SUCCESS") -> int:
        checksum = stable_hash({r["CM_ID"]: r["record_hash"] for r in records})
        snapshot_id = self.repo.create_snapshot("ITSM", collected_at.date().isoformat(), collected_at.isoformat(), run_id, status, len(records), checksum)
        self.repo.insert_itsm_records(snapshot_id, records)
        return snapshot_id

    def save_rv_snapshot(self, run_id: int, records: list[dict[str, Any]], collected_at: datetime, status: str = "SUCCESS") -> int:
        checksum = stable_hash({r["asset_key"]: r["record_hash"] for r in records})
        snapshot_id = self.repo.create_snapshot("RVTOOLS", collected_at.date().isoformat(), collected_at.isoformat(), run_id, status, len(records), checksum)
        self.repo.insert_rv_records(snapshot_id, records)
        return snapshot_id

    @staticmethod
    def _clean_id(value: object) -> str | None:
        text = "" if value is None else str(value).strip().lower()
        return None if text in {"", "-", "nan", "none", "null"} else text

    @staticmethod
    def _text(value: object) -> str | None:
        text = "" if value is None else str(value).strip()
        return text or None
