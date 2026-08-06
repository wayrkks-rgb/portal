from __future__ import annotations

from typing import Any

import logging

from flask import Blueprint, jsonify, request

from ..collectors.oracle_connection import describe_exception
from ..config import load_config
from ..db.manager import create_manager
from ..services import CollectionService
from ..web_common import admin_required

LOGGER = logging.getLogger(__name__)


def _failure(source: str, exc: Exception) -> tuple[Any, int]:
    """실패를 화면과 로그 양쪽에 같은 내용으로 남긴다.

    str(exc) 만 쓰면 원인 사슬이 잘린다. DPY-1001 처럼 결과만 알려주는 오류가
    맨 앞에 오면 화면에는 이유가 아무것도 남지 않는다.
    """
    LOGGER.exception("%s collection failed", source)
    return jsonify({"error": describe_exception(exc)}), 500


def _service() -> CollectionService:
    cfg = load_config()
    manager = create_manager(cfg)
    manager.initialize()
    return CollectionService(cfg, manager)


def create_collection_blueprint(cfg, manager) -> Blueprint:  # type: ignore[no-untyped-def]
    del cfg, manager
    bp = Blueprint("asset_sync_collection", __name__)

    @bp.route("/api/asset-sync/collect/itsm", methods=["POST"])
    @admin_required
    def collect_itsm() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_service().collect_itsm(mode=payload.get("mode")))
        except Exception as exc:
            return _failure("ITSM", exc)

    @bp.route("/api/asset-sync/collect/vcenter", methods=["POST"])
    @bp.route("/api/asset-sync/collect/rvtools", methods=["POST"])
    @admin_required
    def collect_vcenter() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_service().collect_vcenter(mode=payload.get("mode")))
        except Exception as exc:
            return _failure("vCenter", exc)

    @bp.route("/api/asset-sync/run-daily", methods=["POST"])
    @admin_required
    def run_daily() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_service().run_daily(demo=bool(payload.get("demo", False))))
        except Exception as exc:
            return _failure("일일 배치", exc)

    return bp
