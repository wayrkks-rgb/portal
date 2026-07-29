from __future__ import annotations

from flask import Blueprint

from .config import AppConfig, load_config
from .db.sqlite_manager import SQLiteManager
from .quality.seed_rules import seed_default_rules
from .repositories import AssetRepository
from .routes import create_admin_blueprint, create_collection_blueprint, create_core_blueprint, create_itsm_blueprint


def create_asset_sync_blueprint(config: AppConfig | None = None) -> Blueprint:
    """Create a parent Blueprint composed of small role-based Blueprints."""
    cfg = config or load_config()
    manager = SQLiteManager(cfg.database_path)
    manager.initialize()
    with manager.connect() as connection:
        seed_default_rules(AssetRepository(connection), cfg)

    parent = Blueprint("asset_sync", __name__)
    parent.register_blueprint(create_core_blueprint(cfg, manager))
    parent.register_blueprint(create_collection_blueprint(cfg, manager))
    parent.register_blueprint(create_itsm_blueprint(cfg, manager))
    parent.register_blueprint(create_admin_blueprint(cfg, manager))
    return parent
