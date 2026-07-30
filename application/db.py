"""Shared database manager for the portal front end.

``create_manager`` reads the configuration on every call, so the manager is cached
here to avoid re-reading the YAML and .env files on each request. The manager holds
no open connection: it opens one per transaction, so a single instance is safe to
share across requests.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from asset_sync.config import load_config
from asset_sync.db.manager import create_manager


@lru_cache(maxsize=1)
def database_manager() -> Any:
    return create_manager(load_config())


def reset_database_manager() -> None:
    """Drop the cached manager. Used by tests that switch configuration."""
    database_manager.cache_clear()
