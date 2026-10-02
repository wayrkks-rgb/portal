from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..config import AppConfig
from ..repositories import AssetRepository
from ..utils.hashing import canonical_json


class DiffService:
    """Generate source-specific change events between normal snapshots."""

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def compare_itsm(self, snapshot_id: int) -> dict[str, Any]:
        previous = self.repo.latest_snapshot("ITSM", before_snapshot_id=snapshot_id)
        if previous is None:
            return {"status": "NO_BASELINE", "snapshot_id": snapshot_id, "events": []}
        result = self.compare_pair("ITSM", snapshot_id, int(previous["id"]))
        self.repo.replace_change_events(snapshot_id, result["events"])
        return result

    def compare_rvtools(self, snapshot_id: int) -> dict[str, Any]:
        current_snapshot = self.repo.snapshot_by_id(snapshot_id)
        previous = self.repo.latest_snapshot("RVTOOLS", before_snapshot_id=snapshot_id)
        if previous is None:
            return {"status": "NO_BASELINE", "snapshot_id": snapshot_id, "events": []}
        result = self.compare_pair("RVTOOLS", snapshot_id, int(previous["id"]))
        self.repo.replace_change_events(snapshot_id, result["events"])
        return result

    def compare_pair(self, source: str, current_snapshot_id: int, previous_snapshot_id: int) -> dict[str, Any]:
        """Compare an explicit snapshot pair without persisting change events.

        The regular collection path compares consecutive successful runs. The daily screen uses
        this method to compare the latest snapshot with the latest snapshot from an earlier date.
        """
        source = source.upper()
        current_snapshot = self.repo.snapshot_by_id(current_snapshot_id)
        previous_snapshot = self.repo.snapshot_by_id(previous_snapshot_id)
        if current_snapshot is None or previous_snapshot is None:
            raise ValueError("비교할 스냅샷을 찾을 수 없습니다.")
        if str(current_snapshot["source"]).upper() != source or str(previous_snapshot["source"]).upper() != source:
            raise ValueError("스냅샷 source가 비교 요청과 일치하지 않습니다.")
        detected_at = str(current_snapshot["collected_at"])
        if source == "ITSM":
            current = self.repo.load_itsm_records(current_snapshot_id)
            old = self.repo.load_itsm_records(previous_snapshot_id)
            events = self._itsm_events(old, current, previous_snapshot_id, detected_at)
        elif source == "RVTOOLS":
            current = self.repo.load_rv_records(current_snapshot_id)
            old = self.repo.load_rv_records(previous_snapshot_id)
            run = self.repo.conn.execute(
                "SELECT * FROM collection_run WHERE id=?",
                (current_snapshot["collection_run_id"],),
            ).fetchone()
            successful_scopes = set(json.loads(run["success_scope_json"] or "[]")) if run else set()
            events = self._rv_events(old, current, previous_snapshot_id, detected_at, successful_scopes)
        else:
            raise ValueError("source는 ITSM 또는 RVTOOLS여야 합니다.")
        return {
            "status": "SUCCESS",
            "snapshot_id": current_snapshot_id,
            "previous_snapshot_id": previous_snapshot_id,
            "events": events,
        }

    def _itsm_events(
        self,
        old: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        previous_snapshot_id: int,
        detected_at: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        old_keys, new_keys = set(old), set(current)
        for key in sorted(new_keys - old_keys):
            events.append(self._event("ITSM", key, "ITSM_ASSET_CREATED", previous_snapshot_id, detected_at, new_value=canonical_json(current[key]["raw"])))
        for key in sorted(old_keys - new_keys):
            events.append(self._event("ITSM", key, "ITSM_RECORD_REMOVED", previous_snapshot_id, detected_at, old_value=canonical_json(old[key]["raw"])))

        tracked = list(self.config.itsm.get("tracked_fields", []))
        eos_field = str(self.config.itsm.get("os_eos_field", "OS_EOS_DATE"))
        cpu_field = str(self.config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT"))
        memory_field = str(self.config.itsm.get("memory_field", "CM_MEMORY"))
        for required_field in (eos_field, cpu_field, memory_field):
            if required_field not in tracked:
                tracked.append(required_field)
        ignored = set(self.config.itsm.get("ignore_fields", []))
        tracked = [f for f in tracked if f not in ignored]
        event_map = {
            "CM_IP": "ITSM_IP_CHANGED", "CM_SUB_IP": "ITSM_IP_CHANGED", "CM_HOSTNAME": "ITSM_HOSTNAME_CHANGED",
            cpu_field: "ITSM_CPU_CHANGED", "CM_CPU_CNT": "ITSM_CPU_CHANGED", memory_field: "ITSM_MEMORY_CHANGED",
            "CM_OS": "ITSM_OS_CHANGED", "CM_OS_VERSION": "ITSM_OS_CHANGED", eos_field: "ITSM_EOS_CHANGED",
            "CM_OWN_EMP_ID": "ITSM_OWNER_CHANGED", "CM_WOR_MNG_EMP_ID": "ITSM_OWNER_CHANGED",
            "CM_OWN_DPT_ID": "ITSM_DEPARTMENT_CHANGED", "CM_SVR_CAT_CD": "ITSM_SERVER_CATEGORY_CHANGED",
            "CM_OWN_CAT_CD": "ITSM_ENVIRONMENT_CHANGED", "CM_STA_CD": "ITSM_STATUS_CHANGED",
            "CM_PLACE": "ITSM_LOCATION_CHANGED", "CM_RACK_LOC": "ITSM_LOCATION_CHANGED",
        }
        active = {"CMSTA010", "CMSTA050"}
        inactive = {"CMSTA020", "CMSTA060"}
        for key in sorted(old_keys & new_keys):
            before, after = old[key]["raw"], current[key]["raw"]
            old_status, new_status = str(before.get("CM_STA_CD") or ""), str(after.get("CM_STA_CD") or "")
            if old_status in active and new_status == "CMSTA020":
                events.append(self._event("ITSM", key, "ITSM_STATUS_TO_UNUSED", previous_snapshot_id, detected_at, "CM_STA_CD", old_status, new_status))
            elif old_status in active and new_status == "CMSTA060":
                events.append(self._event("ITSM", key, "ITSM_STATUS_TO_DISPOSED", previous_snapshot_id, detected_at, "CM_STA_CD", old_status, new_status))
            elif old_status in inactive and new_status in active:
                events.append(self._event("ITSM", key, "ITSM_ASSET_REACTIVATED", previous_snapshot_id, detected_at, "CM_STA_CD", old_status, new_status))

            field_events = []
            for field in tracked:
                old_value = self._comparable(before.get(field))
                new_value = self._comparable(after.get(field))
                if field in {"CM_IP", "CM_SUB_IP"}:
                    old_value = sorted(self._to_ip_set(before.get(field)))
                    new_value = sorted(self._to_ip_set(after.get(field)))
                if old_value == new_value:
                    continue
                field_events.append(self._event(
                    "ITSM", key, event_map.get(field, "ITSM_FIELD_CHANGED"), previous_snapshot_id,
                    detected_at, field, self._serialize(old_value), self._serialize(new_value), group_key=f"ITSM|{key}|{detected_at}",
                ))
            if field_events:
                events.append(self._event("ITSM", key, "ITSM_ASSET_UPDATED", previous_snapshot_id, detected_at, group_key=f"ITSM|{key}|{detected_at}", metadata={"field_count": len(field_events)}))
                events.extend(field_events)
        return events

    def _rv_events(
        self,
        old: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        previous_snapshot_id: int,
        detected_at: str,
        successful_scopes: set[str],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        old_keys, new_keys = set(old), set(current)
        added, removed = new_keys - old_keys, old_keys - new_keys

        # 같은 VM 인데 키만 바뀐 것을 먼저 이어 붙인다.
        #
        # asset_key 는 vm_uuid → smbios_uuid → vCenter|MoRef → vCenter|이름 순으로
        # 고른다. 그래서 어느 날 vCenter 가 Config 를 돌려주지 않으면(VM 이 잠깐
        # 접근 불가 상태일 때 그렇다) uuid 가 비고 키가 MoRef 로 떨어진다. 다음
        # 날 돌아오면 다시 uuid 가 된다. 그러면 삭제·생성이 한 쌍씩 생기는데
        # 실제로는 아무 일도 없었다.
        relinked = self._relink(old, current, added, removed)
        for new_key, old_key in relinked.items():
            added.discard(new_key)
            removed.discard(old_key)
            events.append(self._event(
                "RVTOOLS", new_key, "RV_KEY_CHANGED", previous_snapshot_id, detected_at,
                "asset_key", old_key, new_key,
                metadata={"vm_name": current[new_key].get("vm_name"),
                          "reason": "식별자가 바뀌어 같은 VM 으로 이어 붙였습니다."},
            ))

        for key in sorted(added):
            events.append(self._event("RVTOOLS", key, "RV_NEW", previous_snapshot_id, detected_at, new_value=canonical_json(self._rv_replay_record(current[key]))))
        for key in sorted(removed):
            old_scope = str(old[key].get("vcenter") or "")
            if successful_scopes and old_scope not in successful_scopes:
                events.append(self._event("RVTOOLS", key, "COLLECTION_GAP", previous_snapshot_id, detected_at, metadata={"vcenter": old_scope}))
            else:
                events.append(self._event("RVTOOLS", key, "RV_REMOVED", previous_snapshot_id, detected_at, old_value=canonical_json(self._rv_replay_record(old[key]))))

        fields = {
            "power_state": ("RV_POWER_ON", "RV_POWER_OFF"),
            "primary_ip": "RV_IP_CHANGED", "normalized_hostname": "RV_DNS_CHANGED", "vm_name": "RV_VM_NAME_CHANGED",
            "cpus": "RV_CPU_CHANGED", "memory_mb": "RV_MEMORY_CHANGED", "os_family": "RV_OS_CHANGED",
            "os_version": "RV_OS_CHANGED", "esxi_host": "RV_HOST_CHANGED", "vcenter": "RV_VCENTER_CHANGED",
        }
        # 키가 바뀐 짝도 항목별 변경을 봐야 한다. 그 사이에 CPU 가 늘었을 수 있다.
        pairs = [(key, key) for key in sorted(old_keys & new_keys)]
        pairs.extend(sorted(relinked.items(), key=lambda item: item[0]))
        for new_key, old_key in pairs:
            key = new_key
            before, after = old[old_key], current[new_key]
            for field, event_type in fields.items():
                old_value, new_value = before.get(field), after.get(field)
                if old_value == new_value:
                    continue
                resolved_type = event_type
                if field == "power_state":
                    resolved_type = event_type[0] if str(new_value).lower() == "poweredon" else event_type[1]
                events.append(self._event("RVTOOLS", key, str(resolved_type), previous_snapshot_id, detected_at, field, self._serialize(old_value), self._serialize(new_value)))
        return events


    #: VM 을 알아보는 데 쓸 수 있는 값. 하나라도 같으면 같은 VM 으로 본다.
    #: 이름은 혼자서는 쓰지 않는다 -- 지우고 같은 이름으로 다시 만드는 일이 있다.
    _RV_IDENTITY_FIELDS = ("vm_uuid", "smbios_uuid", "vm_id")

    @classmethod
    def _relink(
        cls,
        old: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        added: set[str],
        removed: set[str],
    ) -> dict[str, str]:
        """없어진 키와 새로 생긴 키가 같은 VM 인지 가린다.

        {새 키: 옛 키}. 식별자(uuid·MoRef) 를 하나라도 같이 갖고 있고 **같은
        vCenter** 에 있을 때만 이어 붙인다. 식별자가 전부 비어 있으면 이어 붙이지
        않는다 -- 그때는 정말로 모르는 것이고, 모르는 것을 같다고 하면 실제 생성·
        삭제를 놓친다.
        """
        if not added or not removed:
            return {}

        index: dict[tuple[str, str, str], str] = {}
        for key in removed:
            record = old[key]
            scope = str(record.get("vcenter") or "")
            for field in cls._RV_IDENTITY_FIELDS:
                value = str(record.get(field) or "").strip().lower()
                if value:
                    index.setdefault((field, scope, value), key)

        linked: dict[str, str] = {}
        taken: set[str] = set()
        for key in sorted(added):
            record = current[key]
            scope = str(record.get("vcenter") or "")
            for field in cls._RV_IDENTITY_FIELDS:
                value = str(record.get(field) or "").strip().lower()
                if not value:
                    continue
                candidate = index.get((field, scope, value))
                if candidate and candidate not in taken:
                    linked[key] = candidate
                    taken.add(candidate)
                    break
        return linked

    @staticmethod
    def _rv_replay_record(record: dict[str, Any]) -> dict[str, Any]:
        fields = [
            "asset_key", "vm_uuid", "smbios_uuid", "vm_id", "vcenter", "vm_name", "dns_name",
            "normalized_hostname", "primary_ip", "ip_addresses", "cpus", "memory_mb", "os_family",
            "os_version", "power_state", "datacenter", "cluster_name", "esxi_host",
            "template_flag", "srm_placeholder",
        ]
        return {field: record.get(field) for field in fields}

    @staticmethod
    def _event(
        source: str,
        asset_key: str,
        event_type: str,
        previous_snapshot_id: int,
        detected_at: str,
        field_name: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        group_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "source": source, "asset_key": asset_key, "event_type": event_type,
            "previous_snapshot_id": previous_snapshot_id, "detected_at": detected_at,
            "field_name": field_name, "old_value": old_value, "new_value": new_value,
            "group_key": group_key, "metadata": metadata or {},
        }

    @staticmethod
    def _comparable(value: Any) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
            text = text[:-2]
        return text

    @staticmethod
    def _to_ip_set(value: Any) -> set[str]:
        from ..normalization.ip import split_ips
        return set(split_ips([value]))

    @staticmethod
    def _serialize(value: Any) -> str | None:
        if value is None:
            return None
        return canonical_json(value) if isinstance(value, (dict, list, tuple, set)) else str(value)
