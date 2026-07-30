from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from ..config import load_config
from ..db.manager import create_manager
from ..services import CollectionService
from ..web_common import admin_required


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
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/asset-sync/collect/vcenter", methods=["POST"])
    @bp.route("/api/asset-sync/collect/rvtools", methods=["POST"])
    @admin_required
    def collect_vcenter() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_service().collect_vcenter(mode=payload.get("mode")))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/asset-sync/run-daily", methods=["POST"])
    @admin_required
    def run_daily() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(_service().run_daily(demo=bool(payload.get("demo", False))))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    return bp
