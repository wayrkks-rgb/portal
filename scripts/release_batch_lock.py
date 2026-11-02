"""남아 있는 일일 배치 잠금을 보여주고, 원하면 푼다.

배치가 중간에 죽으면 잠금(process_lock)이 남는다. 잠금은 4시간 뒤 저절로
풀리지만 그 사이 다시 돌리면 SKIPPED 로 빠진다. 돌고 있는 배치가 정말 없을
때만 푼다 -- 작업 관리자에 python.exe 가 없는지 먼저 본다.

쓰는 법::

    .venv\\Scripts\\python.exe scripts\\release_batch_lock.py           잠금 상태만
    .venv\\Scripts\\python.exe scripts\\release_batch_lock.py --release 잠금 풀기
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOCK_NAME = "daily_batch"


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 배치 잠금 확인·해제")
    parser.add_argument("--release", action="store_true", help="잠금을 푼다")
    args = parser.parse_args()

    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager

    manager = create_manager(load_config())
    manager.initialize()
    with manager.connect() as conn:
        row = conn.execute(
            "SELECT owner, acquired_at, expires_at FROM process_lock WHERE lock_name=?",
            (LOCK_NAME,),
        ).fetchone()
        if row is None:
            print("잠금 없음 — 배치를 바로 돌릴 수 있습니다.")
            return 0
        expires = str(row["expires_at"] or "")
        alive = expires > datetime.now().isoformat()
        print(f"잠금 보유자 {row['owner']} · 잡은 시각 {row['acquired_at']}"
              f" · 만료 {expires} · {'유효' if alive else '만료됨'}")
        if not args.release:
            print("풀려면 --release 를 붙여 다시 실행하세요(돌고 있는 배치가 없을 때만).")
            return 0
        conn.execute("DELETE FROM process_lock WHERE lock_name=?", (LOCK_NAME,))
    print("잠금을 풀었습니다. scripts\\run_daily_batch.bat 으로 다시 돌릴 수 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
