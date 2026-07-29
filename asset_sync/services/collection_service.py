from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ..collectors import (
    ITSMFileCollector,
    OracleITSMCollector,
    PowerCLICollector,
    SyntheticITSMCollector,
    SyntheticRVToolsCollector,
    VCenterSnapshotFileCollector,
)
from ..config import AppConfig
from ..db.sqlite_manager import SQLiteManager
from ..repositories import AssetRepository
from .diff_service import DiffService
from .quality_service import DataQualityService
from .reconciliation_service import ReconciliationService
from .snapshot_service import SnapshotService
from .resource_usage_service import VMResourceUsageExportService

LOGGER = logging.getLogger(__name__)


class CollectionService:
    """Orchestrate lightweight collection, snapshots, diffs and reconciliation."""

    def __init__(self, config: AppConfig, manager: SQLiteManager) -> None:
        self.config = config
        self.manager = manager

    def collect_itsm(self, mode: str | None = None, files: list[Path] | None = None) -> dict[str, Any]:
        started = datetime.now()
        selected_mode = str(mode or self.config.itsm.get("collection_mode", "ORACLE")).upper()
        with self.manager.connect() as conn:
            repo = AssetRepository(conn)
            run_id = repo.start_collection_run("ITSM", started.isoformat())
            try:
                raw, collector_metadata = self._collect_itsm_raw(selected_mode, files)
                previous_run = repo.conn.execute(
                    "SELECT metadata_json FROM collection_run WHERE source='ITSM' AND status='SUCCESS' ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                previous_query_hash = None
                if previous_run:
                    import json
                    previous_query_hash = (json.loads(previous_run["metadata_json"] or "{}") or {}).get("collector", {}).get("query_hash")
                query_changed = bool(
                    selected_mode == "ORACLE"
                    and previous_query_hash
                    and previous_query_hash != collector_metadata.get("query_hash")
                )
                snapshot_service = SnapshotService(self.config, repo)
                records, validation = snapshot_service.normalize_itsm(raw)
                baseline = self._check_baseline(repo, "ITSM", len(records))
                snapshot_id = snapshot_service.save_itsm_snapshot(run_id, records, datetime.now())
                if query_changed:
                    diff = {
                        "status": "QUERY_CHANGED",
                        "events": [],
                        "message": "Oracle 조회 SQL이 변경되어 삭제/변경 이벤트 생성을 보류합니다.",
                    }
                elif baseline.get("critical"):
                    diff = {
                        "status": "COLLECTION_ANOMALY",
                        "events": [],
                        "message": "수집 건수가 임계값 미만이어서 삭제/변경 이벤트 생성을 보류합니다.",
                    }
                else:
                    diff = DiffService(self.config, repo).compare_itsm(snapshot_id)
                quality = DataQualityService(self.config, repo).run(snapshot_id)
                status = "PARTIAL_SUCCESS" if baseline.get("warning") else "SUCCESS"
                repo.finish_collection_run(
                    run_id,
                    status,
                    len(records),
                    datetime.now().isoformat(),
                    ["ALL"],
                    metadata={
                        "validation": validation,
                        "collector": collector_metadata,
                        "query_changed": query_changed,
                        "baseline": baseline,
                    },
                )
                return {
                    "status": status,
                    "mode": selected_mode,
                    "run_id": run_id,
                    "snapshot_id": snapshot_id,
                    "count": len(records),
                    "diff": diff,
                    "quality": quality,
                    "baseline": baseline,
                }
            except Exception as exc:
                repo.finish_collection_run(
                    run_id,
                    "FAILED",
                    0,
                    datetime.now().isoformat(),
                    error_message=str(exc),
                    metadata={"mode": selected_mode},
                )
                raise

    def collect_vcenter(self, mode: str | None = None, files: list[Path] | None = None) -> dict[str, Any]:
        """Collect vCenter inventory through PowerCLI and compare with the prior snapshot.

        The SQLite source identifier remains RVTOOLS for backward compatibility with
        existing history tables. UI and collection logic use the vCenter/PowerCLI terms.
        """
        started = datetime.now()
        selected_mode = str(mode or self.config.rvtools.get("collection_mode", "POWERCLI")).upper()
        collector_metadata: dict[str, Any] | None = None
        with self.manager.connect() as conn:
            repo = AssetRepository(conn)
            run_id = repo.start_collection_run("RVTOOLS", started.isoformat())
            try:
                if selected_mode == "DEMO":
                    raw, collector_metadata = SyntheticRVToolsCollector(self.config).collect()
                    collector_metadata["mode"] = "DEMO"
                elif selected_mode == "FILE_ONLY":
                    raw, collector_metadata = VCenterSnapshotFileCollector(self.config).collect(files)
                elif selected_mode == "POWERCLI":
                    raw, collector_metadata = PowerCLICollector(self.config).collect_all()
                else:
                    raise RuntimeError("vCenter collection_mode는 DEMO, FILE_ONLY, POWERCLI 중 하나여야 합니다.")

                snapshot_service = SnapshotService(self.config, repo)
                records, validation = snapshot_service.normalize_rvtools(raw)
                baseline = self._check_baseline(repo, "RVTOOLS", len(records))
                failed_scopes = list((collector_metadata or {}).get("failed_scopes", {}).keys())
                status = "PARTIAL_SUCCESS" if failed_scopes or baseline.get("warning") else "SUCCESS"
                snapshot_id = snapshot_service.save_rv_snapshot(run_id, records, datetime.now(), status)
                if baseline.get("critical"):
                    diff = {
                        "status": "COLLECTION_ANOMALY",
                        "events": [],
                        "message": "vCenter 수집 건수가 임계값 미만이어서 삭제 이벤트 생성을 보류합니다.",
                    }
                else:
                    diff = DiffService(self.config, repo).compare_rvtools(snapshot_id)
                repo.finish_collection_run(
                    run_id,
                    status,
                    len(records),
                    datetime.now().isoformat(),
                    (collector_metadata or {}).get("success_scopes", []),
                    failed_scopes,
                    metadata={
                        "collector": collector_metadata,
                        "validation": validation,
                        "baseline": baseline,
                        "mode": selected_mode,
                    },
                )
                return {
                    "status": status,
                    "mode": selected_mode,
                    "run_id": run_id,
                    "snapshot_id": snapshot_id,
                    "count": len(records),
                    "diff": diff,
                    "collector_metadata": collector_metadata,
                    "baseline": baseline,
                }
            except Exception as exc:
                repo.finish_collection_run(
                    run_id,
                    "FAILED",
                    0,
                    datetime.now().isoformat(),
                    error_message=str(exc),
                    metadata={"collector": collector_metadata, "mode": selected_mode},
                )
                raise

    def collect_rvtools(self, mode: str | None = None, files: list[Path] | None = None) -> dict[str, Any]:
        """Backward-compatible alias. New code should call collect_vcenter()."""
        return self.collect_vcenter(mode=mode, files=files)

    def run_daily(self, demo: bool = False) -> dict[str, Any]:
        """Run the 07:00 automation pipeline and persist one parent batch record."""
        started = datetime.now()
        with self.manager.connect() as conn:
            batch_id = AssetRepository(conn).start_daily_batch(started.date().isoformat(), started.isoformat())

        results: dict[str, Any] = {"batch_id": batch_id}
        itsm_mode = "DEMO" if demo else str(self.config.itsm.get("collection_mode", "ORACLE")).upper()
        rv_mode = "DEMO" if demo else str(self.config.rvtools.get("collection_mode", "POWERCLI")).upper()
        rv_ok = itsm_ok = False
        errors: dict[str, str] = {}

        try:
            results["vcenter"] = self.collect_vcenter(mode=rv_mode)
            rv_ok = results["vcenter"].get("status") in {"SUCCESS", "PARTIAL_SUCCESS"}
            if results["vcenter"].get("baseline", {}).get("critical"):
                rv_ok = False
        except Exception as exc:
            LOGGER.exception("vCenter PowerCLI daily collection failed")
            errors["vcenter"] = str(exc)
            results["vcenter"] = {"status": "FAILED", "error": str(exc), "count": 0, "mode": rv_mode}

        try:
            results["itsm"] = self.collect_itsm(mode=itsm_mode)
            itsm_ok = results["itsm"].get("status") in {"SUCCESS", "PARTIAL_SUCCESS"}
            if results["itsm"].get("baseline", {}).get("critical"):
                itsm_ok = False
        except Exception as exc:
            LOGGER.exception("ITSM daily collection failed")
            errors["itsm"] = str(exc)
            results["itsm"] = {"status": "FAILED", "error": str(exc), "count": 0, "mode": itsm_mode}

        if rv_ok and itsm_ok:
            with self.manager.connect() as conn:
                results["reconciliation"] = ReconciliationService(self.config, AssetRepository(conn)).reconcile(
                    int(results["itsm"]["snapshot_id"]), int(results["vcenter"]["snapshot_id"])
                )
        else:
            results["reconciliation"] = {
                "status": "SKIPPED",
                "reason": "같은 일일 배치에서 vCenter와 ITSM 수집이 모두 정상일 때만 정합성을 실행합니다.",
                "counts": {},
            }

        if rv_ok and results["vcenter"].get("snapshot_id"):
            with self.manager.connect() as conn:
                resource_usage = VMResourceUsageExportService(self.config, AssetRepository(conn)).collect_for_batch(
                    batch_id, int(results["vcenter"]["snapshot_id"]), demo=demo
                )
            if resource_usage.get("status") == "FAILED":
                errors["resource_usage"] = str(resource_usage.get("error") or "자원사용률 수집 실패")
        else:
            resource_usage = {
                "status": "SKIPPED",
                "reason": "같은 배치의 정상 vCenter 인벤토리가 없어 자원사용률 수집을 건너뜁니다.",
            }
        results["resource_usage"] = resource_usage

        if rv_ok and itsm_ok:
            results["status"] = (
                "SUCCESS"
                if results["vcenter"].get("status") == "SUCCESS" and results["itsm"].get("status") == "SUCCESS"
                else "PARTIAL_SUCCESS"
            )
        elif rv_ok or itsm_ok:
            results["status"] = "PARTIAL_SUCCESS"
        else:
            results["status"] = "FAILED"
        if results["status"] == "SUCCESS" and resource_usage.get("status") in {"FAILED", "PARTIAL_SUCCESS"}:
            results["status"] = "PARTIAL_SUCCESS"

        reconciliation_created_at = None
        if results["reconciliation"].get("results"):
            reconciliation_created_at = results["reconciliation"]["results"][0].get("created_at")
        with self.manager.connect() as conn:
            AssetRepository(conn).finish_daily_batch(
                batch_id,
                status=results["status"],
                ended_at=datetime.now().isoformat(),
                itsm_run_id=results["itsm"].get("run_id"),
                itsm_snapshot_id=results["itsm"].get("snapshot_id"),
                vcenter_run_id=results["vcenter"].get("run_id"),
                vcenter_snapshot_id=results["vcenter"].get("snapshot_id"),
                reconciliation_created_at=reconciliation_created_at,
                resource_usage_status=resource_usage.get("status", "PENDING_SCRIPT"),
                errors=errors,
                metadata={
                    "itsm_mode": itsm_mode,
                    "vcenter_mode": rv_mode,
                    "reconciliation_counts": results["reconciliation"].get("counts", {}),
                    "resource_usage_run_id": resource_usage.get("run_id"),
                    "resource_usage_period": {
                        "start": resource_usage.get("period_start"),
                        "end": resource_usage.get("period_end"),
                    },
                },
            )
        return results

    def _collect_itsm_raw(self, mode: str, files: list[Path] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if mode == "DEMO":
            collector = SyntheticITSMCollector(self.config)
            return collector.collect(), collector.last_metadata
        if mode == "FILE_ONLY":
            collector = ITSMFileCollector(self.config)
            return collector.collect(files), collector.last_metadata
        if mode == "ORACLE":
            if not bool(self.config.oracle.get("enabled", False)):
                raise RuntimeError("Oracle 수집이 비활성화되어 있습니다. 연동정보 관리에서 활성화하세요.")
            collector = OracleITSMCollector(self.config)
            return collector.collect(), collector.last_metadata
        raise RuntimeError("ITSM collection_mode는 DEMO, FILE_ONLY, ORACLE 중 하나여야 합니다.")

    def _archive_rv_files(self, file_meta: dict[str, Any]) -> dict[str, Any]:
        date_dir = datetime.now().strftime("%Y%m%d")
        archive_dir = self.config.resolve(self.config.rvtools.get("archive_dir", "data/archive")) / date_dir
        failed_dir = self.config.resolve(self.config.rvtools.get("failed_dir", "data/failed")) / date_dir
        archive_dir.mkdir(parents=True, exist_ok=True)
        failed_dir.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        errors: list[dict[str, str]] = []
        for key, target_dir in (("files", archive_dir), ("failed_files", failed_dir)):
            for item in file_meta.get(key, []):
                source = Path(item["file"])
                if not source.exists() or str(source) == "synthetic":
                    continue
                target = self._unique_target(target_dir / source.name)
                try:
                    shutil.move(str(source), str(target))
                    moved.append(str(target))
                except OSError as exc:
                    errors.append({"file": str(source), "error": str(exc)})
        return {"moved": moved, "errors": errors}

    @staticmethod
    def _unique_target(target: Path) -> Path:
        if not target.exists():
            return target
        return target.with_name(f"{target.stem}_{datetime.now().strftime('%H%M%S_%f')}{target.suffix}")

    def _check_baseline(self, repo: AssetRepository, source: str, current_count: int) -> dict[str, Any]:
        previous = repo.latest_snapshot(source)
        if not previous or int(previous["record_count"]) <= 0:
            return {"status": "NO_BASELINE", "previous_count": None, "current_count": current_count, "warning": False, "critical": False}
        previous_count = int(previous["record_count"])
        ratio = current_count / previous_count
        prefix = "rvtools" if source == "RVTOOLS" else "itsm"
        warning_ratio = float(self.config.quality.get(f"{prefix}_count_warning_ratio", 0.70))
        critical_ratio = float(self.config.quality.get(f"{prefix}_count_critical_ratio", 0.30))
        critical = ratio < critical_ratio
        warning = not critical and ratio < warning_ratio
        return {
            "status": "CRITICAL" if critical else "WARNING" if warning else "NORMAL",
            "previous_count": previous_count,
            "current_count": current_count,
            "ratio": ratio,
            "warning": warning,
            "critical": critical,
        }
