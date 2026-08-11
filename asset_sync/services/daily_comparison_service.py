from __future__ import annotations

from collections import Counter
from typing import Any

from ..config import AppConfig
from ..repositories import AssetRepository
from .change_presenter import present
from .diff_service import DiffService


class DailyComparisonService:
    """Compare the latest snapshot with the latest usable snapshot from an earlier date.

    When no earlier calendar date exists, a same-day previous run is used only as a
    fallback so initial validation can still be performed before the first overnight run.
    """

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def latest(self, source: str, limit: int = 2000) -> dict[str, Any]:
        source = source.upper()
        if source not in {"ITSM", "RVTOOLS"}:
            raise ValueError("source는 ITSM 또는 RVTOOLS여야 합니다.")
        current = self.repo.latest_snapshot(source)
        if current is None:
            return {
                "status": "NO_SNAPSHOT",
                "source": source,
                "comparison_basis": None,
                "current": None,
                "previous": None,
                "counts": {},
                "events": [],
            }

        previous = self.repo.previous_day_snapshot(source, str(current["snapshot_date"]))
        comparison_basis = "PREVIOUS_DAY"
        if previous is None:
            previous = self.repo.latest_snapshot(source, before_snapshot_id=int(current["id"]))
            comparison_basis = "PREVIOUS_RUN_SAME_DAY"
        if previous is None:
            return {
                "status": "NO_BASELINE",
                "source": source,
                "comparison_basis": None,
                "current": dict(current),
                "previous": None,
                "counts": {"ADDED": 0, "REMOVED": 0, "CHANGED": 0, "STATUS_CHANGED": 0, "COLLECTION_GAP": 0},
                "events": [],
            }

        pair = DiffService(self.config, self.repo).compare_pair(source, int(current["id"]), int(previous["id"]))
        events: list[dict[str, Any]] = []
        type_counts: Counter[str] = Counter()
        categories: dict[str, set[str]] = {
            "ADDED": set(),
            "REMOVED": set(),
            "CHANGED": set(),
            "STATUS_CHANGED": set(),
            "COLLECTION_GAP": set(),
        }
        for raw in pair["events"][:limit]:
            # 화면용 표현(라벨·요약)을 여기서 붙인다. 코드값과 원본 JSON 을 그대로
            # 내보내면 표가 읽을 수 없게 된다.
            item = present(raw)
            category = self._category(source, str(item["event_type"]))
            item["category"] = category
            events.append(item)
            type_counts[str(item["event_type"])] += 1
            if category:
                categories[category].add(str(item["asset_key"]))

        current_count = int(current["record_count"])
        previous_count = int(previous["record_count"])
        return {
            "status": "SUCCESS",
            "source": source,
            "comparison_basis": comparison_basis,
            "current": dict(current),
            "previous": dict(previous),
            "record_counts": {
                "previous": previous_count,
                "current": current_count,
                "net": current_count - previous_count,
            },
            "counts": {key: len(value) for key, value in categories.items()},
            "event_type_counts": dict(type_counts),
            "events": events,
        }

    @staticmethod
    def _category(source: str, event_type: str) -> str | None:
        if source == "ITSM":
            if event_type == "ITSM_ASSET_CREATED":
                return "ADDED"
            if event_type == "ITSM_RECORD_REMOVED":
                return "REMOVED"
            if event_type.startswith("ITSM_STATUS_") or event_type == "ITSM_ASSET_REACTIVATED":
                return "STATUS_CHANGED"
            if event_type == "ITSM_ASSET_UPDATED":
                return "CHANGED"
            return None
        if event_type == "RV_NEW":
            return "ADDED"
        if event_type == "RV_REMOVED":
            return "REMOVED"
        if event_type == "COLLECTION_GAP":
            return "COLLECTION_GAP"
        if event_type.startswith("RV_"):
            return "CHANGED"
        return None
