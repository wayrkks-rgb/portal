from __future__ import annotations

import copy
import json
import logging
import socket
from typing import Any

from flask import Blueprint, jsonify, render_template, request, session

from ..collectors import OracleITSMCollector, PowerCLICollector, SyntheticRVToolsCollector, VCenterSnapshotFileCollector
from ..config import AppConfig, load_config
from ..db.sqlite_manager import SQLiteManager
from ..repositories import AssetRepository
from ..settings_store import LocalSettingsStore, SettingsValidationError
from ..web_common import admin_required

LOGGER = logging.getLogger(__name__)


def create_admin_blueprint(cfg: AppConfig, manager: SQLiteManager) -> Blueprint:
    bp = Blueprint("asset_sync_admin", __name__)
    settings_store = LocalSettingsStore(cfg.root_dir)

    @bp.route("/integration-settings")
    @admin_required
    def integration_settings_page() -> Any:
        return render_template("main.html", user=session["user"], page="integration_settings")

    @bp.route("/api/asset-sync/admin/settings", methods=["GET", "PUT"])
    @admin_required
    def integration_settings() -> Any:
        if request.method == "GET":
            return jsonify(settings_store.public_settings())
        try:
            payload = request.get_json() or {}
            saved = settings_store.save(payload)
            return jsonify({"success": True, "settings": saved})
        except (SettingsValidationError, ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"error": f"설정파일 저장 실패: {exc}"}), 500

    @bp.route("/api/asset-sync/admin/settings/oracle", methods=["PUT"])
    @admin_required
    def save_oracle_settings() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            saved = settings_store.save_oracle(payload)
            return jsonify({
                "success": True,
                "message": "Oracle/ITSM 설정을 저장했습니다.",
                "oracle": saved["oracle"],
                "itsm": saved["itsm"],
            })
        except (SettingsValidationError, ValueError, TypeError) as exc:
            return jsonify({"success": False, "stage": "VALIDATION", "error": str(exc)}), 400
        except OSError as exc:
            LOGGER.exception("Oracle settings save failed")
            return jsonify({"success": False, "stage": "SAVE", "error": f"Oracle 설정 저장 실패: {exc}"}), 500

    @bp.route("/api/asset-sync/admin/settings/vcenter", methods=["PUT"])
    @admin_required
    def save_vcenter_settings() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            saved = settings_store.save_vcenter(payload)
            return jsonify({
                "success": True,
                "message": "PowerCLI/vCenter 설정을 저장했습니다.",
                "vcenter": saved["vcenter"],
            })
        except (SettingsValidationError, ValueError, TypeError) as exc:
            return jsonify({"success": False, "stage": "VALIDATION", "error": str(exc)}), 400
        except OSError as exc:
            LOGGER.exception("vCenter settings save failed")
            return jsonify({"success": False, "stage": "SAVE", "error": f"vCenter 설정 저장 실패: {exc}"}), 500

    @bp.route("/api/asset-sync/admin/settings/runtime", methods=["PUT"])
    @admin_required
    def save_runtime_settings() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            saved = settings_store.save_runtime(payload)
            return jsonify({
                "success": True,
                "message": "실행시간/로그 설정을 저장했습니다.",
                "scheduler": saved["scheduler"],
                "security": saved["security"],
            })
        except (SettingsValidationError, ValueError, TypeError) as exc:
            return jsonify({"success": False, "stage": "VALIDATION", "error": str(exc)}), 400
        except OSError as exc:
            LOGGER.exception("Runtime settings save failed")
            return jsonify({"success": False, "stage": "SAVE", "error": f"실행 설정 저장 실패: {exc}"}), 500

    def _oracle_error_stage(exc: Exception) -> str:
        message = str(exc).upper()
        if "ORACLEDB 패키지" in message or "NO MODULE NAMED" in message:
            return "DRIVER"
        if "ORA-01017" in message or "INVALID USERNAME/PASSWORD" in message or "AUTHENTICATION" in message:
            return "AUTHENTICATION"
        if any(token in message for token in ("ORA-12154", "ORA-125", "DPY-6005", "LISTENER", "CONNECTION REFUSED", "TIMED OUT")):
            return "CONNECTION"
        if "조회 컬럼 누락" in str(exc) or "ORA-00904" in message:
            return "SCHEMA"
        if any(token in message for token in ("SQL 파일", "ORA-00942", "ORA-00933", "ORA-00936")):
            return "QUERY"
        if "0건" in str(exc):
            return "EMPTY_RESULT"
        return "ORACLE"

    @bp.route("/api/asset-sync/admin/test/oracle", methods=["POST"])
    @admin_required
    def test_oracle() -> Any:
        payload = request.get_json(silent=True) or {}
        scope = str(payload.get("scope") or "ASSET_QUERY").strip().upper()
        if scope not in {"CONNECTION", "ASSET_QUERY"}:
            return jsonify({
                "status": "FAILED",
                "stage": "VALIDATION",
                "error": "Oracle 테스트 범위가 올바르지 않습니다.",
            }), 400
        try:
            current_cfg = settings_store.oracle_test_config(
                payload,
                require_query=scope == "ASSET_QUERY",
            )
        except (SettingsValidationError, ValueError, TypeError) as exc:
            return jsonify({
                "status": "FAILED",
                "stage": "VALIDATION",
                "error": str(exc),
                "details": ["입력값 검증 단계에서 중단되었습니다."],
            }), 400

        host = str(current_cfg.oracle.get("host") or "").strip()
        port = int(current_cfg.oracle.get("port") or 1521)
        dsn = str(current_cfg.oracle.get("dsn") or "").strip()
        network: dict[str, Any]
        if host and not dsn:
            try:
                with socket.create_connection((host, port), timeout=5):
                    network = {"status": "SUCCESS", "host": host, "port": port, "message": "TCP 연결 성공"}
            except OSError as exc:
                return jsonify({
                    "status": "FAILED",
                    "stage": "NETWORK",
                    "network": {"status": "FAILED", "host": host, "port": port, "error": str(exc)},
                    "error": f"Oracle TCP 통신 실패: {exc}",
                    "details": [f"대상 {host}:{port}", "WAS 방화벽, DB Listener, 라우팅을 확인하세요."],
                }), 502
        else:
            network = {"status": "SKIPPED", "reason": "DSN 연결은 Oracle 드라이버 접속 단계에서 확인합니다."}

        try:
            if scope == "CONNECTION":
                try:
                    import oracledb  # type: ignore
                except ImportError as exc:
                    raise RuntimeError("oracledb 패키지가 설치되지 않았습니다.") from exc

                oracle_cfg = current_cfg.oracle
                mode = str(oracle_cfg.get("mode") or "thin").lower()
                if mode == "thick":
                    client_dir = str(oracle_cfg.get("client_lib_dir") or "").strip()
                    try:
                        oracledb.init_oracle_client(lib_dir=client_dir)
                    except Exception as exc:
                        if "already" not in str(exc).lower():
                            raise RuntimeError(f"Oracle Client 초기화 실패: {exc}") from exc

                kwargs: dict[str, Any] = {
                    "user": str(oracle_cfg.get("user") or "").strip(),
                    "password": str(oracle_cfg.get("password") or ""),
                }
                if dsn:
                    kwargs["dsn"] = dsn
                else:
                    kwargs.update({"host": host, "port": port})
                    service_name = str(oracle_cfg.get("service_name") or "").strip()
                    sid = str(oracle_cfg.get("sid") or "").strip()
                    kwargs["service_name" if service_name else "sid"] = service_name or sid

                with oracledb.connect(**kwargs) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM DUAL")
                        row = cursor.fetchone()
                        if not row or int(row[0]) != 1:
                            raise RuntimeError("Oracle 기본 조회 결과가 올바르지 않습니다.")
                return jsonify({
                    "status": "SUCCESS",
                    "stage": "CONNECTION_VALIDATED",
                    "network": network,
                    "message": "Oracle TCP 통신, 계정 로그인, 기본 조회가 모두 성공했습니다.",
                })

            result = OracleITSMCollector(current_cfg).test_connection()
            result["network"] = network
            result.setdefault("stage", "QUERY_VALIDATED")
            result["message"] = "Oracle 로그인, 자산 SQL 실행, 필수 컬럼 검증이 모두 성공했습니다."
            return jsonify(result)
        except Exception as exc:
            stage = _oracle_error_stage(exc)
            LOGGER.exception("Oracle connection test failed at %s", stage)
            status_code = 401 if stage == "AUTHENTICATION" else 422 if stage in {"QUERY", "SCHEMA", "EMPTY_RESULT"} else 500
            return jsonify({
                "status": "FAILED",
                "stage": stage,
                "network": network,
                "error": str(exc),
                "details": [
                    "Oracle 드라이버 접속 또는 조회 단계에서 실패했습니다.",
                    "화면의 접속정보와 서버 내부 자산조회 설정을 확인하세요.",
                ],
            }), status_code

    def _merge_vcenter_test_config(payload: dict[str, Any], entries: list[dict[str, Any]]) -> tuple[AppConfig, list[dict[str, Any]]]:
        """Build an in-memory test config without forcing the operator to save first.

        Blank passwords reuse already-saved local values by vCenter ID. This lets the
        UI test newly edited addresses immediately while keeping existing secrets out
        of the browser response.
        """
        current_cfg = load_config()
        test_cfg = copy.deepcopy(current_cfg)
        runtime = dict(payload.get("vcenter_config") or {})
        saved_entries = {
            str(item.get("id")): dict(item)
            for item in current_cfg.rvtools.get("vcenters", [])
            if str(item.get("id", "")).strip()
        }
        if not str(runtime.get("default_password") or ""):
            runtime["default_password"] = str(current_cfg.rvtools.get("default_password") or "")
        test_cfg.rvtools.update(runtime)
        # A connection test always performs a direct PowerCLI login regardless of
        # the daily collector's current DEMO/FILE_ONLY mode.
        test_cfg.rvtools["collection_mode"] = "POWERCLI"

        merged_entries: list[dict[str, Any]] = []
        for raw in entries:
            entry = dict(raw)
            vc_id = str(entry.get("id") or "").strip()
            saved = saved_entries.get(vc_id, {})
            if not str(entry.get("password") or ""):
                entry["password"] = str(saved.get("password") or saved.get("encrypted_password") or "")
            if not entry.get("username") and str(entry.get("auth_profile", "CUSTOM")).upper() == "CUSTOM":
                entry["username"] = str(saved.get("username") or "")
            merged_entries.append(entry)
        test_cfg.rvtools["vcenters"] = merged_entries
        return test_cfg, merged_entries

    @bp.route("/api/asset-sync/admin/test/vcenter", methods=["POST"])
    @bp.route("/api/asset-sync/admin/test/rvtools", methods=["POST"])
    @admin_required
    def test_vcenter() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            submitted = payload.get("entry")
            if isinstance(submitted, dict):
                test_cfg, entries = _merge_vcenter_test_config(payload, [submitted])
                return jsonify(PowerCLICollector(test_cfg).test_one(entries[0]))

            current_cfg = load_config()
            mode = str(current_cfg.rvtools.get("collection_mode", "POWERCLI")).upper()
            if mode == "DEMO":
                rows, meta = SyntheticRVToolsCollector(current_cfg).collect()
                return jsonify({"status": "SUCCESS", "mode": mode, "row_count": len(rows), "metadata": meta})
            if mode == "FILE_ONLY":
                rows, meta = VCenterSnapshotFileCollector(current_cfg).collect()
                return jsonify({"status": "SUCCESS", "mode": mode, "row_count": len(rows), "metadata": meta})
            vc_id = str(payload.get("vcenter_id") or "")
            vcenters = current_cfg.rvtools.get("vcenters", [])
            entry = next((item for item in vcenters if str(item.get("id")) == vc_id), None) if vc_id else None
            if entry is None:
                entry = next((item for item in vcenters if bool(item.get("enabled", True))), None)
            if entry is None:
                raise RuntimeError("테스트할 활성 vCenter가 없습니다.")
            return jsonify(PowerCLICollector(current_cfg).test_one(entry))
        except Exception as exc:
            return jsonify({"status": "FAILED", "stage": "SERVER", "error": str(exc)}), 500

    @bp.route("/api/asset-sync/admin/test/vcenter/all", methods=["POST"])
    @bp.route("/api/asset-sync/admin/test/rvtools/all", methods=["POST"])
    @admin_required
    def test_all_vcenters() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            submitted = payload.get("entries")
            if isinstance(submitted, list):
                entries = [dict(item) for item in submitted if isinstance(item, dict) and bool(item.get("enabled", True))]
                if not entries:
                    return jsonify({"status": "FAILED", "error": "테스트할 활성 vCenter가 없습니다."}), 400
                test_cfg, _ = _merge_vcenter_test_config(payload, entries)
                return jsonify(PowerCLICollector(test_cfg).test_all())

            current_cfg = load_config()
            if str(current_cfg.rvtools.get("collection_mode", "POWERCLI")).upper() != "POWERCLI":
                return jsonify({"status": "FAILED", "error": "전체 vCenter 테스트는 POWERCLI 모드에서만 실행합니다."}), 400
            return jsonify(PowerCLICollector(current_cfg).test_all())
        except Exception as exc:
            return jsonify({"status": "FAILED", "error": str(exc)}), 500

    @bp.route("/api/admin/data-quality-rules", methods=["GET", "POST", "PUT"])
    @admin_required
    def quality_rules() -> Any:
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            if request.method == "GET":
                return jsonify([dict(row) for row in conn.execute("SELECT * FROM data_quality_rule ORDER BY id").fetchall()])
            payload = request.get_json() or {}
            user = str(session["user"].get("id") or session["user"].get("username"))
            if request.method == "POST":
                cur = conn.execute(
                    "INSERT INTO data_quality_rule(rule_name, field_name, rule_type, severity, enabled, config_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        payload["rule_name"], payload["field_name"], payload["rule_type"],
                        payload.get("severity", "WARNING"), int(payload.get("enabled", 1)),
                        json.dumps(payload.get("config", {}), ensure_ascii=False),
                    ),
                )
                repo.audit(user, "CREATE", "data_quality_rule", str(cur.lastrowid), payload.get("reason"), {}, payload)
                return jsonify({"success": True, "id": cur.lastrowid})
            rule_id = int(payload["id"])
            before = conn.execute("SELECT * FROM data_quality_rule WHERE id=?", (rule_id,)).fetchone()
            conn.execute(
                "UPDATE data_quality_rule SET rule_name=?, field_name=?, rule_type=?, severity=?, enabled=?, config_json=? WHERE id=?",
                (
                    payload["rule_name"], payload["field_name"], payload["rule_type"],
                    payload.get("severity", "WARNING"), int(payload.get("enabled", 1)),
                    json.dumps(payload.get("config", {}), ensure_ascii=False), rule_id,
                ),
            )
            repo.audit(user, "UPDATE", "data_quality_rule", str(rule_id), payload.get("reason"), dict(before) if before else {}, payload)
            return jsonify({"success": True})

    @bp.route("/api/admin/manual-overrides", methods=["GET", "POST", "PUT"])
    @admin_required
    def manual_overrides() -> Any:
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            if request.method == "GET":
                return jsonify([dict(row) for row in conn.execute("SELECT * FROM manual_asset_override ORDER BY created_at DESC").fetchall()])
            payload = request.get_json() or {}
            user = str(session["user"].get("id") or session["user"].get("username"))
            if request.method == "POST":
                cur = conn.execute(
                    "INSERT INTO manual_asset_override(cm_id, field_name, override_value, reason, approval_status, valid_from, valid_to, created_by, created_at) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, datetime('now'))",
                    (
                        payload["cm_id"], payload["field_name"], payload.get("override_value"),
                        payload["reason"], payload.get("valid_from"), payload.get("valid_to"), user,
                    ),
                )
                repo.audit(user, "CREATE", "manual_asset_override", str(cur.lastrowid), payload["reason"], {}, payload)
                return jsonify({"success": True, "id": cur.lastrowid})
            override_id = int(payload["id"])
            before = conn.execute("SELECT * FROM manual_asset_override WHERE id=?", (override_id,)).fetchone()
            conn.execute(
                "UPDATE manual_asset_override SET override_value=?, reason=?, valid_from=?, valid_to=? WHERE id=? AND approval_status IN ('DRAFT','REJECTED')",
                (payload.get("override_value"), payload["reason"], payload.get("valid_from"), payload.get("valid_to"), override_id),
            )
            repo.audit(user, "UPDATE", "manual_asset_override", str(override_id), payload["reason"], dict(before) if before else {}, payload)
            return jsonify({"success": True})

    def approve(override_id: int, status: str) -> Any:
        payload = request.get_json(silent=True) or {}
        user = str(session["user"].get("id") or session["user"].get("username"))
        with manager.connect() as conn:
            repo = AssetRepository(conn)
            before = conn.execute("SELECT * FROM manual_asset_override WHERE id=?", (override_id,)).fetchone()
            if not before:
                return jsonify({"error": "대상이 없습니다."}), 404
            conn.execute(
                "UPDATE manual_asset_override SET approval_status=?, approved_by=?, approved_at=datetime('now') WHERE id=?",
                (status, user, override_id),
            )
            repo.audit(user, status, "manual_asset_override", str(override_id), payload.get("reason"), dict(before), {"approval_status": status})
        return jsonify({"success": True})

    @bp.route("/api/admin/manual-overrides/<int:override_id>/approve", methods=["POST"])
    @admin_required
    def approve_override(override_id: int) -> Any:
        return approve(override_id, "APPROVED")

    @bp.route("/api/admin/manual-overrides/<int:override_id>/reject", methods=["POST"])
    @admin_required
    def reject_override(override_id: int) -> Any:
        return approve(override_id, "REJECTED")

    @bp.route("/api/admin/audit-log")
    @admin_required
    def audit_log() -> Any:
        with manager.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?",
                (min(int(request.args.get("limit", 1000)), 10000),),
            ).fetchall()
            return jsonify([dict(row) for row in rows])

    return bp
