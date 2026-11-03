"""DB 가 얼마나 크고, 수시 수집을 켜면 1년에 얼마나 커질지 실제 데이터로 잰다.

    .venv\\Scripts\\python.exe scripts\\check_db_growth.py

아무것도 바꾸지 않는다. 마지막 스냅샷의 실제 크기와 최근 7일 동안 하루에 스냅샷이 몇 벌
남았는지를 보고 계산한다.

보관 규칙(retention)
  - 07:00 일일 스냅샷: 지우지 않는다(월간 기준 · 감사의 바탕).
  - 수시 수집 스냅샷: intraday_keep_days(기본 14일)가 지난 날은 그날 마지막 1벌만 남긴다.
  - 변경 이벤트(무엇이 언제 바뀌었나): 지우지 않는다. 수시 스냅샷을 정리해도 그대로다.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: raw_json 외에 다른 칸 · 인덱스 · IP 표가 차지하는 몫(대략). 실제 파일 크기로 다시 맞춘다.
OVERHEAD = 1.6


def mb(value: float) -> str:
    return f"{value / 1024 / 1024:,.1f} MB" if value < 1024 ** 3 else f"{value / 1024 ** 3:,.2f} GB"


def main() -> int:
    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager
    from asset_sync.services.retention_service import DEFAULT_KEEP_DAYS

    config = load_config()
    manager = create_manager(config)
    keep_days = int((config.retention or {}).get("intraday_keep_days", DEFAULT_KEEP_DAYS))
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    with manager.connect() as conn:
        print("== 지금 크기")
        if manager.engine == "sqlite":
            path = Path(config.database_path)
            print(f"   DB 파일 {path} : {mb(path.stat().st_size) if path.exists() else '없음'}")
        else:
            row = conn.execute(
                "SELECT SUM(data_length + index_length) AS n FROM information_schema.tables WHERE table_schema = DATABASE()"
            ).fetchone()
            print(f"   MySQL 스키마 : {mb(float(row['n'] or 0))}")
        events = conn.execute("SELECT COUNT(*) AS n FROM change_event").fetchone()["n"]
        print(f"   변경 이벤트 {int(events):,}건 (지우지 않음)\n")

        yearly = {"daily_only": 0.0, "with_intraday": 0.0}
        for source, table, label in (("ITSM", "itsm_asset_snapshot", "ITSM"), ("RVTOOLS", "rv_asset_snapshot", "vCenter")):
            latest = conn.execute(
                "SELECT id, record_count FROM snapshot WHERE source=? ORDER BY collected_at DESC, id DESC LIMIT 1",
                (source,),
            ).fetchone()
            if latest is None:
                print(f"== {label}: 스냅샷 없음\n")
                continue
            raw = conn.execute(f"SELECT SUM(LENGTH(raw_json)) AS n FROM {table} WHERE snapshot_id=?",
                               (int(latest["id"]),)).fetchone()["n"] or 0
            per_snapshot = float(raw) * OVERHEAD
            recent = conn.execute(
                "SELECT s.snapshot_date, r.metadata_json FROM snapshot s LEFT JOIN collection_run r"
                " ON r.id = s.collection_run_id WHERE s.source=? AND s.snapshot_date >= ?",
                (source, week_ago),
            ).fetchall()
            days = {str(r["snapshot_date"]) for r in recent} or {"-"}
            intraday = 0
            for r in recent:
                try:
                    intraday += (json.loads(r["metadata_json"] or "{}") or {}).get("trigger") == "INTRADAY"
                except (TypeError, ValueError):
                    pass
            per_day_intraday = intraday / max(1, len(days))
            daily_only = per_snapshot * 365
            # 14일 안은 수시 스냅샷을 다 갖고, 그 뒤로는 하루 1벌(그날 마지막)만 더 갖는다.
            with_intraday = daily_only + per_snapshot * 365 * (1 if per_day_intraday else 0) \
                + per_snapshot * per_day_intraday * keep_days
            yearly["daily_only"] += daily_only
            yearly["with_intraday"] += with_intraday
            print(f"== {label}")
            print(f"   스냅샷 1벌 ≈ {mb(per_snapshot)} ({int(latest['record_count']):,}건)")
            print(f"   최근 7일 수시 스냅샷 하루 평균 {per_day_intraday:.1f}벌 (바뀐 회차만 저장)")
            print(f"   1년 증가 예상: 일일만 {mb(daily_only)} / 수시 포함 {mb(with_intraday)}\n")

        print("== 합계(1년)")
        print(f"   일일 배치만 : {mb(yearly['daily_only'])}")
        print(f"   수시 수집 포함 : {mb(yearly['with_intraday'])} "
              f"(수시 스냅샷은 {keep_days}일 뒤 그날 마지막 1벌만 남김)")
        print("   변경 이력 · 일 · 주 · 월 보고는 정리와 무관하게 몇 달 전 것도 그대로 볼 수 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
