from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from ..normalization.code_maps import ASSET_STATUS, ENVIRONMENT, SERVER_CATEGORY
from ..repositories import AssetRepository
from .exception_service import ReconciliationExceptionService


ACTIVE_STATUS = {"CMSTA010", "CMSTA050"}
LOGICAL_CATEGORY = "CMSVRCATCD020"
PHYSICAL_CATEGORY = "CMSVRCATCD010"


class IntegratedDashboardService:
    """Read-only dashboard projection built from persisted automatic collection results."""

    def __init__(self, repository: AssetRepository) -> None:
        self.repo = repository

    def summary(self, start: str | None = None, end: str | None = None, detail_limit: int = 500) -> dict[str, Any]:
        start_day, end_day = self._period(start, end)
        latest_itsm = self.repo.latest_snapshot("ITSM")
        latest_rv = self.repo.latest_snapshot("RVTOOLS")
        current_assets = self._current_assets(int(latest_itsm["id"])) if latest_itsm else []
        itsm_status = self._asset_status(current_assets)
        if latest_itsm:
            itsm_status["counting_basis"] = self._counting_basis(int(latest_itsm["id"]))
        period_changes = self._period_changes(start_day, end_day, detail_limit)
        vcenter_changes = self._vcenter_resource_changes(start_day, end_day, detail_limit)
        comparison = self._comparison(start_day, end_day, latest_itsm, latest_rv, detail_limit)
        batch = self.repo.latest_daily_batch()
        return {
            "as_of": latest_itsm["collected_at"] if latest_itsm else None,
            "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
            "automation": self._automation(batch),
            "asset_status": itsm_status,
            "itsm_changes": period_changes,
            "vcenter_changes": vcenter_changes,
            "comparison": comparison,
        }

    def exception_candidates(self, limit: int = 5000) -> list[dict[str, Any]]:
        latest = self._latest_reconciliation_rows(limit=limit)
        active_exceptions = self.repo.active_reconciliation_exceptions()
        result: list[dict[str, Any]] = []
        for row in latest:
            excluded = next((exc for exc in active_exceptions if ReconciliationExceptionService.applies(row, exc)), None)
            row["exception_applied"] = bool(excluded)
            row["exception_id"] = excluded.get("id") if excluded else None
            if excluded:
                row["exception_reason"] = excluded.get("reason")
            result.append(row)
        return result

    def _current_assets(self, snapshot_id: int) -> list[dict[str, Any]]:
        # 집계는 정규화된 컬럼만 쓴다. 원본까지 풀 이유가 없다.
        records = self.repo.load_itsm_records(snapshot_id, with_raw=False).values()
        return [record for record in records if record.get("status_code") in ACTIVE_STATUS]

    def _counting_basis(self, snapshot_id: int) -> dict[str, Any]:
        """대수가 무엇을 세고 무엇을 뺐는지 밝힌다.

        ITSM 총 건수와 화면의 대수가 다를 때, 기준이 안 보이면 어느 쪽이 틀렸는지
        따질 수가 없다. 집계에서 빠진 상태코드를 건수까지 같이 돌려준다.
        """
        records = self.repo.load_itsm_records(snapshot_id, with_raw=False).values()
        included: Counter[str] = Counter()
        excluded: Counter[str] = Counter()
        for record in records:
            code = str(record.get("status_code") or "")
            target = included if code in ACTIVE_STATUS else excluded
            target[f"{ASSET_STATUS.get(code, '알 수 없음')}({code or '없음'})"] += 1
        return {
            "snapshot_total": len(records),
            "counted_status_codes": sorted(ACTIVE_STATUS),
            "included": dict(included),
            "excluded": dict(excluded),
            "category_codes": {"물리": PHYSICAL_CATEGORY, "논리": LOGICAL_CATEGORY},
        }

    def _asset_status(self, assets: list[dict[str, Any]]) -> dict[str, Any]:
        status = Counter()
        category = Counter()
        location = Counter()
        os_count = Counter()
        eos_count = Counter()
        eos_year = Counter()
        current_year = date.today().year
        for item in assets:
            status[ASSET_STATUS.get(str(item.get("status_code")), str(item.get("status_code") or "미정"))] += 1
            category[SERVER_CATEGORY.get(str(item.get("server_category_code")), str(item.get("server_category_code") or "미정"))] += 1
            location[self._location(item)] += 1
            os_count[str(item.get("os_family") or "미정")] += 1
            year = self._eos_year(item.get("eos_value"))
            if year == "미정":
                eos_count["미정"] += 1
            elif year == "확인필요":
                eos_count["확인필요"] += 1
            else:
                eos_year[str(year)] += 1
                if int(year) < current_year:
                    eos_count["종료"] += 1
                elif int(year) == current_year:
                    eos_count["올해 종료"] += 1
                else:
                    eos_count["예정"] += 1
        return {
            "total": len(assets),
            "status": dict(status),
            "category": dict(category),
            "location": dict(location),
            "os": self._sorted_counts(os_count),
            "eosl": dict(eos_count),
            "eosl_by_year": self._sorted_counts(eos_year, numeric=True),
        }

    def _period_changes(self, start_day: date, end_day: date, limit: int) -> dict[str, Any]:
        start_dt = datetime.combine(start_day, time.min).isoformat()
        end_dt = datetime.combine(end_day + timedelta(days=1), time.min).isoformat()
        events = self.repo.changes(source="ITSM", limit=100000, start=start_dt, end=end_dt)
        primary = [
            event for event in events
            if event["event_type"] in {
                "ITSM_ASSET_CREATED", "ITSM_RECORD_REMOVED", "ITSM_STATUS_TO_UNUSED",
                "ITSM_STATUS_TO_DISPOSED", "ITSM_ASSET_REACTIVATED",
            }
            or (event.get("field_name") and event["event_type"] != "ITSM_STATUS_CHANGED")
        ]
        totals = Counter(self._event_category(e["event_type"]) for e in primary)
        snapshot_cache: dict[int, dict[str, dict[str, Any]]] = {}
        details = [self._enrich_event(e, snapshot_cache) for e in primary[:limit]]
        matrix: dict[str, Counter[str]] = defaultdict(Counter)
        for item in details:
            matrix[item["location"]][item["server_type"]] += 1
        return {
            "counts": dict(totals),
            "total_events": len(primary),
            "location_type_counts": {location: dict(values) for location, values in matrix.items()},
            "details": details,
        }


    def _vcenter_resource_changes(self, start_day: date, end_day: date, limit: int) -> dict[str, Any]:
        start_dt = datetime.combine(start_day, time.min).isoformat()
        end_dt = datetime.combine(end_day + timedelta(days=1), time.min).isoformat()
        events = self.repo.changes(source="RVTOOLS", limit=100000, start=start_dt, end=end_dt)
        wanted = {"RV_NEW", "RV_REMOVED", "RV_CPU_CHANGED", "RV_MEMORY_CHANGED", "RV_HOST_CHANGED", "RV_VCENTER_CHANGED"}
        events = [event for event in events if event.get("event_type") in wanted]
        counts = Counter(event.get("event_type") for event in events)
        cache: dict[int, dict[str, dict[str, Any]]] = {}
        details: list[dict[str, Any]] = []
        for event in events[:limit]:
            current: dict[str, Any] = {}
            previous: dict[str, Any] = {}
            if event.get("snapshot_id"):
                sid = int(event["snapshot_id"])
                if sid not in cache:
                    cache[sid] = self.repo.load_rv_records(sid, with_raw=False)
                current = cache[sid].get(event["asset_key"], {})
            if event.get("previous_snapshot_id"):
                sid = int(event["previous_snapshot_id"])
                if sid not in cache:
                    cache[sid] = self.repo.load_rv_records(sid, with_raw=False)
                previous = cache[sid].get(event["asset_key"], {})
            vm = current or previous
            details.append({
                "detected_at": event.get("detected_at"),
                "event_type": event.get("event_type"),
                "asset_key": event.get("asset_key"),
                "vm_name": vm.get("vm_name") or event.get("asset_key"),
                "vcenter_id": vm.get("vcenter"),
                "cluster_name": vm.get("cluster_name"),
                "esxi_host": vm.get("esxi_host"),
                "field_name": event.get("field_name"),
                "old_value": event.get("old_value"),
                "new_value": event.get("new_value"),
                "current_cpu_cores": current.get("cpus") if current else None,
                "current_memory_mb": current.get("memory_mb") if current else None,
                "previous_cpu_cores": previous.get("cpus") if previous else None,
                "previous_memory_mb": previous.get("memory_mb") if previous else None,
            })
        return {
            "counts": {
                "신규": counts.get("RV_NEW", 0),
                "삭제": counts.get("RV_REMOVED", 0),
                "CPU변경": counts.get("RV_CPU_CHANGED", 0),
                "MEM변경": counts.get("RV_MEMORY_CHANGED", 0),
                "통합기이동": counts.get("RV_HOST_CHANGED", 0),
                "vCenter이동": counts.get("RV_VCENTER_CHANGED", 0),
            },
            "total_events": len(events),
            "details": details,
        }

    def _comparison(
        self,
        start_day: date,
        end_day: date,
        latest_itsm: Any,
        latest_rv: Any,
        limit: int,
    ) -> dict[str, Any]:
        rows = self._latest_reconciliation_rows(limit=100000)
        active_exceptions = self.repo.active_reconciliation_exceptions(end_day.isoformat())
        visible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in rows:
            exception = next((exc for exc in active_exceptions if ReconciliationExceptionService.applies(row, exc)), None)
            if exception:
                row["exception_id"] = exception["id"]
                row["exception_reason"] = exception["reason"]
                excluded.append(row)
            else:
                visible.append(row)
        counts = Counter(row["match_status"] for row in visible)
        raw_counts = Counter(row["match_status"] for row in rows)
        normal = counts.get("MATCHED", 0)
        denominator = sum(counts.get(k, 0) for k in {"MATCHED", "MATCHED_WITH_DRIFT", "ITSM_ONLY", "RVTOOLS_ONLY", "IP_CHANGE_CANDIDATE", "HOSTNAME_REVIEW", "AMBIGUOUS"})
        period = self._logical_period_metrics(start_day, end_day)
        return {
            "snapshot": {
                "itsm_id": int(latest_itsm["id"]) if latest_itsm else None,
                "vcenter_id": int(latest_rv["id"]) if latest_rv else None,
                "itsm_count": period["end"]["itsm_logical"],
                "vcenter_count": period["end"]["vcenter_vm"],
            },
            "counts": dict(counts),
            "raw_counts": dict(raw_counts),
            "exception_count": len(excluded),
            "match_rate": round(normal / denominator * 100, 2) if denominator else None,
            "period_metrics": period,
            "details": [row for row in visible if row["match_status"] != "MATCHED"][:limit],
            "exceptions": excluded[:limit],
        }

    def _logical_period_metrics(self, start_day: date, end_day: date) -> dict[str, Any]:
        start_itsm = self._snapshot_on_or_before("ITSM", start_day)
        end_itsm = self._snapshot_on_or_before("ITSM", end_day, end_of_day=True)
        start_rv = self._snapshot_on_or_before("RVTOOLS", start_day)
        end_rv = self._snapshot_on_or_before("RVTOOLS", end_day, end_of_day=True)
        start_counts = {
            "itsm_logical": self._logical_count(start_itsm),
            "vcenter_vm": self._vm_count(start_rv),
        }
        end_counts = {
            "itsm_logical": self._logical_count(end_itsm),
            "vcenter_vm": self._vm_count(end_rv),
        }
        start_dt = datetime.combine(start_day, time.min).isoformat()
        end_dt = datetime.combine(end_day + timedelta(days=1), time.min).isoformat()
        itsm_events = self._logical_itsm_events(self.repo.changes("ITSM", 100000, start_dt, end_dt))
        rv_events = self._vm_events(self.repo.changes("RVTOOLS", 100000, start_dt, end_dt))
        return {
            "start": start_counts,
            "end": end_counts,
            "itsm": self._source_event_counts(itsm_events),
            "vcenter": self._source_event_counts(rv_events),
        }

    def _logical_itsm_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cache: dict[int, dict[str, dict[str, Any]]] = {}
        result: list[dict[str, Any]] = []
        for event in events:
            ids = [event.get("snapshot_id"), event.get("previous_snapshot_id")]
            records: list[dict[str, Any]] = []
            for snapshot_id in ids:
                if not snapshot_id:
                    continue
                sid = int(snapshot_id)
                if sid not in cache:
                    cache[sid] = self.repo.load_itsm_records(sid, with_raw=False)
                record = cache[sid].get(event["asset_key"])
                if record:
                    records.append(record)
            if any(record.get("server_category_code") == LOGICAL_CATEGORY for record in records):
                result.append(event)
        return result

    def _vm_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cache: dict[int, dict[str, dict[str, Any]]] = {}
        result: list[dict[str, Any]] = []
        for event in events:
            ids = [event.get("snapshot_id"), event.get("previous_snapshot_id")]
            records: list[dict[str, Any]] = []
            for snapshot_id in ids:
                if not snapshot_id:
                    continue
                sid = int(snapshot_id)
                if sid not in cache:
                    cache[sid] = self.repo.load_rv_records(sid, with_raw=False)
                record = cache[sid].get(event["asset_key"])
                if record:
                    records.append(record)
            if not records or any(not record.get("template_flag") and not record.get("srm_placeholder") for record in records):
                result.append(event)
        return result

    def _latest_reconciliation_rows(self, limit: int) -> list[dict[str, Any]]:
        marker = self.repo.conn.execute("SELECT MAX(created_at) AS created_at FROM reconciliation_result").fetchone()
        if not marker or not marker["created_at"]:
            return []
        rows = self.repo.conn.execute(
            "SELECT * FROM reconciliation_result WHERE created_at=? ORDER BY id LIMIT ?",
            (marker["created_at"], limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        itsm_cache: dict[int, dict[str, dict[str, Any]]] = {}
        rv_cache: dict[int, dict[str, dict[str, Any]]] = {}
        for source_row in rows:
            row = dict(source_row)
            row["drifts"] = json.loads(row.pop("drift_json") or "[]")
            itsm_id = int(row["itsm_snapshot_id"])
            rv_id = int(row["rv_snapshot_id"])
            if itsm_id not in itsm_cache:
                itsm_cache[itsm_id] = self.repo.load_itsm_records(itsm_id)
            if rv_id not in rv_cache:
                rv_cache[rv_id] = self.repo.load_rv_records(rv_id, with_raw=False)
            asset = itsm_cache[itsm_id].get(row.get("cm_id")) if row.get("cm_id") else None
            vm = rv_cache[rv_id].get(row.get("rv_asset_key")) if row.get("rv_asset_key") else None
            row.update(
                {
                    "server_name": self._server_name(asset, vm),
                    "hostname": (asset or {}).get("normalized_hostname") or (vm or {}).get("normalized_hostname"),
                    "primary_ip": (asset or {}).get("primary_ip") or (vm or {}).get("primary_ip"),
                    "itsm_cpu": (asset or {}).get("cpu_cores"),
                    "vcenter_cpu": (vm or {}).get("cpus"),
                    "itsm_memory_mb": (asset or {}).get("memory_mb"),
                    "vcenter_memory_mb": (vm or {}).get("memory_mb"),
                }
            )
            result.append(row)
        return result

    def _enrich_event(
        self, event: dict[str, Any], cache: dict[int, dict[str, dict[str, Any]]] | None = None
    ) -> dict[str, Any]:
        # 스냅샷 하나가 수천~수만 행이다. 이벤트마다 다시 읽으면 건수만큼 곱해진다.
        cache = {} if cache is None else cache

        def snapshot(snapshot_id: Any) -> dict[str, dict[str, Any]]:
            if not snapshot_id:
                return {}
            key = int(snapshot_id)
            if key not in cache:
                cache[key] = self.repo.load_itsm_records(key)
            return cache[key]

        current = snapshot(event.get("snapshot_id")).get(event["asset_key"], {})
        previous = snapshot(event.get("previous_snapshot_id")).get(event["asset_key"], {})
        asset = current or previous
        raw = asset.get("raw", {})
        return {
            "detected_at": event["detected_at"],
            "asset_key": event["asset_key"],
            "category": self._event_category(event["event_type"]),
            "event_type": event["event_type"],
            "field_name": event.get("field_name"),
            "old_value": event.get("old_value"),
            "new_value": event.get("new_value"),
            "server_name": str(raw.get("CM_NAME") or raw.get("CM_HOSTNAME") or event["asset_key"]),
            "hostname": asset.get("normalized_hostname"),
            "primary_ip": asset.get("primary_ip"),
            "status": ASSET_STATUS.get(str(asset.get("status_code")), str(asset.get("status_code") or "미정")),
            "server_type": SERVER_CATEGORY.get(str(asset.get("server_category_code")), str(asset.get("server_category_code") or "미정")),
            "location": self._location(asset),
            "os": asset.get("os_family"),
            "os_version": asset.get("os_version"),
            "cpu_cores": asset.get("cpu_cores"),
            "memory_mb": asset.get("memory_mb"),
            "department": raw.get("CM_OWN_DPT_ID") or raw.get("CM_USER_DPT_ID"),
        }

    def _snapshot_on_or_before(self, source: str, day: date, end_of_day: bool = False) -> Any:
        marker = datetime.combine(day, time.max if end_of_day else time.min).isoformat()
        return self.repo.conn.execute(
            """
            SELECT * FROM snapshot
             WHERE source=? AND status IN ('SUCCESS','PARTIAL_SUCCESS') AND collected_at<=?
             ORDER BY collected_at DESC, id DESC LIMIT 1
            """,
            (source, marker),
        ).fetchone()

    def _logical_count(self, snapshot: Any) -> int:
        if not snapshot:
            return 0
        return int(
            self.repo.conn.execute(
                "SELECT COUNT(*) AS cnt FROM itsm_asset_snapshot WHERE snapshot_id=? AND status_code IN ('CMSTA010','CMSTA050') AND server_category_code=?",
                (snapshot["id"], LOGICAL_CATEGORY),
            ).fetchone()["cnt"]
        )

    def _vm_count(self, snapshot: Any) -> int:
        if not snapshot:
            return 0
        return int(
            self.repo.conn.execute(
                "SELECT COUNT(*) AS cnt FROM rv_asset_snapshot WHERE snapshot_id=? AND power_state='poweredon' AND template_flag=0 AND srm_placeholder=0",
                (snapshot["id"],),
            ).fetchone()["cnt"]
        )

    @staticmethod
    def _source_event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
        counts = Counter()
        for event in events:
            category = IntegratedDashboardService._event_category(event["event_type"])
            if category in {"신규", "삭제", "변경", "상태변경", "수집공백"}:
                counts[category] += 1
        return dict(counts)

    @staticmethod
    def _event_category(event_type: str) -> str:
        if event_type in {"ITSM_ASSET_CREATED", "RV_NEW"}:
            return "신규"
        if event_type in {"ITSM_RECORD_REMOVED", "RV_REMOVED"}:
            return "삭제"
        if event_type == "COLLECTION_GAP":
            return "수집공백"
        if "STATUS" in event_type or event_type in {"RV_POWER_ON", "RV_POWER_OFF", "ITSM_ASSET_REACTIVATED"}:
            return "상태변경"
        return "변경"

    @staticmethod
    def _location(item: dict[str, Any]) -> str:
        raw = item.get("raw", {}) or {}
        dr_yn = str(raw.get("CM_DR_YN") or "").strip().upper()
        environment = str(item.get("environment_code") or raw.get("CM_OWN_CAT_CD") or "").strip().upper()
        place = str(raw.get("CM_PLACE") or "").strip().upper()
        if dr_yn in {"Y", "YES", "1"} or environment == "CMOWNCATCD0040" or "DR" in place:
            return "DR"
        return "IDC"

    @staticmethod
    def _eos_year(value: Any) -> int | str:
        text = str(value or "").strip()
        if not text:
            return "확인필요"
        match = re.search(r"(\d{4})", text)
        if not match:
            return "확인필요"
        year = int(match.group(1))
        return "미정" if year == 9999 else year

    @staticmethod
    def _sorted_counts(counter: Counter[str], numeric: bool = False) -> list[dict[str, Any]]:
        key = (lambda item: int(item[0])) if numeric else (lambda item: (-item[1], item[0]))
        return [{"name": name, "count": count} for name, count in sorted(counter.items(), key=key)]

    @staticmethod
    def _server_name(asset: dict[str, Any] | None, vm: dict[str, Any] | None) -> str | None:
        raw = (asset or {}).get("raw", {}) or {}
        return str(raw.get("CM_NAME") or raw.get("CM_HOSTNAME") or (vm or {}).get("vm_name") or "").strip() or None

    @staticmethod
    def _period(start: str | None, end: str | None) -> tuple[date, date]:
        today = date.today()
        try:
            start_day = date.fromisoformat((start or today.isoformat())[:10])
            end_day = date.fromisoformat((end or today.isoformat())[:10])
        except ValueError as exc:
            raise ValueError("조회기간은 YYYY-MM-DD 형식이어야 합니다.") from exc
        if start_day > end_day:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        if (end_day - start_day).days > 3660:
            raise ValueError("한 번에 조회할 수 있는 기간은 최대 10년입니다.")
        return start_day, end_day

    @staticmethod
    def _automation(batch: dict[str, Any] | None) -> dict[str, Any]:
        if not batch:
            return {"status": "NO_RUN", "message": "자동 실행 이력이 없습니다."}
        result = dict(batch)
        result["errors"] = json.loads(result.pop("error_json") or "{}")
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result
