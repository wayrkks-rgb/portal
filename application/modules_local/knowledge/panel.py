"""통합 대시보드에 넣는 지식 저장소 요약 패널."""

from __future__ import annotations

from typing import Any, Mapping

from application.db import database_manager

from .store import KnowledgeStore


def panel(user: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    with database_manager().connect() as conn:
        # 요약은 파일을 건드리지 않으므로 보관 위치는 의미가 없다.
        summary = KnowledgeStore(conn, ".").summary()
    return {
        "title": "지식 저장소",
        "metrics": [
            {"label": "자료", "value": summary["datasets"], "unit": "건", "state": "info"},
            {"label": "찾을 수 있는 행", "value": summary["rows"], "unit": "행", "state": "info"},
            {"label": "이번 달 올림", "value": summary["uploads_this_month"], "unit": "건", "state": "success"},
        ],
        "table": {
            "columns": ["자료", "분류", "올린 날"],
            "rows": [
                [item.get("title"), item.get("category") or "-", str(item.get("updated_at") or "")[:10]]
                for item in summary["recent"]
            ],
        },
        "note": "최근에 올리거나 고친 자료",
        "updated_at": (summary["recent"][0].get("updated_at") if summary["recent"] else None),
    }
