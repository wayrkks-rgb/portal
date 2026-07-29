from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from ..config import AppConfig
from ..normalization.numeric import memory_to_mb, normalize_int
from ..repositories import AssetRepository
from ..utils.hashing import canonical_json


class ChangeSyncService:
    """Link vCenter and ITSM change events and determine update synchronization."""

    RV_CONCEPT = {
        "RV_NEW": "CREATED", "RV_REMOVED": "REMOVED", "RV_CPU_CHANGED": "CPU",
        "RV_MEMORY_CHANGED": "MEMORY", "RV_IP_CHANGED": "IP", "RV_DNS_CHANGED": "HOSTNAME",
        "RV_VM_NAME_CHANGED": "HOSTNAME", "RV_OS_CHANGED": "OS", "RV_POWER_ON": "POWER",
        "RV_POWER_OFF": "POWER", "RV_HOST_CHANGED": "LOCATION", "RV_VCENTER_CHANGED": "LOCATION",
    }
    ITSM_CONCEPT = {
        "ITSM_ASSET_CREATED": "CREATED", "ITSM_ASSET_REACTIVATED": "CREATED",
        "ITSM_STATUS_TO_UNUSED": "REMOVED", "ITSM_STATUS_TO_DISPOSED": "REMOVED",
        "ITSM_RECORD_REMOVED": "REMOVED", "ITSM_CPU_CHANGED": "CPU",
        "ITSM_MEMORY_CHANGED": "MEMORY", "ITSM_IP_CHANGED": "IP",
        "ITSM_HOSTNAME_CHANGED": "HOSTNAME", "ITSM_OS_CHANGED": "OS",
        "ITSM_LOCATION_CHANGED": "LOCATION",
    }

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def evaluate(self, start: str, end: str) -> dict[str, Any]:
        tolerance = int(self.config.matching.get("sync_tolerance_days", 1))
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        query_start = (start_dt - timedelta(days=tolerance)).isoformat()
        query_end = (end_dt + timedelta(days=tolerance)).isoformat()
        rv_events = [e for e in self.repo.changes("RVTOOLS", 100000, query_start, query_end) if self.RV_CONCEPT.get(e["event_type"])]
        itsm_events = [e for e in self.repo.changes("ITSM", 100000, query_start, query_end) if self.ITSM_CONCEPT.get(e["event_type"])]
        identity = self._identity_maps()
        itsm_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in itsm_events:
            itsm_by_identity[event["asset_key"]].append(event)

        results: list[dict[str, Any]] = []
        used_itsm_ids: set[int] = set()
        for rv_event in sorted(rv_events, key=lambda e: e["detected_at"]):
            rv_time = datetime.fromisoformat(rv_event["detected_at"])
            if not (start_dt <= rv_time < end_dt):
                continue
            cm_id = identity.get(rv_event["asset_key"])
            concept = self.RV_CONCEPT[rv_event["event_type"]]
            if not cm_id:
                results.append(self._result(start, end, rv_event["asset_key"], rv_event, None, "ENVIRONMENT_REVIEW", {"reason": "RV 자산과 CM_ID 연결 없음"}))
                continue
            candidates = []
            for itsm_event in itsm_by_identity.get(cm_id, []):
                itsm_time = datetime.fromisoformat(itsm_event["detected_at"])
                if abs((itsm_time - rv_time).total_seconds()) <= tolerance * 86400 and self.ITSM_CONCEPT.get(itsm_event["event_type"]) == concept:
                    candidates.append(itsm_event)
            if not candidates:
                results.append(self._result(start, end, cm_id, rv_event, None, "ITSM_UPDATE_REQUIRED", {"concept": concept}))
                continue
            candidate = min(candidates, key=lambda e: abs((datetime.fromisoformat(e["detected_at"]) - rv_time).total_seconds()))
            used_itsm_ids.add(int(candidate["id"]))
            values_equal = self._values_equal(concept, rv_event.get("new_value"), candidate.get("new_value"))
            status = "SYNCED" if values_equal or concept in {"CREATED", "REMOVED", "LOCATION", "POWER"} else "SYNC_VALUE_MISMATCH"
            results.append(self._result(start, end, cm_id, rv_event, candidate, status, {"concept": concept, "values_equal": values_equal}))

        for itsm_event in itsm_events:
            event_time = datetime.fromisoformat(itsm_event["detected_at"])
            if not (start_dt <= event_time < end_dt) or int(itsm_event["id"]) in used_itsm_ids:
                continue
            results.append(self._result(start, end, itsm_event["asset_key"], None, itsm_event, "ENVIRONMENT_REVIEW", {"reason": "동일 기간 vCenter 변경 없음", "concept": self.ITSM_CONCEPT[itsm_event["event_type"]]}))

        self.repo.conn.execute("DELETE FROM sync_result WHERE period_start=? AND period_end=?", (start, end))
        self.repo.conn.executemany(
            "INSERT INTO sync_result(period_start, period_end, asset_identity, rv_event_type, itsm_event_type, sync_status, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["period_start"], r["period_end"], r["asset_identity"], r.get("rv_event_type"), r.get("itsm_event_type"), r["sync_status"], canonical_json(r["detail"]), r["created_at"]) for r in results],
        )
        counts: dict[str, int] = defaultdict(int)
        for result in results:
            counts[result["sync_status"]] += 1
        return {"start": start, "end": end, "counts": dict(counts), "results": results}

    def latest_results(self, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.repo.conn.execute("SELECT * FROM sync_result ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            result.append(item)
        return result

    def _identity_maps(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        latest_pair = self.repo.conn.execute(
            "SELECT itsm_snapshot_id, rv_snapshot_id, MAX(created_at) FROM reconciliation_result"
        ).fetchone()
        if latest_pair and latest_pair["itsm_snapshot_id"]:
            rows = self.repo.conn.execute(
                "SELECT cm_id, rv_asset_key FROM reconciliation_result WHERE itsm_snapshot_id=? AND rv_snapshot_id=? AND cm_id IS NOT NULL AND rv_asset_key IS NOT NULL",
                (latest_pair["itsm_snapshot_id"], latest_pair["rv_snapshot_id"]),
            ).fetchall()
            mapping.update({row["rv_asset_key"]: row["cm_id"] for row in rows})
        for cm_id, vm_uuid in self.repo.identity_maps().items():
            mapping[vm_uuid] = cm_id
        return mapping

    @staticmethod
    def _values_equal(concept: str, rv_value: Any, itsm_value: Any) -> bool:
        if rv_value is None or itsm_value is None:
            return False
        if concept == "CPU":
            return normalize_int(rv_value) == normalize_int(itsm_value)
        if concept == "MEMORY":
            rv_mb = memory_to_mb(rv_value, "MB")
            itsm_mb = memory_to_mb(itsm_value, "GB")
            return rv_mb is not None and itsm_mb is not None and abs(rv_mb - itsm_mb) <= 1
        if concept == "IP":
            try:
                rv_set = set(json.loads(rv_value)) if str(rv_value).startswith("[") else {str(rv_value).strip()}
                itsm_set = set(json.loads(itsm_value)) if str(itsm_value).startswith("[") else {str(itsm_value).strip()}
                return bool(rv_set & itsm_set) or rv_set == itsm_set
            except (json.JSONDecodeError, TypeError):
                pass
        return str(rv_value).strip().lower() == str(itsm_value).strip().lower()

    @staticmethod
    def _result(start: str, end: str, identity: str, rv_event: dict[str, Any] | None, itsm_event: dict[str, Any] | None, status: str, detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "period_start": start, "period_end": end, "asset_identity": identity,
            "rv_event_type": rv_event.get("event_type") if rv_event else None,
            "itsm_event_type": itsm_event.get("event_type") if itsm_event else None,
            "sync_status": status,
            "detail": {**detail, "rv_event_id": rv_event.get("id") if rv_event else None, "itsm_event_id": itsm_event.get("id") if itsm_event else None},
            "created_at": datetime.now().isoformat(),
        }
