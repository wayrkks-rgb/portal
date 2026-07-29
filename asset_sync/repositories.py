from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from .utils.hashing import canonical_json


class AssetRepository:
    """Persistence facade for collection, snapshot, event and reconciliation data."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection

    def start_collection_run(self, source: str, started_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_run(source, started_at, status) VALUES (?, ?, 'RUNNING')",
            (source, started_at),
        )
        return int(cur.lastrowid)

    def finish_collection_run(
        self,
        run_id: int,
        status: str,
        record_count: int,
        ended_at: str,
        success_scopes: Iterable[str] = (),
        failed_scopes: Iterable[str] = (),
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE collection_run
               SET ended_at=?, status=?, record_count=?, success_scope_json=?, failed_scope_json=?,
                   error_message=?, metadata_json=?
             WHERE id=?
            """,
            (
                ended_at,
                status,
                record_count,
                canonical_json(list(success_scopes)),
                canonical_json(list(failed_scopes)),
                error_message,
                canonical_json(metadata or {}),
                run_id,
            ),
        )

    def create_snapshot(
        self,
        source: str,
        snapshot_date: str,
        collected_at: str,
        run_id: int,
        status: str,
        record_count: int,
        checksum: str,
        source_scope: str = "ALL",
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO snapshot(source, snapshot_date, collected_at, collection_run_id, status,
                                 source_scope, record_count, checksum)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, snapshot_date, collected_at, run_id, status, source_scope, record_count, checksum),
        )
        return int(cur.lastrowid)

    def latest_snapshot(self, source: str, before_snapshot_id: int | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM snapshot WHERE source=? AND status IN ('SUCCESS','PARTIAL_SUCCESS')"
        params: list[Any] = [source]
        if before_snapshot_id is not None:
            sql += " AND id < ?"
            params.append(before_snapshot_id)
        sql += " ORDER BY collected_at DESC, id DESC LIMIT 1"
        return self.conn.execute(sql, params).fetchone()

    def previous_day_snapshot(self, source: str, current_snapshot_date: str) -> sqlite3.Row | None:
        """Return the latest usable snapshot from a calendar date before the current snapshot date."""
        return self.conn.execute(
            """
            SELECT *
              FROM snapshot
             WHERE source=?
               AND status IN ('SUCCESS','PARTIAL_SUCCESS')
               AND snapshot_date < ?
             ORDER BY snapshot_date DESC, collected_at DESC, id DESC
             LIMIT 1
            """,
            (source, current_snapshot_date),
        ).fetchone()

    def snapshot_by_id(self, snapshot_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM snapshot WHERE id=?", (snapshot_id,)).fetchone()

    def insert_itsm_records(self, snapshot_id: int, records: list[dict[str, Any]]) -> None:
        rows = [
            (
                snapshot_id,
                r["CM_ID"],
                r.get("normalized_hostname"),
                r.get("primary_ip"),
                canonical_json(r.get("ip_addresses", [])),
                r.get("cpu_cores"),
                r.get("memory_mb"),
                r.get("os_family"),
                r.get("os_version_normalized"),
                r.get("CM_STA_CD"),
                r.get("CM_SVR_CAT_CD"),
                r.get("CM_OWN_CAT_CD"),
                r.get("eos_value"),
                r["record_hash"],
                canonical_json(r.get("raw", r)),
            )
            for r in records
        ]
        self.conn.executemany(
            """
            INSERT INTO itsm_asset_snapshot(
                snapshot_id, cm_id, normalized_hostname, primary_ip, ip_json, cpu_cores, memory_mb,
                os_family, os_version, status_code, server_category_code, environment_code,
                eos_value, record_hash, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        ip_rows = []
        for r in records:
            for ip in r.get("ip_addresses", []):
                ip_rows.append(("ITSM", snapshot_id, r["CM_ID"], ip, 1 if ip == r.get("primary_ip") else 0))
        self.conn.executemany(
            "INSERT OR IGNORE INTO asset_ip(source, snapshot_id, asset_key, ip_address, is_primary) VALUES (?, ?, ?, ?, ?)",
            ip_rows,
        )

    def insert_rv_records(self, snapshot_id: int, records: list[dict[str, Any]]) -> None:
        rows = [
            (
                snapshot_id,
                r["asset_key"],
                r.get("vm_uuid"),
                r.get("smbios_uuid"),
                r.get("vm_id"),
                r.get("vcenter"),
                r.get("vm_name"),
                r.get("dns_name"),
                r.get("normalized_hostname"),
                r.get("primary_ip"),
                canonical_json(r.get("ip_addresses", [])),
                r.get("cpus"),
                r.get("memory_mb"),
                r.get("os_family"),
                r.get("os_version"),
                r.get("power_state"),
                r.get("datacenter"),
                r.get("cluster"),
                r.get("esxi_host"),
                1 if r.get("template_flag") else 0,
                1 if r.get("srm_placeholder") else 0,
                r["record_hash"],
                canonical_json(r.get("raw", r)),
            )
            for r in records
        ]
        self.conn.executemany(
            """
            INSERT INTO rv_asset_snapshot(
                snapshot_id, asset_key, vm_uuid, smbios_uuid, vm_id, vcenter, vm_name, dns_name,
                normalized_hostname, primary_ip, ip_json, cpus, memory_mb, os_family, os_version,
                power_state, datacenter, cluster_name, esxi_host, template_flag, srm_placeholder,
                record_hash, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        ip_rows = []
        for r in records:
            for ip in r.get("ip_addresses", []):
                ip_rows.append(("RVTOOLS", snapshot_id, r["asset_key"], ip, 1 if ip == r.get("primary_ip") else 0))
        self.conn.executemany(
            "INSERT OR IGNORE INTO asset_ip(source, snapshot_id, asset_key, ip_address, is_primary) VALUES (?, ?, ?, ?, ?)",
            ip_rows,
        )

    def load_itsm_records(self, snapshot_id: int) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM itsm_asset_snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchall()
        return {row["cm_id"]: self._row_to_record(row) for row in rows}

    def load_rv_records(self, snapshot_id: int) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM rv_asset_snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchall()
        return {row["asset_key"]: self._row_to_record(row) for row in rows}

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["raw"] = json.loads(record.pop("raw_json"))
        if "ip_json" in record:
            record["ip_addresses"] = json.loads(record.pop("ip_json"))
        return record

    def replace_change_events(self, snapshot_id: int, events: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM change_event WHERE snapshot_id=?", (snapshot_id,))
        self.conn.executemany(
            """
            INSERT INTO change_event(source, snapshot_id, previous_snapshot_id, asset_key, event_type,
                                     field_name, old_value, new_value, detected_at, group_key, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e["source"], snapshot_id, e.get("previous_snapshot_id"), e["asset_key"], e["event_type"],
                    e.get("field_name"), e.get("old_value"), e.get("new_value"), e["detected_at"],
                    e.get("group_key"), canonical_json(e.get("metadata", {})),
                )
                for e in events
            ],
        )

    def replace_reconciliation(self, itsm_snapshot_id: int, rv_snapshot_id: int, results: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "DELETE FROM reconciliation_result WHERE itsm_snapshot_id=? AND rv_snapshot_id=?",
            (itsm_snapshot_id, rv_snapshot_id),
        )
        self.conn.executemany(
            """
            INSERT INTO reconciliation_result(
                itsm_snapshot_id, rv_snapshot_id, cm_id, rv_asset_key, match_status, match_method,
                score, drift_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    itsm_snapshot_id, rv_snapshot_id, r.get("cm_id"), r.get("rv_asset_key"),
                    r["match_status"], r.get("match_method"), r.get("score", 0),
                    canonical_json(r.get("drifts", [])), r.get("reason"), r["created_at"],
                )
                for r in results
            ],
        )

    def collection_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM collection_run ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def changes(self, source: str | None = None, limit: int = 500, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if source:
            clauses.append("source=?")
            params.append(source)
        if start:
            clauses.append("detected_at>=?")
            params.append(start)
        if end:
            clauses.append("detected_at<?")
            params.append(end)
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM change_event WHERE {' AND '.join(clauses)} ORDER BY detected_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def reconciliation(self, limit: int = 1000, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM reconciliation_result WHERE match_status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM reconciliation_result ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def identity_maps(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT cm_id, vm_uuid FROM identity_map WHERE active_yn=1").fetchall()
        return {row["cm_id"]: row["vm_uuid"] for row in rows}

    def remember_identity(self, cm_id: str, vm_uuid: str, method: str = "IP_HOSTNAME") -> bool:
        """Persist a high-confidence local mapping without changing Oracle ITSM."""
        if not cm_id or not vm_uuid:
            return False
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO identity_map(
                cm_id, vm_uuid, active_yn, approved_by, approved_at, note
            ) VALUES (?, ?, 1, 'AUTO', datetime('now'), ?)
            """,
            (cm_id, vm_uuid, f"AUTO:{method}"),
        )
        return cursor.rowcount > 0

    def start_daily_batch(self, batch_date: str, started_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO daily_batch_run(batch_date, started_at, status) VALUES (?, ?, 'RUNNING')",
            (batch_date, started_at),
        )
        return int(cur.lastrowid)

    def finish_daily_batch(
        self,
        batch_id: int,
        *,
        status: str,
        ended_at: str,
        itsm_run_id: int | None = None,
        itsm_snapshot_id: int | None = None,
        vcenter_run_id: int | None = None,
        vcenter_snapshot_id: int | None = None,
        reconciliation_created_at: str | None = None,
        resource_usage_status: str = "PENDING_SCRIPT",
        errors: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE daily_batch_run
               SET ended_at=?, status=?, itsm_run_id=?, itsm_snapshot_id=?,
                   vcenter_run_id=?, vcenter_snapshot_id=?, reconciliation_created_at=?,
                   resource_usage_status=?, error_json=?, metadata_json=?
             WHERE id=?
            """,
            (
                ended_at, status, itsm_run_id, itsm_snapshot_id, vcenter_run_id,
                vcenter_snapshot_id, reconciliation_created_at, resource_usage_status,
                canonical_json(errors or {}), canonical_json(metadata or {}), batch_id,
            ),
        )

    def latest_daily_batch(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM daily_batch_run ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def active_reconciliation_exceptions(self, as_of: str | None = None) -> list[dict[str, Any]]:
        day = (as_of or datetime.now().date().isoformat())[:10]
        rows = self.conn.execute(
            """
            SELECT * FROM reconciliation_exception
             WHERE active_yn=1
               AND (valid_from IS NULL OR valid_from='' OR valid_from<=?)
               AND (valid_to IS NULL OR valid_to='' OR valid_to>=?)
             ORDER BY created_at DESC, id DESC
            """,
            (day, day),
        ).fetchall()
        return [dict(row) for row in rows]

    def reconciliation_exceptions(self, include_inactive: bool = True, limit: int = 5000) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reconciliation_exception"
        params: list[Any] = []
        if not include_inactive:
            sql += " WHERE active_yn=1"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def add_reconciliation_exception(self, item: dict[str, Any], user_id: str) -> int:
        now = datetime.now().isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO reconciliation_exception(
                exception_type, cm_id, rv_asset_key, server_name, reason, valid_from, valid_to,
                active_yn, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                item["exception_type"], item.get("cm_id") or None, item.get("rv_asset_key") or None,
                item.get("server_name") or None, item["reason"], item.get("valid_from") or None,
                item.get("valid_to") or None, user_id, now,
            ),
        )
        return int(cur.lastrowid)

    def deactivate_reconciliation_exception(self, exception_id: int, user_id: str) -> bool:
        now = datetime.now().isoformat()
        cur = self.conn.execute(
            """
            UPDATE reconciliation_exception
               SET active_yn=0, deactivated_by=?, deactivated_at=?, updated_by=?, updated_at=?
             WHERE id=? AND active_yn=1
            """,
            (user_id, now, user_id, now, exception_id),
        )
        return cur.rowcount > 0

    def replace_resource_usage(self, stat_date: str, records: list[dict[str, Any]]) -> int:
        self.conn.execute("DELETE FROM vcenter_resource_daily WHERE stat_date=?", (stat_date,))
        now = datetime.now().isoformat()
        rows = [
            (
                stat_date, r.get("entity_type", "VM"), r.get("vcenter_id"), r.get("esxi_host"),
                r.get("vm_uuid"), r.get("vm_name"), r.get("cpu_max_pct"), r.get("cpu_avg_pct"),
                r.get("mem_max_pct"), r.get("mem_avg_pct"), int(r.get("sample_count") or 0),
                r.get("collection_status", "SUCCESS"), r.get("source_name", "VM_ResourceUsageExport"),
                canonical_json(r.get("raw", r)), now,
            )
            for r in records
        ]
        self.conn.executemany(
            """
            INSERT INTO vcenter_resource_daily(
                stat_date, entity_type, vcenter_id, esxi_host, vm_uuid, vm_name,
                cpu_max_pct, cpu_avg_pct, mem_max_pct, mem_avg_pct, sample_count,
                collection_status, source_name, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def audit(self, user_id: str, action: str, target_type: str, target_id: str | None, reason: str | None, before: Any, after: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(user_id, action, target_type, target_id, reason, before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, action, target_type, target_id, reason, canonical_json(before), canonical_json(after), datetime.now().isoformat()),
        )
