from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from application.accounts import ensure_accounts
from application.db import database_manager
from application.local_panels import register_all as register_local_panels
from application.modules import ModuleClient, ModuleRegistry, create_modules_blueprint
from application.settings import BASE_DIR, USERS_FILE, initialize_legacy_data
from asset_sync.config import load_config as load_asset_sync_config
from asset_sync.flask_blueprint import create_asset_sync_blueprint
from asset_sync.logging_config import configure_logging


def create_app() -> Flask:
    initialize_legacy_data()
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static") if (BASE_DIR / "static").exists() else None,
    )
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "monthly-report-secret-2024-change-me")
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024

    from application.auth.routes import bp as auth_bp
    from application.report.routes import bp as report_bp
    from application.history.routes import bp as history_bp
    from application.admin.routes import bp as admin_bp
    from application.asset_excel.routes import bp as asset_excel_bp

    for blueprint in (auth_bp, report_bp, history_bp, admin_bp, asset_excel_bp):
        app.register_blueprint(blueprint)

    asset_config = load_asset_sync_config()
    configure_logging(asset_config.resolve("logs"), asset_config.log_level, asset_config)
    app.register_blueprint(create_asset_sync_blueprint(asset_config))

    # 대메뉴는 설정에 있는 모듈 레지스트리로 결정된다. 새 대메뉴를 붙일 때
    # 통합 웹 소스를 고치지 않도록 여기서는 목록을 읽어 등록만 한다.
    registry = ModuleRegistry.from_config(asset_config.modules)
    client = ModuleClient(registry)
    register_local_panels()
    app.register_blueprint(create_modules_blueprint(registry, client))
    app.extensions["module_registry"] = registry
    app.extensions["module_client"] = client

    @app.context_processor
    def inject_modules() -> dict:
        # 모든 화면이 같은 메뉴를 그리도록 템플릿에 모듈 목록을 넣어준다.
        # 명시 부여를 반영해야 대메뉴별 담당자 구분이 화면에도 적용된다.
        from application.modules.routes import current_user, granted_permissions

        user = current_user()
        if not user:
            return {"portal_modules": []}
        pairs = registry.accessible(user, granted_permissions(user))
        return {
            "portal_modules": [
                {**module.public(), "permission": permission} for module, permission in pairs
            ]
        }

    # 계정은 공유 DB에 둔다. 레거시 users.json 이 남아 있으면 최초 기동 때 이관한다.
    manager = database_manager()
    manager.initialize()
    ensure_accounts(manager, USERS_FILE)

    return app
