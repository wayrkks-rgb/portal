from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask

from application.accounts import ensure_accounts
from application.db import database_manager
from application.local_panels import register_all as register_local_panels
from application.menu import build_sidebar
from application.module_assets import (
    ModuleScopedLoader,
    discover_module_templates,
    register_local_modules,
)
from application.modules import ModuleClient, ModuleRegistry, create_modules_blueprint
from application.settings import BASE_DIR, USERS_FILE, initialize_legacy_data
from asset_sync.collectors.oracle_itsm_collector import COLLECTOR_BUILD
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

    # 파일만 덮어쓰고 프로세스를 안 껐다 켜면 예전 코드가 계속 돈다. 그 상태에서는
    # 화면 오류만 보고 원인을 판단할 수 없으므로, 기동할 때 무엇이 올라왔는지 남긴다.
    logging.getLogger(__name__).info(
        "통합 웹 기동 · 소스=%s · 설정=%s · 수집기=%s",
        BASE_DIR, asset_config.root_dir, COLLECTOR_BUILD,
    )
    app.register_blueprint(create_asset_sync_blueprint(asset_config))

    # 대메뉴는 설정에 있는 모듈 레지스트리로 결정된다. 새 대메뉴를 붙일 때
    # 통합 웹 소스를 고치지 않도록 여기서는 목록을 읽어 등록만 한다.
    registry = ModuleRegistry.from_config(asset_config.modules)
    client = ModuleClient(registry)
    register_local_panels()
    # 내부 모듈은 application/modules_local/<id>/ 에 파일을 두면 등록된다.
    # 목록을 코드에 두면 담당자가 늘 때마다 이 파일에서 병합 충돌이 난다.
    register_local_modules(app, BASE_DIR)
    app.register_blueprint(create_modules_blueprint(registry, client))
    app.extensions["module_registry"] = registry
    app.extensions["module_client"] = client

    # 모듈별 화면 파일도 마찬가지로 있으면 쓰인다. main.html 은 이 목록을 보고
    # include 하므로 담당자가 통합 웹 화면 파일을 고칠 일이 없다.
    module_templates = discover_module_templates(BASE_DIR / "templates")
    app.extensions["module_templates"] = module_templates

    # 모듈 화면의 <style> 은 그 화면 안으로 가둔다. 전역 스타일을 공유하므로
    # 담당자가 .card 를 재정의하면 다른 팀 화면까지 바뀌기 때문이다.
    app.jinja_env.loader = ModuleScopedLoader(
        str(BASE_DIR / "templates"),
        page_of={module.id: module.page or module.id for module in registry.all()},
    )

    @app.context_processor
    def inject_modules() -> dict:
        # 모든 화면이 같은 메뉴를 그리도록 템플릿에 모듈 목록을 넣어준다.
        # 명시 부여를 반영해야 대메뉴별 담당자 구분이 화면에도 적용된다.
        from application.modules.routes import current_user, granted_permissions

        user = current_user()
        if not user:
            return {"portal_modules": [], "module_templates": {}, "portal_sidebar": []}
        pairs = registry.accessible(user, granted_permissions(user))
        visible = [{**module.public(), "permission": permission} for module, permission in pairs]
        return {
            "portal_modules": visible,
            # 권한이 있는 모듈의 템플릿만 내려보낸다. 화면 파일이 있다고 해서
            # 권한 없는 사용자에게 렌더링되면 안 된다.
            "module_templates": {
                module.id: module_templates[module.id]
                for module, _ in pairs
                if module.id in module_templates
            },
            # 사이드바는 통합 웹 화면과 모듈을 같은 방식으로 그린다(소메뉴 포함).
            "portal_sidebar": build_sidebar(visible, str(user.get("role") or "user")),
        }

    # 계정은 공유 DB에 둔다. 레거시 users.json 이 남아 있으면 최초 기동 때 이관한다.
    manager = database_manager()
    manager.initialize()
    ensure_accounts(manager, USERS_FILE)

    return app
