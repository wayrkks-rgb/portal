"""통합 웹 안에 있는 모듈의 대시보드 패널.

원격 WAS 는 ``panel_path`` 로 패널 스펙을 돌려주지만, 통합 웹 내부 기능은 HTTP 를
거칠 이유가 없다. 같은 스펙을 파이썬 함수로 돌려주면 통합 대시보드는 로컬/원격을
구분하지 않고 같은 방식으로 그린다.

이 파일은 모듈이 지켜야 할 패널 스펙의 참조 구현이기도 하다. 각 담당자가 만드는
WAS 는 이 형태의 JSON 을 돌려주면 된다.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from application.db import database_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.dashboard_service import DashboardService

LOGGER = logging.getLogger(__name__)


def asset_sync_panel(user: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    """자산 정합성 요약 패널."""
    with database_manager().connect() as conn:
        summary = DashboardService(AssetRepository(conn)).summary()

    itsm = summary.get("latest_itsm") or {}
    vcenter = summary.get("latest_rvtools") or {}
    counts = summary.get("reconciliation_counts") or {}
    matched = int(counts.get("MATCHED", 0))
    review = sum(int(value) for key, value in counts.items() if key != "MATCHED")
    total = matched + review
    match_rate = round(matched * 100 / total, 1) if total else None

    return {
        "title": "자산 정합성 요약",
        "metrics": [
            {"label": "ITSM 자산", "value": itsm.get("record_count", 0), "unit": "건", "state": "info"},
            {"label": "vCenter VM", "value": vcenter.get("record_count", 0), "unit": "대", "state": "info"},
            {"label": "일치", "value": matched, "unit": "건", "state": "success"},
            {
                "label": "검토 대상",
                "value": review,
                "unit": "건",
                "state": "warning" if review else "success",
            },
            {"label": "일치율", "value": match_rate, "unit": "%", "state": "info"},
        ],
        "table": {
            "columns": ["출처", "이벤트", "건수"],
            "rows": [
                [item.get("source"), item.get("event_type"), item.get("count")]
                for item in summary.get("daily_change_counts") or []
            ],
        },
        "note": "최근 24시간 변경 기준",
        "updated_at": itsm.get("collected_at") or vcenter.get("collected_at"),
    }


def register_all() -> None:
    from application.modules.panels import register_local_panel

    register_local_panel("asset_sync", asset_sync_panel)
