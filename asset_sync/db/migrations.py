from __future__ import annotations

from .sqlite_manager import SQLiteManager


def migrate(manager: SQLiteManager) -> None:
    """Idempotent schema migration entry point for the initial release."""
    manager.initialize()
