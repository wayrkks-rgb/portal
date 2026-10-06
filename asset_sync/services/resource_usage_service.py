from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from ..collectors.powercli_resource_collector import PowerCLIResourceUsageCollector
from ..config import AppConfig
from ..repositories import AssetRepository
from .asset_scope import AssetScope, vcenter_key
from .change_presenter import present
from .display_name_service import DisplayNameService
from ..utils.hashing import canonical_json

#: 변경 유형을 화면 문구로. 코드를 그대로 두면 받은 사람이 읽을 수 없다.
CHANGE_LABELS = {
    "RV_NEW": "VM 신규 생성",
    "RV_REMOVED": "VM 삭제",
    "RV_CPU_CHANGED": "vCPU 변경",
    "RV_MEMORY_CHANGED": "메모리 변경",
    "RV_HOST_CHANGED": "통합기(ESXi) 이동",
    "RV_VCENTER_CHANGED": "vCenter 이동",
}


class VMResourceUsageExportService:
    """Collect, persist, query and export ESXi/VM resource usage.

    Daily 07:00 processing stores the previous day's usage and links it to the
    vCenter inventory snapshot from the same batch. Arbitrary date ranges are
    calculated from the persisted daily facts; operators do not run PowerCLI from
    the screen.
    """

    COLUMN_ALIASES = {
        "ENTITY_TYPE": "entity_type",
        "TYPE": "entity_type",
        "VCENTER_ID": "vcenter_id",
        "VCENTER": "vcenter_id",
        "SERVICE_NAME": "service_name",
        "SERVICENAME": "service_name",
        "CLUSTER_NAME": "cluster_name",
        "CLUSTERNAME": "cluster_name",
        "ESXI_HOST": "esxi_host",
        "HOST": "esxi_host",
        "HOSTNAME": "esxi_host",
        "VM_UUID": "vm_uuid",
        "VM_NAME": "vm_name",
        "VMNAME": "vm_name",
        "POWER_STATE": "power_state",
        "POWERSTATE": "power_state",
        "ALLOCATED_CPU_CORES": "allocated_cpu_cores",
        "CPUS": "allocated_cpu_cores",
        "ALLOCATED_MEMORY_MB": "allocated_memory_mb",
        "MEMORY_MB": "allocated_memory_mb",
        "CPU_MAX": "cpu_max_pct",
        "CPUMAX": "cpu_max_pct",
        "CPU_MAX_PCT": "cpu_max_pct",
        "CPU_AVG": "cpu_avg_pct",
        "CPUAVG": "cpu_avg_pct",
        "CPU_AVG_PCT": "cpu_avg_pct",
        "MEM_MAX": "mem_max_pct",
        "MEMMAX": "mem_max_pct",
        "MEM_MAX_PCT": "mem_max_pct",
        "MEM_AVG": "mem_avg_pct",
        "MEMAVG": "mem_avg_pct",
        "MEM_AVG_PCT": "mem_avg_pct",
        "SAMPLE_COUNT": "sample_count",
        "PROVISIONED_DISK_MB": "provisioned_disk_mb",
        "USED_DISK_MB": "used_disk_mb",
    }

    #: 데이터스토어 행의 칸 이름. 수집기가 주는 그대로 쓴다.
    DATASTORE_FIELDS = (
        "vcenter_id", "service_name", "cluster_name", "datastore_name",
        "datastore_type", "capacity_mb", "free_mb", "used_mb", "provisioned_mb",
        "host_count",
    )

    def __init__(
        self,
        config: AppConfig,
        repository: AssetRepository,
        scope: AssetScope | None = None,
    ) -> None:
        self.config = config
        self.repo = repository
        self.settings = config.rvtools.get("resource_usage", {}) or {}
        # 실제로 쓰지 않는 VM(vCLS 등)은 세지 않는다. ITSM 자산과 같은 판단을
        # 쓰므로 어느 화면이든 VM 대수가 같다.
        self.scope = scope or AssetScope.load(config, repository)

    def daily_status(self) -> dict[str, Any]:
        script = self.config.resolve(
            self.settings.get("script_path", "scripts/collect_vcenter_resource_usage.ps1")
        )
        enabled = bool(self.settings.get("enabled", True))
        if not enabled:
            return {"status": "DISABLED", "message": "통합서버 자원사용률 자동수집이 비활성화되어 있습니다.", "script": script.name}
        if not script.exists():
            return {"status": "PENDING_SCRIPT", "message": "자원사용률 PowerCLI 스크립트가 없습니다.", "script": script.name}
        latest = self.repo.conn.execute(
            "SELECT * FROM resource_usage_run ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        result = {"status": "READY", "message": "07시 자동배치에서 자원사용률을 수집합니다.", "script": script.name}
        if latest:
            result["latest_run"] = dict(latest)
        return result

    def collect_for_batch(
        self,
        daily_batch_id: int,
        vcenter_snapshot_id: int,
        *,
        demo: bool = False,
        period_start: date | None = None,
        period_end: date | None = None,
    ) -> dict[str, Any]:
        end_day = period_end or (date.today() - timedelta(days=1))
        start_day = period_start or end_day
        started_at = datetime.now().isoformat()
        cur = self.repo.conn.execute(
            """
            INSERT INTO resource_usage_run(
                daily_batch_id, vcenter_snapshot_id, period_start, period_end, started_at, status
            ) VALUES (?, ?, ?, ?, ?, 'RUNNING')
            """,
            (daily_batch_id, vcenter_snapshot_id, start_day.isoformat(), end_day.isoformat(), started_at),
        )
        run_id = int(cur.lastrowid)
        try:
            if demo:
                payload = self._demo_payload(vcenter_snapshot_id)
            else:
                if not bool(self.settings.get("enabled", True)):
                    raise RuntimeError("통합서버 자원사용률 자동수집이 비활성화되어 있습니다.")
                payload = PowerCLIResourceUsageCollector(self.config).collect_all(start_day, end_day)
                if payload.get("status") == "FAILED":
                    raise RuntimeError("모든 vCenter 자원사용률 수집이 실패했습니다.")
            hosts, vms = self._enrich_with_inventory(
                payload.get("hosts", []), payload.get("vms", []), vcenter_snapshot_id
            )
            datastores = self._normalize_datastores(payload.get("datastores", []))
            self._replace_run_rows(
                run_id, end_day.isoformat(), vcenter_snapshot_id, hosts, vms, datastores
            )
            status = str(payload.get("status") or "SUCCESS")
            self.repo.conn.execute(
                """
                UPDATE resource_usage_run
                   SET ended_at=?, status=?, success_scope_json=?, failed_scope_json=?,
                       host_count=?, vm_count=?, metadata_json=?
                 WHERE id=?
                """,
                (
                    datetime.now().isoformat(), status,
                    canonical_json(payload.get("success_scopes", [])),
                    canonical_json(payload.get("failed_scopes", {})),
                    len(hosts), len(vms),
                    canonical_json({"period_start": start_day.isoformat(), "period_end": end_day.isoformat()}),
                    run_id,
                ),
            )
            return {
                "status": status,
                "run_id": run_id,
                "period_start": start_day.isoformat(),
                "period_end": end_day.isoformat(),
                "host_count": len(hosts),
                "vm_count": len(vms),
                "datastore_count": len(datastores),
                "failed_scopes": payload.get("failed_scopes", {}),
            }
        except Exception as exc:
            self.repo.conn.execute(
                "UPDATE resource_usage_run SET ended_at=?, status='FAILED', error_message=? WHERE id=?",
                (datetime.now().isoformat(), str(exc), run_id),
            )
            return {"status": "FAILED", "run_id": run_id, "error": str(exc), "host_count": 0, "vm_count": 0}

    def _demo_payload(self, snapshot_id: int) -> dict[str, Any]:
        records = list(self.repo.load_rv_records(snapshot_id).values())
        vms: list[dict[str, Any]] = []
        host_members: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for index, vm in enumerate(records, start=1):
            if vm.get("template_flag") or vm.get("srm_placeholder"):
                continue
            vc = str(vm.get("vcenter") or "DEMO_VCENTER")
            cluster = str(vm.get("cluster_name") or "DEMO_CLUSTER")
            host = str(vm.get("esxi_host") or "DEMO_ESXI")
            row = {
                "vcenter_id": vc,
                "service_name": vc,
                "cluster_name": cluster,
                "esxi_host": host,
                "vm_uuid": vm.get("vm_uuid"),
                "vm_name": vm.get("vm_name"),
                "power_state": vm.get("power_state"),
                "allocated_cpu_cores": vm.get("cpus"),
                "allocated_memory_mb": vm.get("memory_mb"),
                # 시연용. 할당 100GB 중 60% 쯤 쓴 모양으로 둔다.
                "provisioned_disk_mb": 100 * 1024,
                "used_disk_mb": int(60 * 1024 + index * 128),
                "cpu_max_pct": round(30 + index * 1.7, 2),
                "cpu_avg_pct": round(12 + index * 0.8, 2),
                "mem_max_pct": round(45 + index * 1.3, 2),
                "mem_avg_pct": round(28 + index * 0.7, 2),
                "sample_count": 12,
            }
            vms.append(row)
            host_members[(vc, cluster, host)].append(row)
        hosts = []
        for (vc, cluster, host), members in host_members.items():
            hosts.append({
                "vcenter_id": vc,
                "service_name": vc,
                "cluster_name": cluster,
                "esxi_host": host,
                "allocated_cpu_cores": 64,
                "allocated_memory_mb": 524288,
                "cpu_max_pct": max(float(m["cpu_max_pct"]) for m in members),
                "cpu_avg_pct": round(sum(float(m["cpu_avg_pct"]) for m in members) / len(members), 2),
                "mem_max_pct": max(float(m["mem_max_pct"]) for m in members),
                "mem_avg_pct": round(sum(float(m["mem_avg_pct"]) for m in members) / len(members), 2),
                "sample_count": 12,
            })
        datastores = []
        for vc in sorted({str(r["vcenter_id"]) for r in vms}):
            members = [r for r in vms if str(r["vcenter_id"]) == vc]
            provisioned = sum(int(r["provisioned_disk_mb"]) for r in members)
            used = sum(int(r["used_disk_mb"]) for r in members)
            datastores.append({
                "vcenter_id": vc, "service_name": vc, "cluster_name": None,
                "datastore_name": f"{vc}_DEMO_DS01", "datastore_type": "VMFS",
                "capacity_mb": max(provisioned, used) + 200 * 1024,
                "free_mb": 200 * 1024, "used_mb": used, "provisioned_mb": provisioned,
                "host_count": len({str(r["esxi_host"]) for r in members}),
            })
        return {
            "status": "SUCCESS", "hosts": hosts, "vms": vms, "datastores": datastores,
            "success_scopes": sorted({r["vcenter_id"] for r in vms}), "failed_scopes": {},
        }

    def _normalize_datastores(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """수집기가 준 데이터스토어 행을 저장할 모양으로."""
        vc_names = {
            str(item.get("id") or item.get("name") or ""): str(item.get("name") or item.get("id") or "")
            for item in self.config.rvtools.get("vcenters", [])
        }
        result: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            name = self._text(raw.get("datastore_name") or raw.get("DATASTORE_NAME") or raw.get("name"))
            if not name:
                continue
            vcenter = self._text(raw.get("vcenter_id")) or ""
            result.append({
                "vcenter_id": vcenter,
                "service_name": self._text(raw.get("service_name")) or vc_names.get(vcenter) or vcenter,
                "cluster_name": self._text(raw.get("cluster_name")),
                "datastore_name": name,
                "datastore_type": self._text(raw.get("datastore_type")),
                "accessible": 0 if str(raw.get("accessible", True)).lower() in {"false", "0", "no"} else 1,
                "capacity_mb": self._int(raw.get("capacity_mb")),
                "free_mb": self._int(raw.get("free_mb")),
                "used_mb": self._int(raw.get("used_mb")),
                "provisioned_mb": self._int(raw.get("provisioned_mb")),
                "host_count": self._int(raw.get("host_count")) or 0,
                "raw": raw,
            })
        return result

    def _enrich_with_inventory(
        self,
        host_rows: list[dict[str, Any]],
        usage_rows: list[dict[str, Any]],
        snapshot_id: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # 템플릿·SRM 자리표시자와 vSphere 가 스스로 만드는 VM 은 여기서 빠진다.
        inventory = [
            record for record in self.repo.load_rv_records(snapshot_id).values()
            if self.scope.decide_vcenter(record).included
        ]
        by_uuid = {(str(r.get("vcenter") or ""), str(r.get("vm_uuid") or "").lower()): r for r in inventory if r.get("vm_uuid")}
        by_name = {(str(r.get("vcenter") or ""), str(r.get("vm_name") or "").lower()): r for r in inventory if r.get("vm_name")}
        usage_map: dict[str, dict[str, Any]] = {}
        for raw in usage_rows:
            row = self._normalize(raw)
            vc = str(row.get("vcenter_id") or "")
            match = None
            if row.get("vm_uuid"):
                match = by_uuid.get((vc, str(row["vm_uuid"]).lower()))
            if not match and row.get("vm_name"):
                match = by_name.get((vc, str(row["vm_name"]).lower()))
            if match:
                row.update({
                    "asset_key": match.get("asset_key"),
                    "vcenter_id": match.get("vcenter") or vc,
                    "cluster_name": match.get("cluster_name"),
                    "esxi_host": match.get("esxi_host"),
                    "vm_uuid": match.get("vm_uuid") or row.get("vm_uuid"),
                    "vm_name": match.get("vm_name") or row.get("vm_name"),
                    "power_state": match.get("power_state"),
                    "allocated_cpu_cores": match.get("cpus"),
                    "allocated_memory_mb": match.get("memory_mb"),
                    "inventory_status": "CURRENT",
                })
            key = str(row.get("asset_key") or f"{row.get('vcenter_id')}|{row.get('vm_uuid') or row.get('vm_name')}")
            usage_map[key] = row

        vc_names = {
            str(item.get("id") or item.get("name") or ""): str(item.get("name") or item.get("id") or "")
            for item in self.config.rvtools.get("vcenters", [])
        }
        final_vms: list[dict[str, Any]] = []
        for vm in inventory:
            key = str(vm.get("asset_key"))
            row = usage_map.pop(key, None) or {
                "vcenter_id": vm.get("vcenter"),
                "vm_uuid": vm.get("vm_uuid"),
                "vm_name": vm.get("vm_name"),
                "collection_status": "NO_STAT",
                "sample_count": 0,
            }
            row.update({
                "asset_key": vm.get("asset_key"),
                "vcenter_id": vm.get("vcenter"),
                "service_name": row.get("service_name") or vc_names.get(str(vm.get("vcenter") or "")) or vm.get("vcenter"),
                "cluster_name": vm.get("cluster_name"),
                "esxi_host": vm.get("esxi_host"),
                "vm_uuid": vm.get("vm_uuid"),
                "vm_name": vm.get("vm_name"),
                "power_state": vm.get("power_state"),
                "allocated_cpu_cores": vm.get("cpus"),
                "allocated_memory_mb": vm.get("memory_mb"),
                "inventory_status": "CURRENT",
            })
            final_vms.append(row)
        for orphan in usage_map.values():
            orphan["inventory_status"] = "NOT_IN_CURRENT_INVENTORY"
            final_vms.append(orphan)

        host_vm_count: dict[tuple[str, str], int] = defaultdict(int)
        for vm in final_vms:
            if vm.get("inventory_status") == "CURRENT" and vm.get("esxi_host"):
                host_vm_count[(str(vm.get("vcenter_id") or ""), str(vm.get("esxi_host")))] += 1
        normalized_hosts = []
        for raw in host_rows:
            row = self._normalize(raw)
            row["entity_type"] = "ESXI"
            row["service_name"] = row.get("service_name") or vc_names.get(str(row.get("vcenter_id") or "")) or row.get("vcenter_id")
            row["vm_count"] = host_vm_count.get((str(row.get("vcenter_id") or ""), str(row.get("esxi_host") or "")), 0)
            normalized_hosts.append(row)
        known_hosts = {(str(r.get("vcenter_id") or ""), str(r.get("esxi_host") or "")) for r in normalized_hosts}
        for (vc, host), vm_count in host_vm_count.items():
            if (vc, host) not in known_hosts:
                sample_vm = next((v for v in final_vms if str(v.get("vcenter_id") or "") == vc and str(v.get("esxi_host") or "") == host), {})
                normalized_hosts.append({
                    "entity_type": "ESXI", "vcenter_id": vc, "service_name": vc_names.get(vc) or vc,
                    "cluster_name": sample_vm.get("cluster_name"), "esxi_host": host, "vm_count": vm_count,
                    "collection_status": "NO_STAT", "sample_count": 0,
                })
        return normalized_hosts, final_vms

    def _replace_run_rows(
        self,
        run_id: int,
        stat_date: str,
        snapshot_id: int,
        hosts: list[dict[str, Any]],
        vms: list[dict[str, Any]],
        datastores: list[dict[str, Any]] | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        self.repo.conn.execute("DELETE FROM host_resource_usage_daily WHERE run_id=?", (run_id,))
        self.repo.conn.execute("DELETE FROM vm_resource_usage_daily WHERE run_id=?", (run_id,))
        self.repo.conn.execute("DELETE FROM datastore_usage_daily WHERE run_id=?", (run_id,))
        self.repo.conn.executemany(
            """
            INSERT INTO host_resource_usage_daily(
                run_id, stat_date, vcenter_id, service_name, cluster_name, esxi_host, vm_count,
                allocated_cpu_cores, allocated_memory_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct,
                mem_avg_pct, sample_count, collection_status, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(
                run_id, stat_date, str(r.get("vcenter_id") or "UNKNOWN"), r.get("service_name"),
                r.get("cluster_name"), str(r.get("esxi_host") or "UNKNOWN"), int(r.get("vm_count") or 0),
                self._int(r.get("allocated_cpu_cores")), self._int(r.get("allocated_memory_mb")),
                self._float(r.get("cpu_max_pct")), self._float(r.get("cpu_avg_pct")),
                self._float(r.get("mem_max_pct")), self._float(r.get("mem_avg_pct")),
                self._int(r.get("sample_count")) or 0, r.get("collection_status", "SUCCESS"),
                canonical_json(r.get("raw", r)), now,
            ) for r in hosts],
        )
        self.repo.conn.executemany(
            """
            INSERT INTO vm_resource_usage_daily(
                run_id, stat_date, vcenter_snapshot_id, asset_key, vcenter_id, service_name,
                cluster_name, esxi_host, vm_uuid, vm_name, power_state, allocated_cpu_cores,
                allocated_memory_mb, provisioned_disk_mb, used_disk_mb,
                cpu_max_pct, cpu_avg_pct, mem_max_pct, mem_avg_pct,
                sample_count, inventory_status, collection_status, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(
                run_id, stat_date, snapshot_id, r.get("asset_key"), str(r.get("vcenter_id") or "UNKNOWN"),
                r.get("service_name"), r.get("cluster_name"), r.get("esxi_host"), r.get("vm_uuid"),
                str(r.get("vm_name") or "UNKNOWN"), r.get("power_state"), self._int(r.get("allocated_cpu_cores")),
                self._int(r.get("allocated_memory_mb")),
                self._int(r.get("provisioned_disk_mb")), self._int(r.get("used_disk_mb")),
                self._float(r.get("cpu_max_pct")),
                self._float(r.get("cpu_avg_pct")), self._float(r.get("mem_max_pct")),
                self._float(r.get("mem_avg_pct")), self._int(r.get("sample_count")) or 0,
                r.get("inventory_status", "CURRENT"), r.get("collection_status", "SUCCESS"),
                canonical_json(r.get("raw", r)), now,
            ) for r in vms],
        )
        self.repo.conn.executemany(
            """
            INSERT INTO datastore_usage_daily(
                run_id, stat_date, vcenter_id, service_name, cluster_name, datastore_name,
                datastore_type, accessible, capacity_mb, free_mb, used_mb, provisioned_mb,
                host_count, vm_count, collection_status, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(
                run_id, stat_date, str(r.get("vcenter_id") or "UNKNOWN"), r.get("service_name"),
                r.get("cluster_name"), str(r.get("datastore_name") or "UNKNOWN"),
                r.get("datastore_type"), int(r.get("accessible", 1)),
                self._int(r.get("capacity_mb")), self._int(r.get("free_mb")),
                self._int(r.get("used_mb")), self._int(r.get("provisioned_mb")),
                self._int(r.get("host_count")) or 0, self._int(r.get("vm_count")) or 0,
                r.get("collection_status", "SUCCESS"), canonical_json(r.get("raw", r)), now,
            ) for r in (datastores or [])],
        )

    def summary(
        self,
        start: str,
        end: str,
        *,
        vcenter_id: str | None = None,
        cluster_name: str | None = None,
        esxi_host: str | None = None,
    ) -> dict[str, Any]:
        start_day = date.fromisoformat(start[:10])
        end_day = date.fromisoformat(end[:10])
        if start_day > end_day:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        filters = ["stat_date>=?", "stat_date<=?"]
        params: list[Any] = [start_day.isoformat(), end_day.isoformat()]
        for column, value in (("vcenter_id", vcenter_id), ("cluster_name", cluster_name), ("esxi_host", esxi_host)):
            if value:
                filters.append(f"{column}=?")
                params.append(value)
        where = " AND ".join(filters)
        host_rows = [dict(r) for r in self.repo.conn.execute(
            f"SELECT * FROM host_resource_usage_daily WHERE {where} ORDER BY stat_date, service_name, esxi_host", params
        ).fetchall()]
        vm_rows = [dict(r) for r in self.repo.conn.execute(
            f"SELECT * FROM vm_resource_usage_daily WHERE {where} ORDER BY stat_date, service_name, esxi_host, vm_name", params
        ).fetchall()]
        # 쌓여 있는 행도 지금 기준으로 다시 걸러야 한다. 이 변경 전에 수집한
        # 행에는 vCLS 가 들어 있고, 나중에 수동으로 뺀 VM 도 반영되어야 한다.
        vm_rows, dropped = self._apply_scope(vm_rows)
        hosts = self._aggregate(host_rows, ["vcenter_id", "service_name", "cluster_name", "esxi_host"], host=True)
        vms = self._aggregate(vm_rows, ["vcenter_id", "service_name", "vm_uuid", "vm_name"], host=False)
        # 데이터스토어는 ESXi 여럿이 함께 쓴다. esxi_host 로 걸러낼 수 없으므로
        # 그 조건은 빼고 vCenter·클러스터만 본다.
        datastores = self._datastores(start_day, end_day, vcenter_id, cluster_name)
        changes = self._vm_configuration_changes(start_day, end_day, vcenter_id, esxi_host)
        # 사용률과 별개로 할당률을 붙인다. 증설 판단은 할당률로 한다.
        self._apply_allocation(hosts, vms)
        # 통합기의 VM 대수도 걸러낸 목록에서 다시 센다. 저장된 수를 그대로 쓰면
        # 표의 대수와 아래 VM 목록의 줄 수가 어긋난다.
        self._recount_hosts(hosts, vms)
        clusters = self._roll_up_clusters(hosts)
        # 데이터스토어의 VM 대수는 세지 않는 VM 을 뺀 목록에서 다시 센다.
        self._attach_datastore_vms(datastores, vm_rows)
        # vc_0001 · esxi-07 같은 이름으로는 보고서에서 무엇인지 알 수 없다. 업무명이
        # 붙어 있으면 그것을 같이 내려보낸다.
        self._apply_display_names(hosts, vms, changes, clusters, datastores)
        disk = self._disk_totals(datastores)
        return {
            "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
            "hosts": hosts,
            "clusters": clusters,
            "vms": vms,
            "datastores": datastores,
            "disk": disk,
            "changes": changes,
            "filters": self._available_filters(),
            # 무엇을 왜 뺐는지. 대수가 vCenter 화면과 다를 때 따질 수 있어야 한다.
            "scope": dropped,
            "summary": {
                "host_count": len(hosts), "vm_count": len(vms), "cluster_count": len(clusters),
                "datastore_count": len(datastores),
                "disk_capacity_gb": disk.get("capacity_gb"),
                "disk_used_pct": disk.get("used_pct"),
                "disk_provision_pct": disk.get("provision_pct"),
                "disk_over_provisioned": disk.get("over_provisioned"),
                "cpu_changed": sum(1 for r in changes if r["event_type"] == "RV_CPU_CHANGED"),
                "memory_changed": sum(1 for r in changes if r["event_type"] == "RV_MEMORY_CHANGED"),
                "vm_added": sum(1 for r in changes if r["event_type"] == "RV_NEW"),
                "vm_removed": sum(1 for r in changes if r["event_type"] == "RV_REMOVED"),
            },
        }

    def _datastores(
        self,
        start_day: date,
        end_day: date,
        vcenter_id: str | None,
        cluster_name: str | None,
    ) -> list[dict[str, Any]]:
        """기간 안의 마지막 값을 데이터스토어별로 한 줄씩.

        디스크는 CPU·메모리처럼 '평균' 이 쓸모 없다. 지금 얼마나 차 있는지가
        중요하므로 기간 마지막 날의 값을 쓰고, 기간 중 최대치를 같이 적는다.
        """
        filters = ["stat_date>=?", "stat_date<=?"]
        params: list[Any] = [start_day.isoformat(), end_day.isoformat()]
        if vcenter_id:
            filters.append("vcenter_id=?")
            params.append(vcenter_id)
        if cluster_name:
            filters.append("cluster_name=?")
            params.append(cluster_name)
        rows = [dict(r) for r in self.repo.conn.execute(
            f"SELECT * FROM datastore_usage_daily WHERE {' AND '.join(filters)}"
            " ORDER BY stat_date, service_name, datastore_name", params
        ).fetchall()]
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row.get("vcenter_id") or ""), str(row.get("datastore_name") or ""))].append(row)
        result: list[dict[str, Any]] = []
        for (vcenter, name), group in grouped.items():
            latest = sorted(group, key=lambda r: (str(r.get("stat_date") or ""), int(r.get("id") or 0)))[-1]
            capacity = self._int(latest.get("capacity_mb")) or 0
            used = self._int(latest.get("used_mb")) or 0
            provisioned = self._int(latest.get("provisioned_mb")) or 0
            free = self._int(latest.get("free_mb"))
            # 0(접속불가)을 ``or 1`` 로 받으면 1 이 되어 버린다. 없을 때만 1 로 본다.
            raw_accessible = latest.get("accessible")
            accessible = True if raw_accessible is None else bool(int(raw_accessible))
            result.append({
                "vcenter_id": vcenter or None,
                "service_name": latest.get("service_name"),
                "cluster_name": latest.get("cluster_name"),
                "datastore_name": name,
                "datastore_type": latest.get("datastore_type"),
                "accessible": accessible,
                "capacity_mb": capacity, "capacity_gb": self._mb_to_gb(capacity),
                "free_mb": free, "free_gb": self._mb_to_gb(free),
                "used_mb": used, "used_gb": self._mb_to_gb(used),
                "provisioned_mb": provisioned, "provisioned_gb": self._mb_to_gb(provisioned),
                # 사용률은 '지금 차 있는 양', 할당률은 'VM 에게 약속한 양' 이다.
                # 씬 프로비저닝이면 할당률이 100% 를 넘을 수 있고, 그게 위험 신호다.
                "used_pct": self._ratio(used, capacity),
                "provision_pct": self._ratio(provisioned, capacity),
                "over_provisioned": bool(capacity and provisioned > capacity),
                "host_count": self._int(latest.get("host_count")) or 0,
                "used_pct_max": self._max([
                    {"used_pct": self._ratio(self._int(r.get("used_mb")) or 0, self._int(r.get("capacity_mb")) or 0)}
                    for r in group
                ], "used_pct"),
                "latest_stat_date": latest.get("stat_date"),
            })
        result.sort(key=lambda r: (str(r.get("service_name") or ""), str(r.get("datastore_name") or "")))
        return result

    @staticmethod
    def _attach_datastore_vms(datastores: list[dict[str, Any]], vm_rows: list[dict[str, Any]]) -> None:
        """데이터스토어가 어느 VM 들을 담고 있는지는 vCenter 만 아는 값이다.

        수집기는 데이터스토어 단위 VM 대수를 주지 않는다. 적어도 그 vCenter 의
        세는 VM 대수를 적어 두면 "이 데이터스토어가 비어 있는 것인지" 를 가늠할
        수 있다. 정확한 배치가 필요하면 VM 목록을 봐야 한다.
        """
        counted: dict[str, int] = defaultdict(int)
        for vm in vm_rows:
            if str(vm.get("inventory_status") or "CURRENT") != "CURRENT":
                continue
            counted[str(vm.get("vcenter_id") or "")] += 1
        for row in datastores:
            row["vcenter_vm_count"] = counted.get(str(row.get("vcenter_id") or ""), 0)

    def _disk_totals(self, datastores: list[dict[str, Any]]) -> dict[str, Any]:
        """전체 디스크 한 줄 요약. 쓸 수 없는 데이터스토어는 용량에서 뺀다."""
        usable = [r for r in datastores if r.get("accessible")]
        capacity = sum(int(r.get("capacity_mb") or 0) for r in usable)
        used = sum(int(r.get("used_mb") or 0) for r in usable)
        provisioned = sum(int(r.get("provisioned_mb") or 0) for r in usable)
        return {
            "datastore_count": len(datastores),
            "inaccessible_count": len(datastores) - len(usable),
            "capacity_mb": capacity, "capacity_gb": self._mb_to_gb(capacity),
            "used_mb": used, "used_gb": self._mb_to_gb(used),
            "free_mb": capacity - used, "free_gb": self._mb_to_gb(capacity - used),
            "provisioned_mb": provisioned, "provisioned_gb": self._mb_to_gb(provisioned),
            "used_pct": self._ratio(used, capacity),
            "provision_pct": self._ratio(provisioned, capacity),
            "over_provisioned": sum(1 for r in usable if r.get("over_provisioned")),
        }

    def _apply_scope(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """세지 않을 VM 을 걸러낸다. 무엇을 왜 뺐는지 함께 돌려준다."""
        kept: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        dropped_names: list[str] = []
        for row in rows:
            decision = self.scope.decide_vcenter(row)
            if decision.included:
                kept.append(row)
                continue
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
            name = str(row.get("vm_name") or "")
            if name and name not in dropped_names and len(dropped_names) < 20:
                dropped_names.append(name)
        from .asset_scope import REASON_LABELS

        return kept, {
            "excluded_rows": sum(reasons.values()),
            "by_reason": {
                code: {"label": REASON_LABELS.get(code, code), "count": count}
                for code, count in reasons.items()
            },
            "samples": dropped_names,
        }

    @staticmethod
    def _recount_hosts(hosts: list[dict[str, Any]], vms: list[dict[str, Any]]) -> None:
        """통합기의 VM 대수를 **지금 걸러낸 목록에서** 다시 센다.

        저장된 수는 수집 당시의 수다. 그 뒤에 VM 을 자산에서 빼면 목록은 줄지만
        저장된 수는 그대로다. 예전에는 '세어 본 값이 있을 때만' 덮어써서, 어느
        통합기의 VM 이 전부 빠지면 그 통합기만 옛 수를 들고 있었다. 그러면 통합기
        표의 합이 아래 VM 목록의 줄 수보다 커진다 -- 실제로 그랬다.

        그래서 세어 본 값이 없으면 0 으로 적는다. 0 이 맞는 값이다. 대신 저장된
        수를 ``stored_vm_count`` 로 남겨, 수집 자체가 비었을 때 따져볼 수 있게 한다.
        """
        counted: dict[tuple[str, str], int] = defaultdict(int)
        for vm in vms:
            if str(vm.get("inventory_status") or "CURRENT") != "CURRENT":
                continue
            counted[(str(vm.get("vcenter_id") or ""), str(vm.get("esxi_host") or ""))] += 1
        for row in hosts:
            key = (str(row.get("vcenter_id") or ""), str(row.get("esxi_host") or ""))
            row["stored_vm_count"] = row.get("vm_count")
            row["vm_count"] = counted.get(key, 0)

    def _apply_allocation(self, hosts: list[dict[str, Any]], vms: list[dict[str, Any]]) -> None:
        """통합기별 VM 할당량과 할당률을 붙인다.

        사용률(cpu_avg_pct 등)은 '실제로 쓴 양' 이고, 할당률은 'VM 에게 나눠준 양' 이다.
        둘은 다르다. 메모리 1TB 짜리 통합기에 4GB 씩 10 대를 만들었다면 사용률이 5%
        라도 할당률은 40/1024 = 3.9% 다. 더 만들 수 있는지는 할당률로만 알 수 있다.

        VM 목록에 없는(NOT_IN_CURRENT_INVENTORY) 행은 이미 지워진 VM 이므로 뺀다.
        """
        assigned: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"cpu": 0, "memory_mb": 0, "vm_count": 0, "disk_mb": 0, "disk_used_mb": 0}
        )
        for vm in vms:
            if str(vm.get("inventory_status") or "CURRENT") != "CURRENT":
                continue
            host = str(vm.get("esxi_host") or "")
            if not host:
                continue
            bucket = assigned[(str(vm.get("vcenter_id") or ""), host)]
            bucket["cpu"] += int(vm.get("allocated_cpu_cores") or 0)
            bucket["memory_mb"] += int(vm.get("allocated_memory_mb") or 0)
            # 디스크는 ESXi 용량이 아니라 데이터스토어 용량에서 나간다. 그래서
            # 통합기 줄에는 비율 없이 '이 통합기의 VM 이 차지한 양' 만 적는다.
            bucket["disk_mb"] += int(vm.get("provisioned_disk_mb") or 0)
            bucket["disk_used_mb"] += int(vm.get("used_disk_mb") or 0)
            bucket["vm_count"] += 1

        for row in hosts:
            bucket = assigned.get((str(row.get("vcenter_id") or ""), str(row.get("esxi_host") or "")))
            cpu = int(bucket["cpu"]) if bucket else 0
            memory_mb = int(bucket["memory_mb"]) if bucket else 0
            disk_mb = int(bucket["disk_mb"]) if bucket else 0
            disk_used_mb = int(bucket["disk_used_mb"]) if bucket else 0
            row.update({
                "assigned_cpu_cores": cpu,
                "assigned_memory_mb": memory_mb,
                "assigned_memory_gb": self._mb_to_gb(memory_mb),
                "assigned_disk_mb": disk_mb,
                "assigned_disk_gb": self._mb_to_gb(disk_mb),
                "used_disk_mb": disk_used_mb,
                "used_disk_gb": self._mb_to_gb(disk_used_mb),
                # 할당한 디스크 중 실제로 쓴 비율. 씬 디스크가 얼마나 비어 있는지다.
                "disk_fill_pct": self._ratio(disk_used_mb, disk_mb),
                "assigned_vm_count": int(bucket["vm_count"]) if bucket else 0,
                "cpu_alloc_pct": self._ratio(cpu, row.get("allocated_cpu_cores")),
                "mem_alloc_pct": self._ratio(memory_mb, row.get("allocated_memory_mb")),
            })

    def _roll_up_clusters(self, hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """통합기(클러스터) 단위 합. 통합기 한 대는 ESXi 여러 대의 묶음이다.

        ESXi 한 줄만 봐서는 통합기 전체에 얼마가 남았는지 알 수 없으므로 여기서 묶는다.
        """
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in hosts:
            grouped[(
                str(row.get("vcenter_id") or ""),
                str(row.get("service_name") or ""),
                str(row.get("cluster_name") or ""),
            )].append(row)

        result: list[dict[str, Any]] = []
        for (vcenter_id, service_name, cluster_name), members in grouped.items():
            capacity_cpu = sum(int(m.get("allocated_cpu_cores") or 0) for m in members)
            capacity_mem = sum(int(m.get("allocated_memory_mb") or 0) for m in members)
            assigned_cpu = sum(int(m.get("assigned_cpu_cores") or 0) for m in members)
            assigned_mem = sum(int(m.get("assigned_memory_mb") or 0) for m in members)
            assigned_disk = sum(int(m.get("assigned_disk_mb") or 0) for m in members)
            used_disk = sum(int(m.get("used_disk_mb") or 0) for m in members)
            result.append({
                "vcenter_id": vcenter_id or None,
                "service_name": service_name or None,
                "cluster_name": cluster_name or None,
                "host_count": len(members),
                "vm_count": sum(int(m.get("vm_count") or 0) for m in members),
                "allocated_cpu_cores": capacity_cpu,
                "allocated_memory_mb": capacity_mem,
                "allocated_memory_gb": self._mb_to_gb(capacity_mem),
                "assigned_cpu_cores": assigned_cpu,
                "assigned_memory_mb": assigned_mem,
                "assigned_memory_gb": self._mb_to_gb(assigned_mem),
                "assigned_disk_mb": assigned_disk,
                "assigned_disk_gb": self._mb_to_gb(assigned_disk),
                "used_disk_mb": used_disk,
                "used_disk_gb": self._mb_to_gb(used_disk),
                "disk_fill_pct": self._ratio(used_disk, assigned_disk),
                "cpu_alloc_pct": self._ratio(assigned_cpu, capacity_cpu),
                "mem_alloc_pct": self._ratio(assigned_mem, capacity_mem),
                "cpu_max_pct": self._max(members, "cpu_max_pct"),
                "cpu_avg_pct": self._weighted_avg(members, "cpu_avg_pct"),
                "mem_max_pct": self._max(members, "mem_max_pct"),
                "mem_avg_pct": self._weighted_avg(members, "mem_avg_pct"),
                "sample_count": sum(int(m.get("sample_count") or 0) for m in members),
            })
        result.sort(key=lambda r: (str(r.get("service_name") or ""), str(r.get("cluster_name") or "")))
        return result

    def _apply_display_names(self, *row_groups: list[dict[str, Any]]) -> None:
        """통합기(클러스터)·ESXi 업무명을 각 행에 붙인다.

        원래 이름은 지우지 않는다. vCenter 화면에서 찾을 때 필요하다.
        """
        resolver = DisplayNameService(self.repo).resolver()
        for rows in row_groups:
            for row in rows:
                vcenter = row.get("vcenter_id") or ""
                if row.get("cluster_name"):
                    row["cluster_display_name"] = resolver.name("CLUSTER", row["cluster_name"], vcenter)
                if row.get("esxi_host"):
                    row["esxi_display_name"] = resolver.name("ESXI", row["esxi_host"], vcenter)
                if row.get("datastore_name"):
                    row["datastore_display_name"] = resolver.name(
                        "DATASTORE", row["datastore_name"], vcenter
                    )
                if vcenter:
                    row["vcenter_display_name"] = resolver.name("VCENTER", vcenter, vcenter)

    def export_xlsx(self, start: str, end: str, target_dir: Path, **filters: Any) -> Path:
        data = self.summary(start, end, **filters)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"통합서버_자원사용현황_{start}_{end}.xlsx"
        wb = Workbook()
        ws_host = wb.active
        ws_host.title = "HostResourceUsage"
        self._write_sheet(ws_host, [
            ("서비스명", "service_name"), ("vCenter", "vcenter_id"), ("Cluster", "cluster_name"),
            ("통합기", "esxi_host"), ("VM 대수", "vm_count"), ("실제 CPU Core", "allocated_cpu_cores"),
            ("실제 Memory GB", "allocated_memory_gb"),
            ("VM 할당 CPU Core", "assigned_cpu_cores"), ("VM 할당 Memory GB", "assigned_memory_gb"),
            ("CPU 할당률 %", "cpu_alloc_pct"), ("MEM 할당률 %", "mem_alloc_pct"),
            ("VM 할당 Disk GB", "assigned_disk_gb"), ("VM 사용 Disk GB", "used_disk_gb"),
            ("Disk 실사용률 %", "disk_fill_pct"),
            ("CPU MAX %", "cpu_max_pct"),
            ("CPU AVG %", "cpu_avg_pct"), ("MEM MAX %", "mem_max_pct"), ("MEM AVG %", "mem_avg_pct"),
        ], data["hosts"])
        ws_cluster = wb.create_sheet("ClusterResourceUsage")
        self._write_sheet(ws_cluster, [
            ("서비스명", "service_name"), ("vCenter", "vcenter_id"), ("통합기(Cluster)", "cluster_name"),
            ("ESXi 대수", "host_count"), ("VM 대수", "vm_count"),
            ("실제 CPU Core", "allocated_cpu_cores"), ("실제 Memory GB", "allocated_memory_gb"),
            ("VM 할당 CPU Core", "assigned_cpu_cores"), ("VM 할당 Memory GB", "assigned_memory_gb"),
            ("CPU 할당률 %", "cpu_alloc_pct"), ("MEM 할당률 %", "mem_alloc_pct"),
            ("VM 할당 Disk GB", "assigned_disk_gb"), ("VM 사용 Disk GB", "used_disk_gb"),
            ("Disk 실사용률 %", "disk_fill_pct"),
            ("CPU MAX %", "cpu_max_pct"), ("CPU AVG %", "cpu_avg_pct"),
            ("MEM MAX %", "mem_max_pct"), ("MEM AVG %", "mem_avg_pct"),
        ], data["clusters"])
        ws_datastore = wb.create_sheet("DatastoreUsage")
        self._write_sheet(ws_datastore, [
            ("서비스명", "service_name"), ("vCenter", "vcenter_id"), ("Cluster", "cluster_name"),
            ("데이터스토어", "datastore_name"), ("유형", "datastore_type"),
            ("실제 용량 GB", "capacity_gb"), ("사용 GB", "used_gb"), ("여유 GB", "free_gb"),
            ("사용률 %", "used_pct"), ("기간 내 최대 사용률 %", "used_pct_max"),
            ("VM 할당(프로비저닝) GB", "provisioned_gb"), ("할당률 %", "provision_pct"),
            ("과할당", "over_provisioned_label"), ("접속 ESXi 대수", "host_count"),
            ("기준일", "latest_stat_date"),
        ], self._datastore_export_rows(data["datastores"]))
        ws_vm = wb.create_sheet("VMsResource")
        self._write_sheet(ws_vm, [
            ("서비스명", "service_name"), ("vCenter", "vcenter_id"), ("Cluster", "cluster_name"),
            ("통합기", "esxi_host"), ("VM UUID", "vm_uuid"), ("VM명", "vm_name"),
            ("전원상태", "power_state"), ("실제 CPU Core", "allocated_cpu_cores"),
            ("실제 Memory GB", "allocated_memory_gb"),
            ("할당 Disk GB", "provisioned_disk_gb"), ("사용 Disk GB", "used_disk_gb"),
            ("Disk 실사용률 %", "disk_fill_pct"),
            ("CPU MAX %", "cpu_max_pct"),
            ("CPU AVG %", "cpu_avg_pct"), ("MEM MAX %", "mem_max_pct"), ("MEM AVG %", "mem_avg_pct"),
        ], data["vms"])
        ws_change = wb.create_sheet("VMChangeHistory")
        self._write_sheet(ws_change, [
            ("변경일시", "detected_at"), ("변경유형", "change_label"), ("VM명", "vm_name"),
            ("호스트명", "hostname"), ("IP", "primary_ip"), ("OS", "os_family"),
            ("vCenter", "vcenter_id"), ("통합기", "esxi_host"),
            ("변경항목", "field_label"), ("이전값", "old_value_display"), ("현재값", "new_value_display"),
        ], self._change_export_rows(data["changes"]))
        wb.save(target)
        wb.close()
        return target

    def import_file(self, file_path: Path, stat_date: str | None = None) -> dict[str, Any]:
        """Compatibility import for previously exported JSON/CSV/XLSX files."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(path)
        target_date = stat_date or (date.today() - timedelta(days=1)).isoformat()
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            rows = data if isinstance(data, list) else data.get("records", [])
        elif suffix == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        elif suffix in {".xlsx", ".xlsm"}:
            workbook = load_workbook(path, read_only=True, data_only=True)
            sheet = workbook.active
            values = sheet.iter_rows(values_only=True)
            headers = [str(v or "").strip() for v in next(values)]
            rows = [dict(zip(headers, row)) for row in values]
            workbook.close()
        else:
            raise ValueError("지원 파일은 JSON, CSV, XLSX입니다.")
        normalized = [self._normalize(row) for row in rows if any(v not in (None, "") for v in row.values())]
        count = self.repo.replace_resource_usage(target_date, normalized)
        return {"status": "SUCCESS", "stat_date": target_date, "count": count, "source": path.name}

    def _aggregate(self, rows: list[dict[str, Any]], keys: list[str], *, host: bool) -> list[dict[str, Any]]:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[tuple(row.get(k) for k in keys)].append(row)
        result: list[dict[str, Any]] = []
        for key, group in grouped.items():
            latest = sorted(group, key=lambda r: (r.get("stat_date") or "", r.get("id") or 0))[-1]
            item = {name: value for name, value in zip(keys, key)}
            item.update({
                "cluster_name": latest.get("cluster_name"),
                "esxi_host": latest.get("esxi_host"),
                "vm_count": latest.get("vm_count") if host else None,
                "power_state": latest.get("power_state"),
                "allocated_cpu_cores": latest.get("allocated_cpu_cores"),
                "allocated_memory_mb": latest.get("allocated_memory_mb"),
                "allocated_memory_gb": self._mb_to_gb(latest.get("allocated_memory_mb")),
                # 디스크는 평균이 쓸모 없다. 마지막 날의 값이 지금 모습이다.
                "provisioned_disk_mb": latest.get("provisioned_disk_mb"),
                "provisioned_disk_gb": self._mb_to_gb(latest.get("provisioned_disk_mb")),
                "used_disk_mb": latest.get("used_disk_mb"),
                "used_disk_gb": self._mb_to_gb(latest.get("used_disk_mb")),
                "disk_fill_pct": self._ratio(
                    latest.get("used_disk_mb") or 0, latest.get("provisioned_disk_mb")
                ),
                "inventory_status": latest.get("inventory_status"),
                "cpu_max_pct": self._max(group, "cpu_max_pct"),
                "cpu_avg_pct": self._weighted_avg(group, "cpu_avg_pct"),
                "mem_max_pct": self._max(group, "mem_max_pct"),
                "mem_avg_pct": self._weighted_avg(group, "mem_avg_pct"),
                "sample_count": sum(int(r.get("sample_count") or 0) for r in group),
                "latest_stat_date": latest.get("stat_date"),
            })
            result.append(item)
        result.sort(key=lambda r: tuple(str(r.get(k) or "") for k in keys))
        return result

    def _vm_configuration_changes(self, start_day: date, end_day: date, vcenter_id: str | None, esxi_host: str | None) -> list[dict[str, Any]]:
        start_dt = datetime.combine(start_day, datetime.min.time()).isoformat()
        end_dt = datetime.combine(end_day + timedelta(days=1), datetime.min.time()).isoformat()
        events = self.repo.changes("RVTOOLS", 100000, start_dt, end_dt)
        wanted = {"RV_NEW", "RV_REMOVED", "RV_CPU_CHANGED", "RV_MEMORY_CHANGED", "RV_HOST_CHANGED", "RV_VCENTER_CHANGED"}
        cache: dict[int, dict[str, dict[str, Any]]] = {}
        result = []
        for event in events:
            if event.get("event_type") not in wanted:
                continue
            records = []
            for sid in (event.get("snapshot_id"), event.get("previous_snapshot_id")):
                if not sid:
                    continue
                sid = int(sid)
                if sid not in cache:
                    cache[sid] = self.repo.load_rv_records(sid)
                if cache[sid].get(event["asset_key"]):
                    records.append(cache[sid][event["asset_key"]])
            vm = records[0] if records else {}
            # 세지 않는 VM 의 생성·삭제는 내역에도 올리지 않는다. vCLS 는 매일
            # 다시 만들어지므로 그냥 두면 진짜 변경이 묻힌다.
            if vm and not self.scope.decide_vcenter(vm).included:
                continue
            vc = str(vm.get("vcenter") or "")
            host = str(vm.get("esxi_host") or "")
            if vcenter_id and vc != vcenter_id:
                continue
            if esxi_host and host != esxi_host and event.get("old_value") != esxi_host and event.get("new_value") != esxi_host:
                continue
            # 자산키(vc|uuid) 만 적으면 무엇이 바뀐 건지 읽을 수 없다. 이름·IP·
            # OS 를 붙이고, 값은 코드·원본 JSON 이 아니라 사람이 읽는 표현으로 바꾼다.
            row = present({
                "source": "RVTOOLS",
                "detected_at": event.get("detected_at"),
                "asset_key": event.get("asset_key"),
                "event_type": event.get("event_type"),
                "field_name": event.get("field_name"),
                "old_value": event.get("old_value"),
                "new_value": event.get("new_value"),
            })
            fallback = self._record_from_event(event)
            row.update({
                "vcenter_id": vc or str(fallback.get("vcenter") or ""),
                "esxi_host": host or str(fallback.get("esxi_host") or ""),
                "vm_name": vm.get("vm_name") or fallback.get("vm_name") or event.get("asset_key"),
                "hostname": vm.get("normalized_hostname") or fallback.get("normalized_hostname"),
                "primary_ip": vm.get("primary_ip") or fallback.get("primary_ip"),
                "os_family": vm.get("os_family") or fallback.get("os_family"),
                "power_state": vm.get("power_state") or fallback.get("power_state"),
                "change_label": CHANGE_LABELS.get(str(event.get("event_type")), str(event.get("event_type") or "")),
            })
            result.append(row)
        return result

    @staticmethod
    def _record_from_event(event: dict[str, Any]) -> dict[str, Any]:
        """삭제된 VM 은 지금 스냅샷에 없다. 이벤트가 들고 있는 원본에서 찾는다."""
        for side in ("new_value", "old_value"):
            text = event.get(side)
            if isinstance(text, str) and text.strip().startswith("{"):
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def _distinct(self, column: str, where: str = "") -> list[str]:
        # 컬럼명으로 읽는다. MySQL 커서는 dict 를 돌려주므로 위치 색인은 쓸 수 없다.
        rows = self.repo.conn.execute(
            f"SELECT DISTINCT {column} AS value FROM host_resource_usage_daily {where} ORDER BY {column}"
        ).fetchall()
        return [str(row["value"]) for row in rows if row["value"]]

    def _available_filters(self) -> dict[str, list[str]]:
        return {
            "vcenters": self._distinct("vcenter_id"),
            "clusters": self._distinct("cluster_name", "WHERE cluster_name IS NOT NULL"),
            "hosts": self._distinct("esxi_host"),
        }

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        mapped: dict[str, Any] = {}
        for key, value in raw.items():
            alias = self.COLUMN_ALIASES.get(str(key).strip().upper())
            if alias:
                mapped[alias] = value
        entity_type = str(mapped.get("entity_type") or ("VM" if mapped.get("vm_name") or mapped.get("vm_uuid") else "ESXI")).upper()
        if entity_type not in {"VM", "ESXI"}:
            raise ValueError(f"ENTITY_TYPE은 VM 또는 ESXI여야 합니다: {entity_type}")
        return {
            "entity_type": entity_type,
            "vcenter_id": self._text(mapped.get("vcenter_id")), "service_name": self._text(mapped.get("service_name")),
            "cluster_name": self._text(mapped.get("cluster_name")), "esxi_host": self._text(mapped.get("esxi_host")),
            "vm_uuid": self._text(mapped.get("vm_uuid")), "vm_name": self._text(mapped.get("vm_name")),
            "power_state": self._text(mapped.get("power_state")),
            "allocated_cpu_cores": self._int(mapped.get("allocated_cpu_cores")),
            "allocated_memory_mb": self._int(mapped.get("allocated_memory_mb")),
            "provisioned_disk_mb": self._int(mapped.get("provisioned_disk_mb")),
            "used_disk_mb": self._int(mapped.get("used_disk_mb")),
            "cpu_max_pct": self._float(mapped.get("cpu_max_pct")), "cpu_avg_pct": self._float(mapped.get("cpu_avg_pct")),
            "mem_max_pct": self._float(mapped.get("mem_max_pct")), "mem_avg_pct": self._float(mapped.get("mem_avg_pct")),
            "sample_count": self._int(mapped.get("sample_count")) or 0,
            "collection_status": "SUCCESS", "source_name": "VM_ResourceUsageExport", "raw": raw,
        }

    @staticmethod
    def _datastore_export_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """엑셀에 쓸 모양으로. True/False 대신 읽을 수 있는 말을 넣는다."""
        result = []
        for source in rows:
            row = dict(source)
            row["over_provisioned_label"] = "과할당" if row.get("over_provisioned") else ""
            if not row.get("accessible"):
                row["over_provisioned_label"] = "접속불가"
            result.append(row)
        return result

    @classmethod
    def _change_export_rows(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """엑셀에 쓸 모양으로. 값은 이미 present() 가 읽을 수 있게 만들어 두었다.

        생성·삭제 이벤트는 값 자리에 원본 전체(JSON)가 들어 있다. 그걸 그대로
        셀에 넣으면 무엇이 바뀐 건지 알 수 없으므로 요약 문장을 쓴다.
        """
        result: list[dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            row.setdefault("change_label", row.get("event_type"))
            row.setdefault("field_label", row.get("field_name") or "")
            is_memory = row.get("event_type") == "RV_MEMORY_CHANGED" or "MEMORY" in str(row.get("field_name") or "").upper()
            for side in ("old", "new"):
                display = row.get(f"{side}_display")
                if is_memory and not str(display or "").endswith("GB"):
                    display = cls._memory_change_display(row.get(f"{side}_value"))
                if display in (None, ""):
                    display = row.get(f"{side}_value")
                row[f"{side}_value_display"] = display
            result.append(row)
        return result

    @classmethod
    def _memory_change_display(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        try:
            gb = cls._mb_to_gb(value)
        except (TypeError, ValueError):
            return str(value)
        if gb is None:
            return None
        return f"{gb:g} GB"

    @staticmethod
    def _mb_to_gb(value: Any) -> float | None:
        """MB 를 GB 로. 1GB 이상이면 정수로 반올림한다.

        ESXi 는 하이퍼바이저가 쓰는 만큼을 뺀 값을 알려준다. 1TB 짜리 장비가
        1048234MB = 1023.66GB 로 나오는 식이다. 장표에는 1024 로 적어야 하므로
        여기서 반올림한다. 512MB 같은 작은 VM 은 0 이 되면 안 되니 소수로 둔다.
        """
        if value in (None, ""):
            return None
        gb = float(str(value).replace(",", "").strip()) / 1024
        return float(round(gb)) if abs(gb) >= 1 else round(gb, 2)

    @staticmethod
    def _ratio(assigned: Any, capacity: Any) -> float | None:
        """할당률(%). 용량을 모르면 비율도 없다 -- 0 으로 적으면 여유가 있다고 읽힌다."""
        try:
            total = float(capacity or 0)
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        return round(float(assigned or 0) / total * 100, 2)

    @staticmethod
    def _write_sheet(ws: Any, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
        ws.append([label for label, _ in columns])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="ED7D31")
        for row in rows:
            ws.append([row.get(key) for _, key in columns])
        # 표시 형식. '0.##' 은 16 을 "16." 으로 보여준다 -- 엑셀이 소수점 자리가
        # 비어도 점은 찍기 때문이다. General 은 16 을 "16", 0.5 를 "0.5" 로 적는다.
        for index, (_, key) in enumerate(columns, start=1):
            if key.endswith("_gb") or key.endswith("_pct"):
                for cell in ws.iter_cols(min_col=index, max_col=index, min_row=2):
                    for item in cell:
                        item.number_format = "General"
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for column in ws.columns:
            width = min(max(len(str(cell.value or "")) for cell in column) + 2, 40)
            ws.column_dimensions[column[0].column_letter].width = width

    @staticmethod
    def _max(rows: list[dict[str, Any]], field: str) -> float | None:
        values = [float(r[field]) for r in rows if r.get(field) is not None]
        return round(max(values), 2) if values else None

    @staticmethod
    def _weighted_avg(rows: list[dict[str, Any]], field: str) -> float | None:
        values = [(float(r[field]), int(r.get("sample_count") or 0)) for r in rows if r.get(field) is not None]
        if not values:
            return None
        weight = sum(w for _, w in values)
        return round(sum(v * (w or 1) for v, w in values) / (weight or len(values)), 2)

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        return float(str(value).replace("%", "").strip())

    @staticmethod
    def _int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        return int(float(str(value).replace(",", "").strip()))
