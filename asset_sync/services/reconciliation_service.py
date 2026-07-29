from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from typing import Any

from ..config import AppConfig
from ..repositories import AssetRepository
from .override_service import OverrideService


class ReconciliationService:
    """Match current ITSM logical active assets to powered-on vCenter VMs."""

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def reconcile_latest(self) -> dict[str, Any]:
        itsm_snapshot = self.repo.latest_snapshot("ITSM")
        rv_snapshot = self.repo.latest_snapshot("RVTOOLS")
        if not itsm_snapshot or not rv_snapshot:
            return {"status": "NO_SNAPSHOT", "results": []}
        return self.reconcile(int(itsm_snapshot["id"]), int(rv_snapshot["id"]))

    def reconcile(self, itsm_snapshot_id: int, rv_snapshot_id: int) -> dict[str, Any]:
        itsm_all = OverrideService(self.config, self.repo).apply(self.repo.load_itsm_records(itsm_snapshot_id))
        rv_all = self.repo.load_rv_records(rv_snapshot_id)
        rv_snapshot_row = self.repo.snapshot_by_id(rv_snapshot_id)
        rv_run = self.repo.conn.execute("SELECT failed_scope_json FROM collection_run WHERE id=?", (rv_snapshot_row["collection_run_id"],)).fetchone()
        failed_scopes = set(json.loads(rv_run["failed_scope_json"] or "[]")) if rv_run else set()
        itsm = {
            key: rec for key, rec in itsm_all.items()
            if rec.get("server_category_code") == "CMSVRCATCD020" and rec.get("status_code") in {"CMSTA010", "CMSTA050"}
        }
        power_on = str(self.config.rvtools.get("power_on_value", "poweredon")).strip().lower()
        rv = {
            key: rec for key, rec in rv_all.items()
            if str(rec.get("power_state") or "").strip().lower() == power_on
            and not (self.config.rvtools.get("exclude_templates", True) and bool(rec.get("template_flag")))
            and not (self.config.rvtools.get("exclude_srm_placeholders", True) and bool(rec.get("srm_placeholder")))
        }
        identity_map = self.repo.identity_maps()
        by_uuid = {str(rec.get("vm_uuid") or ""): key for key, rec in rv.items() if rec.get("vm_uuid")}
        host_index: dict[str, set[str]] = defaultdict(set)
        ip_index: dict[str, set[str]] = defaultdict(set)
        vm_name_index: dict[str, set[str]] = defaultdict(set)
        for key, rec in rv.items():
            if rec.get("normalized_hostname"):
                host_index[str(rec["normalized_hostname"])].add(key)
            for ip in rec.get("ip_addresses", []):
                ip_index[str(ip)].add(key)
            if rec.get("vm_name"):
                vm_name_index[str(rec["vm_name"]).strip().lower()].add(key)

        matched_rv: set[str] = set()
        results: list[dict[str, Any]] = []
        created_at = datetime.now().isoformat()
        for cm_id, asset in itsm.items():
            candidates: set[str] = set()
            method = None
            score = 0
            mapped_uuid = identity_map.get(cm_id)
            if mapped_uuid and mapped_uuid in by_uuid:
                candidates = {by_uuid[mapped_uuid]}
                method, score = "IDENTITY_MAP", 100
            else:
                host = asset.get("normalized_hostname")
                ips = set(asset.get("ip_addresses", []))
                host_candidates = set(host_index.get(str(host), set())) if host else set()
                ip_candidates = set().union(*(ip_index.get(str(ip), set()) for ip in ips)) if ips else set()
                both = host_candidates & ip_candidates
                if both:
                    candidates, method, score = both, "IP_HOSTNAME", 95
                elif host_candidates:
                    candidates, method, score = host_candidates, "HOSTNAME", 80
                elif ip_candidates:
                    candidates, method, score = ip_candidates, "IP", 70
                elif self.config.matching.get("allow_vm_name_auto_match", False) and host:
                    candidates, method, score = set(vm_name_index.get(str(host).lower(), set())), "VM_NAME", 50

            if len(candidates) > 1:
                results.append(self._result(cm_id, None, "AMBIGUOUS", method, score, [], "복수 후보", created_at))
                continue
            if not candidates:
                status_code = asset.get("status_code")
                if failed_scopes:
                    reason = "일부 vCenter 수집 실패로 미매칭을 확정하지 않음"
                    results.append(self._result(cm_id, None, "COLLECTION_GAP", None, 0, [], reason, created_at))
                else:
                    reason = "대기 자산 미매칭" if status_code == "CMSTA050" else "운영 자산 미매칭"
                    results.append(self._result(cm_id, None, "ITSM_ONLY", None, 0, [], reason, created_at))
                continue
            rv_key = next(iter(candidates))
            matched_rv.add(rv_key)
            vm = rv[rv_key]
            if (
                method == "IP_HOSTNAME"
                and bool(self.config.matching.get("auto_remember_exact_match", True))
                and vm.get("vm_uuid")
            ):
                self.repo.remember_identity(cm_id, str(vm["vm_uuid"]), method)
            drifts = self._drifts(asset, vm)
            match_status = self._match_status(asset, vm, method, drifts)
            results.append(self._result(cm_id, rv_key, match_status, method, score, drifts, None, created_at))

        for rv_key in sorted(set(rv) - matched_rv):
            results.append(self._result(None, rv_key, "RVTOOLS_ONLY", None, 0, [], "ITSM 매칭 없음", created_at))

        self.repo.replace_reconciliation(itsm_snapshot_id, rv_snapshot_id, results)
        counts: dict[str, int] = defaultdict(int)
        for result in results:
            counts[result["match_status"]] += 1
        return {"status": "SUCCESS", "itsm_snapshot_id": itsm_snapshot_id, "rv_snapshot_id": rv_snapshot_id, "counts": dict(counts), "results": results}

    def _drifts(self, itsm: dict[str, Any], rv: dict[str, Any]) -> list[dict[str, Any]]:
        drifts: list[dict[str, Any]] = []
        if itsm.get("cpu_cores") is None or rv.get("cpus") is None:
            drifts.append({"field": "CPU", "status": "CPU_UNKNOWN", "itsm": itsm.get("cpu_cores"), "rvtools": rv.get("cpus")})
        elif int(itsm["cpu_cores"]) != int(rv["cpus"]):
            drifts.append({"field": "CPU", "status": "CPU_DIFF", "itsm": itsm["cpu_cores"], "rvtools": rv["cpus"]})

        tolerance = int(self.config.matching.get("memory_tolerance_mb", 1))
        if itsm.get("memory_mb") is None or rv.get("memory_mb") is None:
            drifts.append({"field": "MEMORY", "status": "MEMORY_UNKNOWN", "itsm": itsm.get("memory_mb"), "rvtools": rv.get("memory_mb")})
        elif abs(int(itsm["memory_mb"]) - int(rv["memory_mb"])) > tolerance:
            drifts.append({"field": "MEMORY", "status": "MEMORY_DIFF", "itsm": itsm["memory_mb"], "rvtools": rv["memory_mb"]})

        if not itsm.get("os_family") or not rv.get("os_family"):
            drifts.append({"field": "OS", "status": "OS_VERSION_UNKNOWN", "itsm": itsm.get("os_family"), "rvtools": rv.get("os_family")})
        elif itsm.get("os_family") != rv.get("os_family"):
            drifts.append({"field": "OS", "status": "OS_FAMILY_DIFF", "itsm": itsm.get("os_family"), "rvtools": rv.get("os_family")})
        elif itsm.get("os_version") and rv.get("os_version") and itsm.get("os_version") != rv.get("os_version"):
            drifts.append({"field": "OS_VERSION", "status": "OS_VERSION_DIFF", "itsm": itsm.get("os_version"), "rvtools": rv.get("os_version")})
        return drifts

    @staticmethod
    def _match_status(itsm: dict[str, Any], rv: dict[str, Any], method: str | None, drifts: list[dict[str, Any]]) -> str:
        same_host = itsm.get("normalized_hostname") and itsm.get("normalized_hostname") == rv.get("normalized_hostname")
        itsm_ips = set(itsm.get("ip_addresses", []))
        rv_ips = set(rv.get("ip_addresses", []))
        same_ip = bool(itsm_ips & rv_ips)
        if method == "HOSTNAME" and same_host and not same_ip:
            return "IP_CHANGE_CANDIDATE"
        if method == "IP" and same_ip and not same_host:
            return "HOSTNAME_REVIEW"
        return "MATCHED_WITH_DRIFT" if drifts else "MATCHED"

    @staticmethod
    def _result(cm_id: str | None, rv_key: str | None, status: str, method: str | None, score: int, drifts: list[dict[str, Any]], reason: str | None, created_at: str) -> dict[str, Any]:
        return {"cm_id": cm_id, "rv_asset_key": rv_key, "match_status": status, "match_method": method, "score": score, "drifts": drifts, "reason": reason, "created_at": created_at}
