from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from ..repositories import AssetRepository
from ..utils.hashing import stable_hash


class PeriodService:
    """Daily/weekly/monthly event totals, net change and replay verification."""

    def __init__(self, repository: AssetRepository) -> None:
        self.repo = repository

    def summary(self, source: str, start: str, end: str) -> dict[str, Any]:
        events = self.repo.changes(source=source, limit=100000, start=start, end=end)
        totals: dict[str, int] = defaultdict(int)
        for event in events:
            totals[event["event_type"]] += 1
        net = self._net_changes(events)
        replay = self.verify_replay(source, start, end)
        return {"source": source, "start": start, "end": end, "event_totals": dict(totals), "net_changes": net, "replay": replay}

    def daily(self, source: str, day: str) -> dict[str, Any]:
        start = datetime.fromisoformat(day).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.summary(source, start.isoformat(), (start + timedelta(days=1)).isoformat())

    def weekly(self, source: str, end_day: str) -> dict[str, Any]:
        end = datetime.fromisoformat(end_day).replace(hour=23, minute=59, second=59, microsecond=999999)
        start = end - timedelta(days=7) + timedelta(microseconds=1)
        return self.summary(source, start.isoformat(), end.isoformat())

    def monthly(self, source: str, year: int, month: int) -> dict[str, Any]:
        start = datetime(year, month, 1)
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
        return self.summary(source, start.isoformat(), end.isoformat())

    def verify_replay(self, source: str, start: str, end: str) -> dict[str, Any]:
        table = "itsm_asset_snapshot" if source == "ITSM" else "rv_asset_snapshot"
        key_column = "cm_id" if source == "ITSM" else "asset_key"
        start_snapshot = self.repo.conn.execute(
            "SELECT * FROM snapshot WHERE source=? AND collected_at<=? AND status IN ('SUCCESS','PARTIAL_SUCCESS') ORDER BY collected_at DESC LIMIT 1",
            (source, start),
        ).fetchone()
        end_snapshot = self.repo.conn.execute(
            "SELECT * FROM snapshot WHERE source=? AND collected_at<=? AND status IN ('SUCCESS','PARTIAL_SUCCESS') ORDER BY collected_at DESC LIMIT 1",
            (source, end),
        ).fetchone()
        if not start_snapshot or not end_snapshot:
            return {"status": "NO_BASELINE", "message": "기간 시작 또는 종료 정상 스냅샷이 없습니다."}
        start_rows = self.repo.conn.execute(f"SELECT * FROM {table} WHERE snapshot_id=?", (start_snapshot["id"],)).fetchall()
        end_rows = self.repo.conn.execute(f"SELECT * FROM {table} WHERE snapshot_id=?", (end_snapshot["id"],)).fetchall()
        if source == "ITSM":
            state = {row[key_column]: json.loads(row["raw_json"]) for row in start_rows}
            expected = {row[key_column]: json.loads(row["raw_json"]) for row in end_rows}
        else:
            state = {row[key_column]: self._rv_state(dict(row)) for row in start_rows}
            expected = {row[key_column]: self._rv_state(dict(row)) for row in end_rows}
        events = list(reversed(self.repo.changes(source=source, limit=100000, start=start, end=end)))
        for event in events:
            key, event_type, field = event["asset_key"], event["event_type"], event["field_name"]
            if event_type in {"ITSM_ASSET_CREATED", "RV_NEW"} and event["new_value"]:
                state[key] = json.loads(event["new_value"])
            elif event_type in {"ITSM_RECORD_REMOVED", "RV_REMOVED"}:
                state.pop(key, None)
            elif field and key in state and event["new_value"] is not None:
                value: Any = event["new_value"]
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
                state[key][field] = value
        matched = stable_hash(state) == stable_hash(expected)
        return {
            "status": "MATCHED" if matched else "MISMATCH",
            "start_snapshot_id": int(start_snapshot["id"]), "end_snapshot_id": int(end_snapshot["id"]),
            "replayed_count": len(state), "expected_count": len(expected),
        }


    @staticmethod
    def _rv_state(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "asset_key": row.get("asset_key"), "vm_uuid": row.get("vm_uuid"),
            "smbios_uuid": row.get("smbios_uuid"), "vm_id": row.get("vm_id"),
            "vcenter": row.get("vcenter"), "vm_name": row.get("vm_name"),
            "dns_name": row.get("dns_name"), "normalized_hostname": row.get("normalized_hostname"),
            "primary_ip": row.get("primary_ip"), "ip_addresses": json.loads(row.get("ip_json") or "[]"),
            "cpus": row.get("cpus"), "memory_mb": row.get("memory_mb"),
            "os_family": row.get("os_family"), "os_version": row.get("os_version"),
            "power_state": row.get("power_state"), "datacenter": row.get("datacenter"),
            "cluster_name": row.get("cluster_name"), "esxi_host": row.get("esxi_host"),
            "template_flag": row.get("template_flag"), "srm_placeholder": row.get("srm_placeholder"),
        }

    @staticmethod
    def _net_changes(events: list[dict[str, Any]]) -> dict[str, Any]:
        field_chains: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        created: set[str] = set()
        removed: set[str] = set()
        for event in sorted(events, key=lambda x: (x["detected_at"], x["id"])):
            if event["event_type"] in {"ITSM_ASSET_CREATED", "RV_NEW"}:
                created.add(event["asset_key"])
            elif event["event_type"] in {"ITSM_RECORD_REMOVED", "RV_REMOVED"}:
                removed.add(event["asset_key"])
            elif event.get("field_name"):
                field_chains[(event["asset_key"], event["field_name"])].append(event)
        changed = 0
        reverted = 0
        for chain in field_chains.values():
            if chain[0].get("old_value") == chain[-1].get("new_value"):
                reverted += 1
            else:
                changed += 1
        return {
            "created_net": len(created - removed), "removed_net": len(removed - created),
            "created_then_removed": len(created & removed), "changed_fields_net": changed,
            "reverted_fields": reverted,
        }
