from __future__ import annotations

from collections import Counter
from typing import Any

from ..config import AppConfig
from ..repositories import AssetRepository
from .asset_scope import AssetScope
from .change_presenter import attach_identity, present
from .diff_service import DiffService


class DailyComparisonService:
    """Compare the latest snapshot with the latest usable snapshot from an earlier date.

    When no earlier calendar date exists, a same-day previous run is used only as a
    fallback so initial validation can still be performed before the first overnight run.
    """

    def __init__(
        self,
        config: AppConfig,
        repository: AssetRepository,
        scope: AssetScope | None = None,
    ) -> None:
        self.config = config
        self.repo = repository
        # 자산 대수와 같은 기준을 쓴다. 대시보드에서 뺀 자산이 일간 점검 변경
        # 내역에는 남아 있으면 두 화면이 서로 다른 말을 하게 된다.
        self.scope = scope or AssetScope.load(config, repository)

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

        raw_events, source_of_events = self._events_for(
            source, int(current["id"]), int(previous["id"]), limit
        )
        # 자산코드만 있으면 어느 서버인지 알 수 없다. 호스트명·IP·업무명을 붙이고,
        # 자산에서 뺀 대상의 변경은 여기서도 뺀다.
        raw_events = attach_identity(raw_events, self.repo, scope=self.scope)
        events: list[dict[str, Any]] = []
        type_counts: Counter[str] = Counter()
        categories: dict[str, set[str]] = {
            "ADDED": set(),
            "REMOVED": set(),
            "CHANGED": set(),
            "STATUS_CHANGED": set(),
            "COLLECTION_GAP": set(),
        }
        for raw in raw_events:
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
            "events_from": source_of_events,
            "events": events,
        }

    def _events_for(
        self, source: str, snapshot_id: int, previous_snapshot_id: int, limit: int
    ) -> tuple[list[dict[str, Any]], str]:
        """변경 이벤트를 가져온다. 저장된 것이 있으면 다시 계산하지 않는다.

        수집할 때 같은 짝을 이미 비교해 저장해 둔다. 그런데도 화면을 열 때마다
        스냅샷 두 개를 전부 읽어 파이썬으로 재비교하면, 자산이 늘수록 그 시간이
        그대로 대기시간이 된다. 저장된 것이 없을 때만(예: 전일과 비교하는 첫 화면,
        수집 당시 이벤트 생성을 보류한 경우) 계산한다.
        """
        if self.repo.count_change_events_for_pair(snapshot_id, previous_snapshot_id):
            return self.repo.change_events_for_pair(snapshot_id, previous_snapshot_id, limit), "STORED"
        pair = DiffService(self.config, self.repo).compare_pair(source, snapshot_id, previous_snapshot_id)
        return pair["events"][:limit], "RECOMPUTED"

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
