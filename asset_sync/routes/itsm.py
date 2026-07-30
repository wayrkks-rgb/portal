from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from ..config import AppConfig
from ..db.manager import DatabaseManager
from ..repositories import AssetRepository
from ..services import PeriodService
from ..web_common import login_required


def create_itsm_blueprint(cfg: AppConfig, manager: DatabaseManager) -> Blueprint:
    del cfg
    bp = Blueprint("asset_sync_itsm", __name__)

    @bp.route("/api/itsm/changes/summary")
    @login_required
    def itsm_change_summary() -> Any:
        start, end = request.args.get("start"), request.args.get("end")
        if not start or not end:
            return jsonify({"error": "start/end가 필요합니다."}), 400
        with manager.connect() as conn:
            return jsonify(PeriodService(AssetRepository(conn)).summary("ITSM", start, end))

    @bp.route("/api/itsm/changes")
    @login_required
    def itsm_changes() -> Any:
        with manager.connect() as conn:
            return jsonify(AssetRepository(conn).changes("ITSM", min(int(request.args.get("limit", 500)), 10000)))

    @bp.route("/api/itsm/changes/<int:event_id>")
    @login_required
    def itsm_change_detail(event_id: int) -> Any:
        with manager.connect() as conn:
            row = conn.execute("SELECT * FROM change_event WHERE id=? AND source='ITSM'", (event_id,)).fetchone()
            return jsonify(dict(row)) if row else (jsonify({"error": "결과가 없습니다."}), 404)

    @bp.route("/api/itsm/data-quality/summary")
    @login_required
    def quality_summary() -> Any:
        with manager.connect() as conn:
            latest = AssetRepository(conn).latest_snapshot("ITSM")
            if not latest:
                return jsonify({"status": "NO_SNAPSHOT", "counts": {}})
            rows = conn.execute(
                "SELECT quality_status, COUNT(*) cnt FROM data_quality_result WHERE snapshot_id=? GROUP BY quality_status",
                (latest["id"],),
            ).fetchall()
            return jsonify({"snapshot_id": latest["id"], "counts": {r["quality_status"]: r["cnt"] for r in rows}})

    @bp.route("/api/itsm/data-quality")
    @login_required
    def quality_rows() -> Any:
        with manager.connect() as conn:
            latest = AssetRepository(conn).latest_snapshot("ITSM")
            if not latest:
                return jsonify([])
            rows = conn.execute(
                "SELECT * FROM data_quality_result WHERE snapshot_id=? ORDER BY quality_status, cm_id LIMIT ?",
                (latest["id"], min(int(request.args.get("limit", 1000)), 10000)),
            ).fetchall()
            return jsonify([dict(row) for row in rows])

    @bp.route("/api/itsm/data-quality/<cm_id>")
    @login_required
    def quality_detail(cm_id: str) -> Any:
        with manager.connect() as conn:
            latest = AssetRepository(conn).latest_snapshot("ITSM")
            if not latest:
                return jsonify([])
            rows = conn.execute(
                "SELECT * FROM data_quality_result WHERE snapshot_id=? AND cm_id=?",
                (latest["id"], cm_id),
            ).fetchall()
            return jsonify([dict(row) for row in rows])

    return bp
