from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from ..repositories import AssetRepository


class DashboardService:
    def __init__(self, repository: AssetRepository) -> None:
        self.repo = repository

    def summary(self) -> dict[str, Any]:
        itsm = self.repo.latest_snapshot("ITSM")
        rv = self.repo.latest_snapshot("RVTOOLS")
        latest_recon = self.repo.conn.execute("SELECT MAX(created_at) AS created_at FROM reconciliation_result").fetchone()
        recon_counts: dict[str, int] = {}
        if latest_recon and latest_recon["created_at"]:
            rows = self.repo.conn.execute(
                "SELECT match_status, COUNT(*) AS cnt FROM reconciliation_result WHERE created_at=? GROUP BY match_status",
                (latest_recon["created_at"],),
            ).fetchall()
            recon_counts = {row["match_status"]: row["cnt"] for row in rows}
        # 경계값을 파이썬에서 만든다. datetime('now','-1 day') 는 SQLite 전용 문법이고,
        # detected_at 은 두 엔진 모두 ISO 문자열로 저장되므로 문자열 비교로 충분하다.
        since = (datetime.now() - timedelta(days=1)).isoformat()
        changes = self.repo.conn.execute(
            "SELECT source, event_type, COUNT(*) AS cnt FROM change_event WHERE detected_at>=? GROUP BY source, event_type",
            (since,),
        ).fetchall()
        return {
            "latest_itsm": dict(itsm) if itsm else None,
            "latest_rvtools": dict(rv) if rv else None,
            "reconciliation_counts": recon_counts,
            "daily_change_counts": [{"source": r["source"], "event_type": r["event_type"], "count": r["cnt"]} for r in changes],
            "collection_runs": self.repo.collection_runs(10),
        }
