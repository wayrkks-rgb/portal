from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from flask import Blueprint, jsonify, render_template, request, send_file, session

from ..config import AppConfig
from ..db.manager import DatabaseManager
from ..repositories import AssetRepository
from ..services import (
    AutomatedReportService, ChangeSyncService, CountAuditService, DailyComparisonService,
    DashboardService, ExportService,
    IntegratedDashboardService, PeriodService, ReconciliationExceptionService,
    MONTHLY_SECTIONS, MonthlyCheckExportService, MonthlyReportService,
    ReconciliationService, ScopeCrossCheckService, ServerStatusService,
    VMResourceUsageExportService, present_all,
)
from ..services.asset_scope import AssetScope
from ..services.monthly_report_service import SORT_FIELDS
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

    def _monthly_status(conn: Any, base_day: date) -> tuple[dict[str, Any] | None, Any]:
        """월간 점검 결과 한 벌. 화면과 엑셀이 **같은 계산**을 쓰도록 여기서만 만든다.

        따로 계산하면 화면과 파일의 숫자가 달라질 수 있고, 그러면 어느 쪽을
        믿어야 하는지 알 수 없다.
        """
        repo = AssetRepository(conn)
        current = repo.snapshot_on_or_before("ITSM", base_day.isoformat())
        if not current:
            return None, None
        # 전월 말일 기준. 그 날짜까지의 마지막 스냅샷이 전월 값이 된다.
        previous_day = base_day.replace(day=1) - timedelta(days=1)
        previous = repo.snapshot_on_or_before("ITSM", previous_day.isoformat())
        service = ServerStatusService(cfg, repo, include_all=_include_all())
        previous_id = int(previous["id"]) if previous else None
        result = service.status(int(current["id"]), previous_id)
        result["eosl"] = service.eosl(int(current["id"]), previous_snapshot_id=previous_id)
        if previous:
            result["movements"] = service.movements(int(current["id"]), int(previous["id"]))
        result.update({
            "status": "SUCCESS",
            "as_of": current["snapshot_date"],
            "previous_as_of": previous["snapshot_date"] if previous else None,
            "period": {"base_day": base_day.isoformat()},
        })
        return result, service

    @bp.route("/api/asset-sync/server-status")
    @login_required
    def server_status() -> Any:
        """월간 점검의 서버 현황·EOSL. 기준일까지의 마지막 스냅샷과 전월 말일을 비교한다."""
        try:
            base_day = _month_end(request.args.get("month"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        with manager.connect() as conn:
            result, _ = _monthly_status(conn, base_day)
        if result is None:
            return jsonify({
                "status": "NO_SNAPSHOT", "as_of": None,
                "message": "해당 기간까지의 ITSM 스냅샷이 없습니다. 수집을 먼저 실행하세요.",
            })
        return jsonify(result)

    @bp.route("/api/asset-sync/count-audit")
    @login_required
    def count_audit() -> Any:
        """대수가 안 맞을 때 어디서 갈라지는지 한 자리에서 본다.

        같아야 하는 쌍(서버 현황 표의 계 = 자산 대수 = EOSL 전체 수량)은 직접
        견주고, 달라도 정상인 것(ITSM 자산 vs vCenter VM)은 왜 다른지 적는다.
        통합기를 새로 붙인 뒤 배치가 아직 안 돌았으면 그 사실도 짚어 준다.
        """
        try:
            base_day = _month_end(request.args.get("month"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        with manager.connect() as conn:
            return jsonify(CountAuditService(
                cfg, AssetRepository(conn), include_all=_include_all()
            ).audit(base_day))

    @bp.route("/api/asset-sync/server-status/export")
    @login_required
    def server_status_export() -> Any:
        """월간 점검 장표를 엑셀로. 항목별로 따로 받을 수 있다.

        월간 보고에 붙일 때 필요한 장표만 뽑는 일이 많다. 전부 한 파일로 주면
        쓰는 사람이 시트를 지워야 한다.
        """
        try:
            base_day = _month_end(request.args.get("month"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        section = str(request.args.get("section") or "all").strip().lower()
        with manager.connect() as conn:
            result, service = _monthly_status(conn, base_day)
            if result is None:
                return jsonify({"error": "해당 기간까지의 ITSM 스냅샷이 없습니다."}), 400
            # 자산 목록 시트는 한 건씩 펼친 값이 필요하다. 다른 항목만 받을 때는
            # 읽지 않는다 -- 전체 목록을 매번 펼칠 이유가 없다.
            records: list[dict[str, Any]] = []
            if section in ("all", "assets"):
                snapshot = AssetRepository(conn).snapshot_on_or_before("ITSM", base_day.isoformat())
                records = service.records(int(snapshot["id"]))
            try:
                path = MonthlyCheckExportService(result, records).save(
                    cfg.resolve("data/export/monthly_check"), section, base_day
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.route("/api/asset-sync/monthly-report")
    @login_required
    def monthly_report() -> Any:
        """월간 보고 장표 한 벌. 보고자료에 그대로 붙일 모양으로 낸다.

        시트 네 장: 통합서버자원사용현황(전월·당월), 통합기별 VM 상세,
        서버현황 대시보드(전체), 서버현황 대시보드(물리).

        줄 단위(unit)와 정렬(sort)은 화면에서 고를 수 있다. 안 주면 설정값을
        쓴다 -- 현장마다 '통합기' 가 가리키는 것과 보는 순서가 다르다.
        """
        try:
            base_day = _month_end(request.args.get("month"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        with manager.connect() as conn:
            try:
                service = MonthlyReportService(
                    cfg, AssetRepository(conn), include_all=_include_all(),
                    unit=request.args.get("unit") or None,
                    sort=request.args.get("sort") or None,
                )
            except ValueError as exc:
                # 모르는 단위·정렬 기준은 조용히 버리지 않고 화면에 알린다.
                return jsonify({"error": str(exc)}), 400
            workbook = service.build(base_day)
            target = cfg.resolve("data/export/monthly_report")
            target.mkdir(parents=True, exist_ok=True)
            path = target / service.file_name(base_day)
            workbook.save(path)
            workbook.close()
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.route("/api/asset-sync/scope-crosscheck")
    @login_required
    def scope_crosscheck() -> Any:
        """VM 제외와 서버현황이 서로 맞는지 본다.

        VM 을 전원 꺼짐 등으로 자원 집계에서 뺐는데 같은 서버가 ITSM 서버현황에는
        자산으로 남아 있으면 확인이 필요하다. 반대 방향도 같이 본다.
        """
        only_review = str(request.args.get("review") or "").strip().lower() not in {"0", "false", "all"}
        limit = min(int(request.args.get("limit", 2000)), 20000)
        with manager.connect() as conn:
            result = ScopeCrossCheckService(cfg, AssetRepository(conn)).check()
        if only_review:
            result["items"] = [item for item in result["items"] if item.get("review")]
        result["truncated"] = len(result["items"]) > limit
        result["items"] = result["items"][:limit]
        return jsonify(result)

    @bp.route("/api/asset-sync/scope-crosscheck/export")
    @login_required
    def scope_crosscheck_export() -> Any:
        """교차 점검 결과를 엑셀로. 받아서 담당자에게 돌릴 목록이다."""
        only_review = str(request.args.get("review") or "").strip().lower() not in {"0", "false", "all"}
        with manager.connect() as conn:
            result = ScopeCrossCheckService(cfg, AssetRepository(conn)).check()
        if result["status"] != "SUCCESS":
            return jsonify({"error": result.get("message") or "견줄 자료가 없습니다."}), 400
        items = [item for item in result["items"] if item.get("review")] if only_review \
            else result["items"]
        target = cfg.resolve("data/export/crosscheck")
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"제외교차점검_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        ScopeCrossCheckService.write_xlsx(result, items, path)
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.route("/api/asset-sync/monthly-report/options")
    @login_required
    def monthly_report_options() -> Any:
        """줄 단위와 정렬로 고를 수 있는 것. 화면이 이 값으로 선택을 만든다."""
        with manager.connect() as conn:
            service = MonthlyReportService(cfg, AssetRepository(conn))
        return jsonify({
            "units": [
                {"id": "ESXI", "name": "통합기(ESXi) 단위"},
                {"id": "CLUSTER", "name": "클러스터 단위"},
            ],
            "sort_fields": [{"id": key, "name": name} for key, name in SORT_FIELDS.items()],
            "current": service.describe(),
        })

    @bp.route("/api/asset-sync/server-status/sections")
    @login_required
    def server_status_sections() -> Any:
        """엑셀로 받을 수 있는 항목 목록. 화면이 버튼을 이 값으로 만든다."""
        return jsonify({"sections": [{"id": key, "name": name} for key, name in MONTHLY_SECTIONS.items()]})

    def _vcenter_rows(conn: Any) -> tuple[Any, list[dict[str, Any]], dict[str, Any]] | tuple[None, None, None]:
        """조건에 맞는 VM 목록. 자원사용현황에서 쓸 VM 을 고르는 화면이 쓴다."""
        term = str(request.args.get("q") or "").strip().lower()
        state = str(request.args.get("state") or "").strip().lower()
        wanted_vcenter = str(request.args.get("vcenter_id") or "").strip()
        wanted_cluster = str(request.args.get("cluster_name") or "").strip()
        wanted_host = str(request.args.get("esxi_host") or "").strip()

        repo = AssetRepository(conn)
        snapshot = repo.latest_snapshot("RVTOOLS")
        if not snapshot:
            return None, None, None
        scope = AssetScope.load(cfg, repo, include_all=True)
        records = list(repo.load_rv_records(int(snapshot["id"])).values())
        rows = [scope.describe_vcenter(record) for record in records]

        def keep(item: dict[str, Any]) -> bool:
            if wanted_vcenter and str(item.get("vcenter_id") or "") != wanted_vcenter:
                return False
            if wanted_cluster and str(item.get("cluster_name") or "") != wanted_cluster:
                return False
            if wanted_host and str(item.get("esxi_host") or "") != wanted_host:
                return False
            if state == "included" and item.get("exclude_reason"):
                return False
            if state == "excluded" and not item.get("exclude_reason"):
                return False
            if not term:
                return True
            haystack = [v for k, v in item.items() if k != "raw"]
            haystack.extend((item.get("raw") or {}).values())
            return term in " ".join(
                str(value).lower() for value in haystack if value not in (None, "")
            )

        matched = [item for item in rows if keep(item)]
        return snapshot, matched, {
            "snapshot_total": len(rows),
            "criteria": {"system_vm_patterns": list(scope.criteria.system_vm_patterns)},
            "scope": scope.vcenter_summary(records),
        }

    def _rows_for_source(conn: Any):
        """출처에 따라 ITSM 자산이나 vCenter VM 목록을 돌려준다."""
        if str(request.args.get("source") or "ITSM").upper() == "RVTOOLS":
            return _vcenter_rows(conn)
        return _asset_rows(conn)

    def _asset_rows(conn: Any) -> tuple[Any, list[dict[str, Any]], dict[str, Any]] | tuple[None, None, None]:
        """조건에 맞는 자산 목록. 화면과 엑셀이 같은 결과를 쓰도록 한 곳에서 고른다."""
        base_day = _month_end(request.args.get("month"))
        term = str(request.args.get("q") or "").strip().lower()
        wanted_os = str(request.args.get("os_group") or "").strip()
        wanted_location = str(request.args.get("location") or "").strip().upper()
        kind = str(request.args.get("kind") or "").strip().lower()      # physical | logical
        state = str(request.args.get("state") or "").strip().lower()    # included | excluded
        # EOSL 표의 열 이름("2028년 이상", "계획 없음"). 표의 숫자를 눌렀을 때 쓴다.
        eosl_bucket = str(request.args.get("eosl") or "").strip()
        # 전월 대비 증감(+8)을 눌렀을 때. created = 이번에 생긴 것, removed = 빠진 것.
        change = str(request.args.get("change") or "").strip().lower()

        repo = AssetRepository(conn)
        snapshot = repo.snapshot_on_or_before("ITSM", base_day.isoformat())
        if not snapshot:
            return None, None, None
        # 목록은 제외된 것도 함께 보여야 한다. 제외 사유를 달고 나온다.
        service = ServerStatusService(cfg, repo, include_all=True)
        rows = service.records(int(snapshot["id"]))

        # 증감을 보려면 전월 말일까지의 마지막 스냅샷과 견준다. 월간 점검 표가
        # 쓰는 것과 **같은 기준**이어야 표의 (+8) 과 목록의 줄 수가 맞는다.
        change_note = ""
        if change in {"created", "removed"}:
            previous_day = base_day.replace(day=1) - timedelta(days=1)
            previous = repo.snapshot_on_or_before("ITSM", previous_day.isoformat())
            if not previous:
                rows = []
                change_note = "비교할 전월 스냅샷이 없습니다."
            else:
                before = service.records(int(previous["id"]))
                # 자산으로 세는 것만 견준다. 제외된 것이 섞이면 표의 증감과 달라진다.
                now_ids = {r["cm_id"] for r in rows if not r.get("exclude_reason")}
                before_ids = {r["cm_id"] for r in before if not r.get("exclude_reason")}
                if change == "created":
                    rows = [r for r in rows
                            if not r.get("exclude_reason") and r["cm_id"] not in before_ids]
                else:
                    # 빠진 것은 이번 스냅샷에 없다. 전월 자료에서 꺼내야 한다.
                    rows = [r for r in before
                            if not r.get("exclude_reason") and r["cm_id"] not in now_ids]
                change_note = (
                    f"{previous['snapshot_date']} → {snapshot['snapshot_date']} "
                    + ("신규" if change == "created" else "삭제")
                )

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
            if eosl_bucket:
                # 표를 만든 것과 같은 함수로 묶는다. 따로 적으면 표의 숫자와
                # 목록의 줄 수가 어긋난다.
                if ServerStatusService.eosl_bucket(item.get("eosl_year"), base_day.year) != eosl_bucket:
                    return False
            if not term:
                return True
            # 집계값과 ITSM 원본 **값**에서 찾는다. 컬럼 이름은 보지 않는다 --
            # 'OS' 로 찾으면 CM_OS 라는 이름 때문에 전부 걸리기 때문이다.
            haystack = [v for k, v in item.items() if k != "raw"]
            haystack.extend((item.get("raw") or {}).values())
            return term in " ".join(
                str(value).lower() for value in haystack if value not in (None, "")
            )

        matched = [item for item in rows if keep(item)]
        return snapshot, matched, {
            "base_day": base_day,
            "snapshot_total": len(rows),
            "criteria": service.describe_criteria(),
            "change": change or None,
            "change_note": change_note or None,
        }

    @bp.route("/api/asset-sync/assets")
    @login_required
    def asset_list() -> Any:
        """자산 한 건씩의 목록.

        화면에서 OS·위치의 대수를 눌렀을 때 그 숫자가 실제로 무엇인지 보여준다.
        같은 목록을 자산 제외 관리에서도 쓴다 -- 제외할 대상을 고르려면 먼저
        찾아야 하고, 찾는 기준은 호스트명·IP·업무명 무엇이든 될 수 있다.
        """
        limit = min(int(request.args.get("limit", 500)), 20000)
        source = str(request.args.get("source") or "ITSM").upper()
        try:
            with manager.connect() as conn:
                snapshot, matched, meta = _rows_for_source(conn)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if snapshot is None:
            return jsonify({
                "status": "NO_SNAPSHOT", "items": [], "total": 0,
                "message": ("vCenter 스냅샷이 없습니다." if source == "RVTOOLS"
                            else "해당 기간까지의 ITSM 스냅샷이 없습니다."),
            })
        # 원본 전 컬럼은 화면에 쓰지 않는다. 응답만 커진다. 엑셀에서만 쓴다.
        items = [{k: v for k, v in item.items() if k != "raw"} for item in matched[:limit]]
        return jsonify({
            "status": "SUCCESS",
            "source": source,
            "as_of": snapshot["snapshot_date"],
            "total": len(matched),
            "snapshot_total": meta["snapshot_total"],
            "truncated": len(matched) > limit,
            "items": items,
            "criteria": meta["criteria"],
            "scope": meta.get("scope"),
            # 증감을 눌러 들어온 목록이면 무엇과 견준 것인지 적는다.
            "change": meta.get("change"),
            "change_note": meta.get("change_note"),
        })

    @bp.route("/api/asset-sync/assets/export")
    @login_required
    def asset_list_export() -> Any:
        """화면(팝업)에 보이는 그 목록을 엑셀로. **ITSM 원본 전 컬럼**이 들어간다.

        화면은 꼭 필요한 것만 보여 준다 -- 스무 컬럼을 늘어놓으면 읽을 수 없다.
        받아서 다시 거르고 피벗하려면 전 컬럼이 필요하므로 파일에는 전부 담는다.
        """
        source = str(request.args.get("source") or "ITSM").upper()
        try:
            with manager.connect() as conn:
                snapshot, matched, meta = _rows_for_source(conn)
                if snapshot is None:
                    return jsonify({"error": "비교할 스냅샷이 없습니다."}), 400
                default_title = "VM 목록" if source == "RVTOOLS" else "자산 목록"
                title = str(request.args.get("title") or "").strip() or default_title
                status = {
                    "as_of": snapshot["snapshot_date"],
                    "excluded": {"items": [], "reason": ""},
                }
                service = MonthlyCheckExportService(status, matched)
                workbook = service.build_asset_list(title, matched, _asset_filter_note())
                target = cfg.resolve("data/export/monthly_check")
                target.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[\\/:*?\"<>|]", "_", title)[:40]
                prefix = "VM목록" if source == "RVTOOLS" else "자산목록"
                path = target / f"{prefix}_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                workbook.save(path)
                workbook.close()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return send_file(path, as_attachment=True, download_name=path.name)

    def _asset_filter_note() -> str:
        """어떤 조건으로 고른 목록인지. 파일만 보고도 알 수 있어야 한다."""
        labels = {
            "q": "검색", "os_group": "OS", "location": "위치",
            "kind": "구분", "state": "상태", "month": "기준월", "eosl": "EOSL",
            "source": "출처", "vcenter_id": "vCenter", "cluster_name": "통합기",
            "esxi_host": "ESXi",
        }
        parts = [
            f"{label}={request.args.get(key)}"
            for key, label in labels.items() if request.args.get(key)
        ]
        return "조건: " + (" · ".join(parts) if parts else "전체")

    @bp.route("/api/asset-sync/collection-health")
    @login_required
    def collection_health() -> Any:
        """마지막 배치가 어디까지 됐는지 한 덩어리로 돌려준다.

        통합기 10 대 중 3 대가 실패하면 VM 대수는 당연히 줄어든다. 그걸 모르면
        화면만 보고 "VM 60 대가 삭제됐다" 고 읽는다. 어느 통합기가 빠졌는지와
        수집공백 건수를 어느 화면에서든 같은 모양으로 띄우기 위한 가벼운 주소다.
        """
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            batch = repo.latest_daily_batch()
            metadata = json.loads((batch or {}).get("metadata_json") or "{}")
            payload: dict[str, Any] = {
                "status": (batch or {}).get("status") or "NO_RUN",
                "started_at": (batch or {}).get("started_at"),
                "ended_at": (batch or {}).get("ended_at"),
                "reasons": [
                    item for item in (metadata.get("status_reasons") or [])
                    if isinstance(item, dict)
                ],
                "scopes": {},
            }
            run = repo.latest_collection_run("RVTOOLS")
            if run:
                payload["scopes"] = {
                    "status": run.get("status"),
                    "collected_at": run.get("ended_at") or run.get("started_at"),
                    "success": json.loads(run.get("success_scope_json") or "[]"),
                    "failed": json.loads(run.get("failed_scope_json") or "[]"),
                }
            return jsonify(payload)

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
