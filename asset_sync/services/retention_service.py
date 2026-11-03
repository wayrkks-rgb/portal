"""오래된 수시 수집 스냅샷 정리. 변경 이력은 지우지 않는다.

수시 수집(30분 · 1시간)은 바뀐 것이 있을 때만 스냅샷을 남기지만, 그래도 하루에 여러 벌이
쌓일 수 있다. 며칠 지나면 하루 중간 상태는 다시 볼 일이 거의 없고, 남는 것은 '그날 마지막
상태' 와 '무엇이 언제 바뀌었나(변경 이벤트)' 다. 그래서

- ``retention.intraday_keep_days`` (기본 14일) 보다 오래된 날은
- 그날의 **마지막 스냅샷만 남기고**, 나머지 수시 스냅샷을 지운다.
- 지우는 스냅샷을 가리키던 변경 이벤트 · 배치 기록은 남는 스냅샷으로 옮긴다(이벤트 자체와
  감지 시각 ``detected_at`` 은 그대로라 일 · 주 · 월 보고가 같은 숫자를 낸다).

07:00 일일 배치 스냅샷은 지우지 않는다(월간 기준 · 감사의 바탕이다).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

DEFAULT_KEEP_DAYS = 14


class RetentionService:
    def __init__(self, config: Any, repository: Any) -> None:
        self.config = config
        self.repo = repository

    @property
    def keep_days(self) -> int:
        value = (getattr(self.config, "retention", None) or {}).get("intraday_keep_days", DEFAULT_KEEP_DAYS)
        return max(1, int(value or DEFAULT_KEEP_DAYS))

    def thin_intraday(self, *, today: date | None = None, dry_run: bool = False) -> dict[str, Any]:
        cutoff = ((today or date.today()) - timedelta(days=self.keep_days)).isoformat()
        conn = self.repo.conn
        report: dict[str, Any] = {"cutoff": cutoff, "dry_run": dry_run, "deleted": {}, "kept_days": {}}
        for source in ("ITSM", "RVTOOLS"):
            rows = [dict(r) for r in conn.execute(
                "SELECT s.id, s.snapshot_date, s.collected_at, r.metadata_json FROM snapshot s"
                " LEFT JOIN collection_run r ON r.id = s.collection_run_id"
                " WHERE s.source = ? ORDER BY s.collected_at, s.id",
                (source,),
            ).fetchall()]
            for row in rows:
                try:
                    row["intraday"] = (json.loads(row.get("metadata_json") or "{}") or {}).get("trigger") == "INTRADAY"
                except (TypeError, ValueError):
                    row["intraday"] = False
            last_of_day: dict[str, int] = {}
            for row in rows:
                last_of_day[str(row["snapshot_date"])] = int(row["id"])   # 시간순이라 마지막이 남는다
            doomed = [
                row for row in rows
                if row["intraday"] and str(row["snapshot_date"]) < cutoff
                and int(row["id"]) != last_of_day[str(row["snapshot_date"])]
            ]
            doomed_ids = {int(r["id"]) for r in doomed}
            kept = [r for r in rows if int(r["id"]) not in doomed_ids]
            report["deleted"][source] = len(doomed)
            report["kept_days"][source] = len({str(r["snapshot_date"]) for r in kept})
            if dry_run or not doomed:
                continue
            for row in doomed:
                sid = int(row["id"])
                later = next((int(k["id"]) for k in kept if (k["collected_at"], int(k["id"])) > (row["collected_at"], sid)), None)
                earlier = next((int(k["id"]) for k in reversed(kept)
                                if (k["collected_at"], int(k["id"])) < (row["collected_at"], sid)), None)
                if later is None:      # 그날 마지막은 남기므로 생기지 않는다. 그래도 지우지 않는다.
                    continue
                conn.execute("UPDATE change_event SET snapshot_id = ? WHERE snapshot_id = ?", (later, sid))
                conn.execute("UPDATE change_event SET previous_snapshot_id = ? WHERE previous_snapshot_id = ?",
                             (earlier, sid))
                for column in ("itsm_snapshot_id", "vcenter_snapshot_id"):
                    conn.execute(f"UPDATE daily_batch_run SET {column} = ? WHERE {column} = ?", (later, sid))
                conn.execute("DELETE FROM snapshot WHERE id = ?", (sid,))
        return report
