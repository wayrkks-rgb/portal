from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from flask import Blueprint, jsonify, render_template, request, send_file, session

from ..config import AppConfig
from ..db.manager import DatabaseManager
from ..repositories import AssetRepository
from ..services import (
    AutomatedReportService, ChangeSyncService, DailyComparisonService, DashboardService, ExportService,
    IntegratedDashboardService, PeriodService, ReconciliationExceptionService,
    ReconciliationService, ServerStatusService, VMResourceUsageExportService, present_all,
)
from ..services.asset_scope import AssetScope
from ..services.change_presenter import attach_identity
from ..web_common import admin_required, login_required


def _month_end(month: str | None) -> date:
    """기준일을 정한다.

    month 가 없으면 오늘이다. 이번 달은 아직 끝나지 않았으므로 '지금까지' 의 값을
    보여주고, 지난 달을 지정하면 그 달 말일 기준이 된다.
    """
    if not month:
        return date.today()
    try:
        year, month_number = (int(part) for part in str(month).split("-")[:2])
        first = date(year, month_number, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("month 는 YYYY-MM 형식이어야 합니다.") from exc
    today = date.today()
    if (first.year, first.month) == (today.year, today.month):
        return today
    next_month = date(year + (month_number == 12), (month_number % 12) + 1, 1)
    return next_month - timedelta(days=1)


def _include_all() -> bool:
    """화면이 [전체 자산] 을 켰는가.

    평소에는 실제 자산만 센다. 원본 전체가 필요할 때만 켜고, 켠 상태라는 것이
    응답에 같이 담겨 화면이 그 사실을 표시한다.
    """
    return str(request.args.get("include_all") or "").lower() in {"1", "true", "yes", "on"}


def create_core_blueprint(cfg: AppConfig, manager: DatabaseManager) -> Blueprint:
    bp = Blueprint("asset_sync_core", __name__)

    def scope_for(conn: Any) -> AssetScope:
        """모든 화면이 같은 기준을 쓰도록 한 곳에서 만든다."""
        return AssetScope.load(cfg, AssetRepository(conn), include_all=_include_all())

    @bp.route("/asset-sync")
    @login_required
    def page() -> Any:
        return render_template("main.html", user=session["user"], page="asset_sync")

    @bp.route("/api/health")
    @bp.route("/api/asset-sync/health")
    @login_required
    def health() -> Any:
        try:
            with manager.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return jsonify({
                "status": "UP",
                "engine": manager.engine,
                "database": manager.describe(),
            })
        except Exception as exc:
            return jsonify({"status": "DOWN", "error": str(exc)}), 500

    @bp.route("/api/dashboard/summary")
    @bp.route("/api/asset-sync/dashboard")
    @login_required
    def dashboard_summary() -> Any:
        try:
            with manager.connect() as conn:
                result = IntegratedDashboardService(AssetRepository(conn), scope_for(conn)).summary(
                    start=request.args.get("start"),
                    end=request.args.get("end"),
                    detail_limit=min(int(request.args.get("limit", 500)), 5000),
                )
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/asset-sync/dashboard/legacy")
    @login_required
    def legacy_dashboard_summary() -> Any:
        with manager.connect() as conn:
            return jsonify(DashboardService(AssetRepository(conn)).summary())

    # ── 점검 화면 ──────────────────────────────────────────────────────
    # 점검 주기별로 화면을 나눈다. 일간에서 모은 것을 주간이 묶고, 월간이 장표로 낸다.
    @bp.route("/daily-check")
    @login_required
    def daily_check_page() -> Any:
        return render_template("main.html", user=session["user"], page="daily_check")

    @bp.route("/weekly-check")
    @login_required
    def weekly_check_page() -> Any:
        return render_template("main.html", user=session["user"], page="weekly_check")

    @bp.route("/monthly-check")
    @login_required
    def monthly_check_page() -> Any:
        return render_template("main.html", user=session["user"], page="monthly_check")

    @bp.route("/api/asset-sync/server-status")
    @login_required
    def server_status() -> Any:
        """월간 점검의 서버 현황·EOSL. 기준일까지의 마지막 스냅샷과 전월 말일을 비교한다."""
        try:
            base_day = _month_end(request.args.get("month"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            current = repo.snapshot_on_or_before("ITSM", base_day.isoformat())
            if not current:
                return jsonify({
                    "status": "NO_SNAPSHOT", "as_of": None,
                    "message": "해당 기간까지의 ITSM 스냅샷이 없습니다. 수집을 먼저 실행하세요.",
                })
            # 전월 말일 기준. 그 날짜까지의 마지막 스냅샷이 전월 값이 된다.
            previous_day = base_day.replace(day=1) - timedelta(days=1)
            previous = repo.snapshot_on_or_before("ITSM", previous_day.isoformat())
            service = ServerStatusService(cfg, repo, include_all=_include_all())
            result = service.status(int(current["id"]), int(previous["id"]) if previous else None)
            result["eosl"] = service.eosl(int(current["id"]))
            if previous:
                result["movements"] = service.movements(int(current["id"]), int(previous["id"]))
        result.update({
            "status": "SUCCESS",
            "as_of": current["snapshot_date"],
            "previous_as_of": previous["snapshot_date"] if previous else None,
            "period": {"base_day": base_day.isoformat()},
        })
        return jsonify(result)

    @bp.route("/api/asset-sync/assets")
    @login_required
    def asset_list() -> Any:
        """자산 한 건씩의 목록.

        화면에서 OS·위치의 대수를 눌렀을 때 그 숫자가 실제로 무엇인지 보여준다.
        같은 목록을 자산 제외 관리에서도 쓴다 -- 제외할 대상을 고르려면 먼저
        찾아야 하고, 찾는 기준은 호스트명·IP·업무명 무엇이든 될 수 있다.
        """
        try:
            base_day = _month_end(request.args.get("month"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        term = str(request.args.get("q") or "").strip().lower()
        wanted_os = str(request.args.get("os_group") or "").strip()
        wanted_location = str(request.args.get("location") or "").strip().upper()
        kind = str(request.args.get("kind") or "").strip().lower()      # physical | logical
        state = str(request.args.get("state") or "").strip().lower()    # included | excluded
        limit = min(int(request.args.get("limit", 500)), 20000)

        with manager.connect() as conn:
            repo = AssetRepository(conn)
            snapshot = repo.snapshot_on_or_before("ITSM", base_day.isoformat())
            if not snapshot:
                return jsonify({"status": "NO_SNAPSHOT", "items": [], "total": 0,
                                "message": "해당 기간까지의 ITSM 스냅샷이 없습니다."})
            # 목록은 제외된 것도 함께 보여야 한다. 제외 사유를 달고 나온다.
            service = ServerStatusService(cfg, repo, include_all=True)
            rows = service.records(int(snapshot["id"]))
            criteria = service.describe_criteria()

        def keep(item: dict[str, Any]) -> bool:
            if wanted_os and item.get("os_group") != wanted_os:
                return False
            if wanted_location and str(item.get("location") or "").upper() != wanted_location:
                return False
            if kind == "physical" and not item.get("physical"):
                return False
            if kind == "logical" and item.get("physical"):
                return False
            if state == "included" and item.get("exclude_reason"):
                return False
            if state == "excluded" and not item.get("exclude_reason"):
                return False
            if not term:
                return True
            # 호스트명·IP·업무명뿐 아니라 담긴 값 전부에서 찾는다. 운영자가 무엇으로
            # 기억하고 있을지 모르기 때문이다.
            return term in " ".join(
                str(value).lower() for value in item.values() if value not in (None, "")
            )

        matched = [item for item in rows if keep(item)]
        return jsonify({
            "status": "SUCCESS",
            "as_of": snapshot["snapshot_date"],
            "total": len(matched),
            "snapshot_total": len(rows),
            "truncated": len(matched) > limit,
            "items": matched[:limit],
            "criteria": criteria,
        })

    @bp.route("/api/collection-runs")
    @bp.route("/api/asset-sync/collection-runs")
    @login_required
    def collection_runs() -> Any:
        limit = min(int(request.args.get("limit", 100)), 1000)
        with manager.connect() as conn:
            return jsonify(AssetRepository(conn).collection_runs(limit))

    @bp.route("/api/asset-sync/daily-comparison")
    @login_required
    def daily_comparison() -> Any:
        source = request.args.get("source", "ITSM").upper()
        limit = min(int(request.args.get("limit", 2000)), 10000)
        try:
            with manager.connect() as conn:
                return jsonify(DailyComparisonService(
                    cfg, AssetRepository(conn), scope_for(conn)
                ).latest(source, limit))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/changes")
    @bp.route("/api/asset-sync/changes")
    @login_required
    def changes() -> Any:
        source = request.args.get("source")
        limit = min(int(request.args.get("limit", 500)), 10000)
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            rows = repo.changes(
                source=source,
                limit=limit,
                start=request.args.get("start"),
                end=request.args.get("end"),
            )
            # 자산코드만 있으면 어느 서버인지 알 수 없다. 호스트명·IP·업무명을
            # 붙이고, 자산에서 뺀 대상의 변경은 여기서도 뺀다.
            rows = attach_identity(
                rows, repo,
                scope=None if _include_all() else scope_for(conn),
            )
        # 코드값·원본 JSON 을 그대로 내보내면 화면에서 읽을 수 없다.
        return jsonify(present_all(rows))

    @bp.route("/api/reconciliation")
    @bp.route("/api/asset-sync/reconciliation")
    @login_required
    def reconciliation() -> Any:
        limit = min(int(request.args.get("limit", 1000)), 10000)
        status = request.args.get("status")
        with manager.connect() as conn:
            rows = AssetRepository(conn).reconciliation(limit=limit, status=status)
            for row in rows:
                row["drifts"] = json.loads(row.pop("drift_json") or "[]")
            return jsonify(rows)

    @bp.route("/api/reconciliation/<int:result_id>")
    @login_required
    def reconciliation_detail(result_id: int) -> Any:
        with manager.connect() as conn:
            row = conn.execute("SELECT * FROM reconciliation_result WHERE id=?", (result_id,)).fetchone()
            if not row:
                return jsonify({"error": "결과가 없습니다."}), 404
            data = dict(row)
            data["drifts"] = json.loads(data.pop("drift_json") or "[]")
            return jsonify(data)

    @bp.route("/api/asset-sync/reconcile", methods=["POST"])
    @admin_required
    def run_reconciliation() -> Any:
        with manager.connect() as conn:
            return jsonify(ReconciliationService(cfg, AssetRepository(conn)).reconcile_latest())

    @bp.route("/api/asset-sync/sync-results")
    @login_required
    def sync_results() -> Any:
        limit = min(int(request.args.get("limit", 1000)), 10000)
        with manager.connect() as conn:
            return jsonify(ChangeSyncService(cfg, AssetRepository(conn)).latest_results(limit))

    @bp.route("/api/asset-sync/evaluate-sync", methods=["POST"])
    @admin_required
    def evaluate_sync() -> Any:
        payload = request.get_json() or {}
        if not payload.get("start") or not payload.get("end"):
            return jsonify({"error": "start/end가 필요합니다."}), 400
        with manager.connect() as conn:
            return jsonify(ChangeSyncService(cfg, AssetRepository(conn)).evaluate(payload["start"], payload["end"]))

    @bp.route("/api/asset-sync/period-summary")
    @login_required
    def period_summary() -> Any:
        source = request.args.get("source", "ITSM").upper()
        mode = request.args.get("mode", "custom")
        with manager.connect() as conn:
            service = PeriodService(AssetRepository(conn))
            if mode == "daily":
                return jsonify(service.daily(source, request.args["date"]))
            if mode == "weekly":
                return jsonify(service.weekly(source, request.args["end_date"]))
            if mode == "monthly":
                return jsonify(service.monthly(source, int(request.args["year"]), int(request.args["month"])))
            return jsonify(service.summary(source, request.args["start"], request.args["end"]))

    @bp.route("/api/asset-sync/reconciliation/candidates")
    @login_required
    def reconciliation_candidates() -> Any:
        with manager.connect() as conn:
            rows = IntegratedDashboardService(AssetRepository(conn)).exception_candidates(
                min(int(request.args.get("limit", 5000)), 10000)
            )
        return jsonify(rows)

    @bp.route("/api/asset-sync/exceptions")
    @login_required
    def reconciliation_exceptions() -> Any:
        include_inactive = request.args.get("include_inactive", "1") != "0"
        with manager.connect() as conn:
            return jsonify(ReconciliationExceptionService(AssetRepository(conn)).list(include_inactive))

    @bp.route("/api/asset-sync/exceptions", methods=["POST"])
    @admin_required
    def create_reconciliation_exceptions() -> Any:
        payload = request.get_json(silent=True) or {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
        user_id = str(session.get("user", {}).get("username") or session.get("user", {}).get("name") or "ADMIN")
        try:
            with manager.connect() as conn:
                result = ReconciliationExceptionService(AssetRepository(conn)).create_many(items, user_id)
            code = 201 if result["created_count"] else 400
            return jsonify(result), code
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/asset-sync/exceptions/<int:exception_id>", methods=["DELETE"])
    @admin_required
    def deactivate_reconciliation_exception(exception_id: int) -> Any:
        user_id = str(session.get("user", {}).get("username") or session.get("user", {}).get("name") or "ADMIN")
        with manager.connect() as conn:
            changed = ReconciliationExceptionService(AssetRepository(conn)).deactivate(exception_id, user_id)
        if not changed:
            return jsonify({"error": "활성 예외처리를 찾을 수 없습니다."}), 404
        return jsonify({"status": "SUCCESS", "exception_id": exception_id})

    @bp.route("/api/asset-sync/exceptions/import", methods=["POST"])
    @admin_required
    def import_reconciliation_exceptions() -> Any:
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"error": "CSV 또는 XLSX 파일이 필요합니다."}), 400
        try:
            items = _read_exception_upload(upload.filename, upload.read())
            user_id = str(session.get("user", {}).get("username") or session.get("user", {}).get("name") or "ADMIN")
            with manager.connect() as conn:
                result = ReconciliationExceptionService(AssetRepository(conn)).create_many(items, user_id)
            return jsonify(result), 201 if result["created_count"] else 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/asset-sync/resource-usage")
    @login_required
    def resource_usage_summary() -> Any:
        start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()
        end = request.args.get("end") or (date.today() - timedelta(days=1)).isoformat()
        try:
            with manager.connect() as conn:
                result = VMResourceUsageExportService(cfg, AssetRepository(conn)).summary(
                    start, end,
                    vcenter_id=request.args.get("vcenter_id") or None,
                    cluster_name=request.args.get("cluster_name") or None,
                    esxi_host=request.args.get("esxi_host") or None,
                )
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/asset-sync/resource-usage/export")
    @login_required
    def resource_usage_export() -> Any:
        start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()
        end = request.args.get("end") or (date.today() - timedelta(days=1)).isoformat()
        try:
            with manager.connect() as conn:
                path = VMResourceUsageExportService(cfg, AssetRepository(conn)).export_xlsx(
                    start, end, cfg.resolve("data/export/resource_usage"),
                    vcenter_id=request.args.get("vcenter_id") or None,
                    cluster_name=request.args.get("cluster_name") or None,
                    esxi_host=request.args.get("esxi_host") or None,
                )
            return send_file(path, as_attachment=True, download_name=path.name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/asset-sync/resource-usage/import", methods=["POST"])
    @admin_required
    def import_resource_usage() -> Any:
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"error": "VM_ResourceUsageExport 결과 파일이 필요합니다."}), 400
        target = cfg.resolve("data/temp/resource_usage_import") / Path(upload.filename).name
        target.parent.mkdir(parents=True, exist_ok=True)
        upload.save(target)
        try:
            with manager.connect() as conn:
                result = VMResourceUsageExportService(cfg, AssetRepository(conn)).import_file(
                    target, request.form.get("stat_date") or None
                )
            return jsonify(result)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            target.unlink(missing_ok=True)

    @bp.route("/api/asset-sync/reports/<report_type>")
    @login_required
    def automated_report(report_type: str) -> Any:
        try:
            with manager.connect() as conn:
                path = AutomatedReportService(
                    AssetRepository(conn), cfg.resolve("data/export/automated_reports"),
                    scope_for(conn),
                ).generate(report_type, request.args.get("start"), request.args.get("end"))
            return send_file(path, as_attachment=True, download_name=path.name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/api/asset-sync/export")
    @login_required
    def export_results() -> Any:
        with manager.connect() as conn:
            path = ExportService(AssetRepository(conn), cfg.resolve("data/export")).export_current()
        return send_file(path, as_attachment=True, download_name=path.name)

    return bp


def _read_exception_upload(filename: str, content: bytes) -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        text = content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
    elif suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        values = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(values)]
        rows = [dict(zip(headers, row)) for row in values]
    else:
        raise ValueError("지원 파일은 CSV 또는 XLSX입니다.")
    aliases = {
        "예외유형": "exception_type", "EXCEPTION_TYPE": "exception_type",
        "CM_ID": "cm_id", "ITSM_ID": "cm_id",
        "VCENTER_KEY": "rv_asset_key", "RV_ASSET_KEY": "rv_asset_key", "VM_UUID": "rv_asset_key",
        "서버명": "server_name", "SERVER_NAME": "server_name",
        "사유": "reason", "REASON": "reason",
        "시작일": "valid_from", "VALID_FROM": "valid_from",
        "종료일": "valid_to", "VALID_TO": "valid_to",
    }
    result: list[dict[str, Any]] = []
    for source in rows:
        item: dict[str, Any] = {}
        for key, value in source.items():
            alias = aliases.get(str(key or "").strip().upper()) or aliases.get(str(key or "").strip())
            if alias:
                item[alias] = value
        if any(value not in (None, "") for value in item.values()):
            result.append(item)
    if not result:
        raise ValueError("등록할 예외 데이터가 없습니다.")
    return result
