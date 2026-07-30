"""Backwards-compatible import path.

The managers moved to ``manager.py`` when MySQL support was added, so that the
engine choice lives in one place. Existing imports of
``asset_sync.db.sqlite_manager.SQLiteManager`` keep working.
"""

from __future__ import annotations

from .manager import DatabaseError, DatabaseManager, MySQLManager, SQLiteManager, create_manager

__all__ = ["DatabaseError", "DatabaseManager", "MySQLManager", "SQLiteManager", "create_manager"]
