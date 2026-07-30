"""Cross-host mutual exclusion for the daily batch.

The batch used to be guarded by a lock file, which only prevents a second run on
the same machine. Once several WAS instances share one database, two hosts could
run the 07:00 collection at the same time and produce duplicate snapshots, so the
lock has to live in the shared database.

The implementation is deliberately plain SQL that works on both engines: taking
the lock is an INSERT that fails on the primary key when somebody already holds
it. A stale lock left behind by a crashed process is reclaimed after its lease
expires.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 4 * 60 * 60


class LockNotAcquired(RuntimeError):
    pass


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class DatabaseLock:
    """Lease-based named lock stored in the shared ``process_lock`` table."""

    def __init__(
        self,
        manager: Any,
        lock_name: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        owner: str | None = None,
    ) -> None:
        self.manager = manager
        self.lock_name = lock_name
        self.lease_seconds = int(lease_seconds)
        self.owner = owner or _owner_id()
        self.acquired = False

    def __enter__(self) -> "DatabaseLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def acquire(self) -> None:
        now = datetime.now()
        expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.manager.connect() as conn:
            current = conn.execute(
                "SELECT owner, acquired_at, expires_at FROM process_lock WHERE lock_name=?",
                (self.lock_name,),
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO process_lock(lock_name, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                    (self.lock_name, self.owner, now.isoformat(), expires_at),
                )
                self.acquired = True
                return
            holder = str(current["owner"])
            held_expires = str(current["expires_at"] or "")
            if held_expires > now.isoformat():
                raise LockNotAcquired(
                    f"다른 WAS에서 이미 실행 중입니다: {self.lock_name} · 보유자 {holder} · 만료 {held_expires}"
                )
            # The lease ran out, so the previous holder died without releasing.
            # Only one transaction can win this UPDATE thanks to the WHERE clause.
            cursor = conn.execute(
                "UPDATE process_lock SET owner=?, acquired_at=?, expires_at=? WHERE lock_name=? AND expires_at=?",
                (self.owner, now.isoformat(), expires_at, self.lock_name, held_expires),
            )
            if int(getattr(cursor, "rowcount", 0)) != 1:
                raise LockNotAcquired(f"만료된 잠금을 다른 WAS가 먼저 회수했습니다: {self.lock_name}")
            LOGGER.warning("만료된 배치 잠금을 회수했습니다: %s (이전 보유자 %s)", self.lock_name, holder)
            self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            with self.manager.connect() as conn:
                conn.execute(
                    "DELETE FROM process_lock WHERE lock_name=? AND owner=?",
                    (self.lock_name, self.owner),
                )
        except Exception:
            # A lock we fail to delete still expires on its own, so the batch
            # result must not be reported as a failure because of cleanup.
            LOGGER.exception("배치 잠금 해제 실패: %s", self.lock_name)
        finally:
            self.acquired = False
