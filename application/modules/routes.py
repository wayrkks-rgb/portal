"""BFF 엔드포인트.

브라우저는 항상 통합 웹만 호출하고, 통합 웹이 도메인 WAS 를 대신 호출한다.
따라서 각 WAS 는 사설망에 남을 수 있고 CORS 설정이 필요 없다.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, jsonify, request, session

from application.common import require_login
from application.modules.client import ALLOWED_METHODS, ModuleClient, filter_request_headers
from application.modules.panels import PanelAggregator
from application.modules.registry import ModuleConfigError, ModuleRegistry

LOGGER = logging.getLogger(__name__)


def create_modules_blueprint(registry: ModuleRegistry, client: ModuleClient) -> Blueprint:
    bp = Blueprint("modules", __name__)
    aggregator = PanelAggregator(registry, client)

    def _current_user() -> dict[str, Any]:
        return dict(session.get("user") or {})

    @bp.route("/api/modules")
    @require_login
    def list_modules() -> Any:
        user = _current_user()
        role = str(user.get("role") or "user")
        return jsonify(
            {
                "modules": [module.public() for module in registry.visible(role)],
                "count": len(registry.visible(role)),
            }
        )

    @bp.route("/api/modules/health")
    @require_login
    def modules_health() -> Any:
        user = _current_user()
        results = []
        for module in registry.visible(str(user.get("role") or "user")):
            response = client.health(module.id, user=user)
            results.append(
                {
                    "module_id": module.id,
                    "module_name": module.name,
                    "location": "LOCAL" if module.is_local else "REMOTE",
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
        user = _current_user()
        params = {key: value for key, value in request.args.items() if key not in {"modules"}}
        requested = request.args.get("modules")
        module_ids = [item for item in (requested or "").split(",") if item.strip()] or None
        return jsonify(aggregator.collect(user, params, module_ids=module_ids))

    @bp.route("/api/modules/<module_id>/proxy/<path:subpath>", methods=list(ALLOWED_METHODS))
    @require_login
    def proxy(module_id: str, subpath: str) -> Any:
        """모듈 API 를 대신 호출한다.

        호출 대상 주소는 설정에 있는 base_url 이고 경로는 모듈별 허용 접두어로
        제한한다. 사용자가 임의 주소를 넣을 수 없어야 한다.
        """
        user = _current_user()
        try:
            module = registry.require(module_id)
        except ModuleConfigError as exc:
            return jsonify({"status": "FAILED", "error": str(exc)}), 404
        if not module.visible_to(str(user.get("role") or "user")):
            return jsonify({"status": "FAILED", "error": "이 모듈에 접근할 권한이 없습니다."}), 403

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
