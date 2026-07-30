"""BFF 엔드포인트.

브라우저는 항상 통합 웹만 호출하고, 통합 웹이 도메인 WAS 를 대신 호출한다.
따라서 각 WAS 는 사설망에 남을 수 있고 CORS 설정이 필요 없다.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, session

from application.common import require_admin, require_login
from application.db import database_manager
from asset_sync.repositories import AssetRepository
from application.modules.client import ALLOWED_METHODS, ModuleClient, filter_request_headers
from application.modules.panels import PanelAggregator
from application.modules.registry import ModuleConfigError, ModuleRegistry
from application.permissions import (
    PERMISSION_MANAGE,
    PERMISSION_NONE,
    ModulePermissionRepository,
    at_least,
    resolve_permission,
)

LOGGER = logging.getLogger(__name__)

# 조회가 아닌 메서드는 MANAGE 권한을 요구한다.
_WRITE_METHODS = {"POST", "PUT", "DELETE"}


def current_user() -> dict[str, Any]:
    return dict(session.get("user") or {})


def granted_permissions(user: dict[str, Any]) -> dict[str, str]:
    """로그인 사용자의 명시 부여 목록. 관리자는 조회할 필요가 없다."""
    if str(user.get("role") or "") == "admin" or not user.get("id"):
        return {}
    try:
        with database_manager().connect() as conn:
            return ModulePermissionRepository(conn).for_user(int(user["id"]))
    except Exception:
        # 권한 조회 실패로 화면 전체가 죽는 것보다, 부여 없음으로 보고 등급 판정에
        # 맡기는 편이 낫다. 실패는 로그로 남긴다.
        LOGGER.exception("모듈 권한 조회 실패: user_id=%s", user.get("id"))
        return {}


def create_modules_blueprint(registry: ModuleRegistry, client: ModuleClient) -> Blueprint:
    bp = Blueprint("modules", __name__)
    aggregator = PanelAggregator(registry, client)

    @bp.route("/api/modules")
    @require_login
    def list_modules() -> Any:
        user = current_user()
        pairs = registry.accessible(user, granted_permissions(user))
        return jsonify(
            {
                "modules": [{**module.public(), "permission": permission} for module, permission in pairs],
                "count": len(pairs),
            }
        )

    @bp.route("/api/modules/health")
    @require_login
    def modules_health() -> Any:
        user = current_user()
        results = []
        for module, permission in registry.accessible(user, granted_permissions(user)):
            response = client.health(module.id, user=user)
            results.append(
                {
                    "module_id": module.id,
                    "module_name": module.name,
                    "location": "LOCAL" if module.is_local else "REMOTE",
                    "permission": permission,
                    "status": response.status,
                    "http_status": response.http_status,
                    "elapsed_ms": response.elapsed_ms,
                    "error": response.error,
                }
            )
        down = [item["module_id"] for item in results if item["status"] not in ("SUCCESS", "SKIPPED")]
        return jsonify({"modules": results, "down": down, "status": "DEGRADED" if down else "UP"})

    @bp.route("/api/modules/dashboard")
    @require_login
    def modules_dashboard() -> Any:
        user = current_user()
        params = {key: value for key, value in request.args.items() if key not in {"modules"}}
        requested = request.args.get("modules")
        module_ids = [item for item in (requested or "").split(",") if item.strip()] or None
        return jsonify(
            aggregator.collect(user, params, module_ids=module_ids, granted=granted_permissions(user))
        )

    @bp.route("/api/modules/reload", methods=["POST"])
    @require_admin
    def reload_modules() -> Any:
        """설정 파일과 화면 파일을 다시 읽는다.

        대메뉴를 추가할 때마다 통합 웹을 재시작하면 다른 담당자들의 화면까지 잠깐
        끊긴다. 레지스트리는 같은 객체의 내용만 바꾸므로 진행 중인 요청도 안전하다.
        """
        from application.module_assets import discover_module_templates
        from application.settings import BASE_DIR
        from asset_sync.config import load_config

        before = {module.id for module in registry.all()}
        try:
            config = load_config()
            after = registry.reload_from_config(config.modules)
        except Exception as exc:
            LOGGER.exception("모듈 레지스트리 재적용 실패")
            return jsonify({"status": "FAILED", "error": str(exc)}), 400

        # 화면 파일 목록도 같은 dict 객체를 비우고 채운다(템플릿이 참조 중이다).
        templates = current_app.extensions.get("module_templates")
        if templates is not None:
            templates.clear()
            templates.update(discover_module_templates(BASE_DIR / "templates"))
        # page 키가 바뀌면 CSS 범위 선택자도 달라진다. 캐시를 비우기 전에 갱신한다.
        loader = getattr(current_app.jinja_env, "loader", None)
        if hasattr(loader, "page_of"):
            loader.page_of = {module.id: module.page or module.id for module in registry.all()}
        # 템플릿 캐시를 비워야 바뀐 화면 파일이 반영된다.
        if current_app.jinja_env.cache is not None:
            current_app.jinja_env.cache.clear()

        added, removed = sorted(set(after) - before), sorted(before - set(after))
        user = current_user()
        try:
            with database_manager().connect() as conn:
                AssetRepository(conn).audit(
                    str(user.get("username") or "system"),
                    "RELOAD",
                    "module_registry",
                    None,
                    None,
                    {"modules": sorted(before)},
                    {"modules": sorted(after)},
                )
        except Exception:
            # 감사 기록 실패로 재적용을 되돌릴 이유는 없다.
            LOGGER.exception("모듈 재적용 감사 기록 실패")
        LOGGER.info("모듈 레지스트리 재적용: 추가 %s · 제거 %s", added or "없음", removed or "없음")
        return jsonify({"status": "SUCCESS", "modules": sorted(after), "added": added, "removed": removed})

    @bp.route("/api/modules/<module_id>/proxy/<path:subpath>", methods=list(ALLOWED_METHODS))
    @require_login
    def proxy(module_id: str, subpath: str) -> Any:
        """모듈 API 를 대신 호출한다.

        호출 대상 주소는 설정에 있는 base_url 이고 경로는 모듈별 허용 접두어로
        제한한다. 사용자가 임의 주소를 넣을 수 없어야 한다.
        """
        user = current_user()
        try:
            module = registry.require(module_id)
        except ModuleConfigError as exc:
            return jsonify({"status": "FAILED", "error": str(exc)}), 404

        permission = resolve_permission(module, user, granted_permissions(user))
        if permission == PERMISSION_NONE:
            return jsonify({"status": "FAILED", "error": "이 모듈에 접근할 권한이 없습니다."}), 403
        if request.method in _WRITE_METHODS and not at_least(permission, PERMISSION_MANAGE):
            return jsonify(
                {"status": "FAILED", "error": "이 모듈에서 변경 작업을 수행할 권한이 없습니다."}
            ), 403

        response = client.call(
            module_id,
            "/" + subpath,
            method=request.method,
            user=user,
            params=request.args.to_dict(flat=True),
            body=request.get_data() or None,
            extra_headers={
                **filter_request_headers(dict(request.headers)),
                **({"Content-Type": request.content_type} if request.content_type else {}),
            },
            parse_json=False,
            permission=permission,
        )
        if response.status != "SUCCESS" and response.raw is None:
            status_code = 504 if response.status in ("TIMEOUT", "UNREACHABLE") else 502
            return (
                jsonify(
                    {
                        "status": response.status,
                        "module_id": module_id,
                        "error": response.error,
                        "elapsed_ms": response.elapsed_ms,
                    }
                ),
                status_code,
            )
        proxied = Response(
            response.raw or b"",
            status=response.http_status or 200,
            content_type=response.content_type,
        )
        for key, value in response.headers.items():
            if key.lower() not in {"content-type"}:
                proxied.headers[key] = value
        return proxied

    return bp
