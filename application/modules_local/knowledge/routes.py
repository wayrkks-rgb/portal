"""지식 저장소 화면과 API. 통합 웹이 기동할 때 ``bp`` 를 자동으로 등록한다.

권한은 대메뉴 권한을 그대로 따른다. ``config/modules/knowledge.yaml`` 의 ``access`` 를
``explicit`` 으로 바꾸면 관리자가 부여한 사람만 화면과 API 를 쓸 수 있다. 화면에서
메뉴를 감추는 것만으로는 API 가 열려 있으므로 여기서도 같은 판정을 한다.
"""

from __future__ import annotations

import logging
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session

from application.common import require_login
from application.db import database_manager

from .parser import ParseError, file_type_of, parse_file
from .store import KnowledgeError, KnowledgeStore, is_admin

LOGGER = logging.getLogger(__name__)
MODULE_ID = "knowledge"

bp = Blueprint("knowledge", __name__)


def files_dir() -> Path:
    """원본 파일을 두는 곳. 설정 루트 아래 ``data/knowledge/files``."""
    cached = current_app.extensions.get("knowledge_files_dir")
    if cached is None:
        from asset_sync.config import load_config

        cached = load_config().resolve("data/knowledge/files")
        current_app.extensions["knowledge_files_dir"] = cached
    return Path(cached)


def _user() -> dict[str, Any]:
    return dict(session.get("user") or {})


def _has_access(user: dict[str, Any]) -> bool:
    registry = current_app.extensions.get("module_registry")
    if registry is None or registry.get(MODULE_ID) is None:
        # 대메뉴 정의가 꺼져 있으면 API 도 닫는다.
        return False
    from application.modules.routes import granted_permissions

    return any(module.id == MODULE_ID for module, _ in registry.accessible(user, granted_permissions(user)))


def api(func: Callable[..., Any]) -> Callable[..., Any]:
    """로그인 · 메뉴 권한을 확인하고, 거절은 JSON 으로 돌려준다."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        user = _user()
        if not user:
            return jsonify({"status": "FAILED", "error": "로그인이 필요합니다."}), 401
        if not _has_access(user):
            return jsonify({"status": "FAILED", "error": "지식 저장소 권한이 없습니다."}), 403
        try:
            return func(user, *args, **kwargs)
        except (KnowledgeError, ParseError) as exc:
            return jsonify({"status": "FAILED", "error": str(exc)}), getattr(exc, "status", 400)

    return wrapper


def _store(conn: Any) -> KnowledgeStore:
    return KnowledgeStore(conn, files_dir())


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@bp.route("/knowledge")
@require_login
def page() -> Any:
    return render_template("main.html", user=session["user"], page="knowledge")


@bp.route("/api/knowledge/summary")
@api
def summary(user: dict[str, Any]) -> Any:
    with database_manager().connect() as conn:
        body = _store(conn).summary()
    return jsonify({"status": "SUCCESS", **body, "is_admin": is_admin(user)})


@bp.route("/api/knowledge/datasets")
@api
def datasets(user: dict[str, Any]) -> Any:
    with database_manager().connect() as conn:
        store = _store(conn)
        items = store.datasets(category=request.args.get("category", ""), query=request.args.get("q", ""))
        categories = store.categories()
    return jsonify({"status": "SUCCESS", "datasets": items, "categories": categories})


@bp.route("/api/knowledge/datasets", methods=["POST"])
@api
def upload(user: dict[str, Any]) -> Any:
    incoming = request.files.get("file")
    if incoming is None or not incoming.filename:
        raise KnowledgeError("올릴 파일을 골라 주세요.")
    filename = Path(incoming.filename.replace("\\", "/")).name
    form = request.form
    dataset_id = _int(form.get("dataset_id"), 0) or None

    # 받은 파일은 임시 이름으로 두고 읽는다. 사용자 파일 이름을 경로에 쓰지 않는다.
    temp_dir = files_dir() / "_incoming"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp = temp_dir / f"{uuid.uuid4().hex}.{file_type_of(filename) or 'bin'}"
    incoming.save(temp)
    try:
        if temp.stat().st_size == 0:
            raise KnowledgeError("빈 파일입니다.")
        parsed = parse_file(temp, filename, _int(form.get("header_row"), 0) or None)
        meta = {name: form.get(name, "") for name in ("title", "category", "description", "source_note", "tags")}
        if not meta["title"] and dataset_id is None:
            meta["title"] = Path(filename).stem
        with database_manager().connect() as conn:
            result = _store(conn).save_upload(
                temp, filename, parsed, meta, user, dataset_id=dataset_id, note=form.get("note", ""),
            )
    finally:
        try:
            temp.unlink()
        except OSError:
            LOGGER.warning("임시 파일을 지우지 못했습니다: %s", temp)
    LOGGER.info("지식 저장소 올리기: %s · 자료 %s · 버전 %s · %s행 · %s",
                filename, result["dataset_id"], result["version_no"], result["row_count"], user.get("username"))
    return jsonify({"status": "SUCCESS", **result})


@bp.route("/api/knowledge/datasets/<int:dataset_id>")
@api
def detail(user: dict[str, Any], dataset_id: int) -> Any:
    version_id = _int(request.args.get("version"), 0)
    with database_manager().connect() as conn:
        store = _store(conn)
        body = store.detail(dataset_id, user)
        chosen = version_id or int(body["dataset"].get("current_version_id") or 0)
        if chosen and not any(int(v["id"]) == chosen for v in body["versions"]):
            raise KnowledgeError("이 자료의 버전이 아닙니다.", 404)
        body["version_id"] = chosen
        body["sheets"] = store.sheets(chosen) if chosen else []
    return jsonify({"status": "SUCCESS", **body})


@bp.route("/api/knowledge/datasets/<int:dataset_id>", methods=["PATCH"])
@api
def edit(user: dict[str, Any], dataset_id: int) -> Any:
    with database_manager().connect() as conn:
        dataset = _store(conn).update_meta(dataset_id, request.get_json(silent=True) or {}, user)
    return jsonify({"status": "SUCCESS", "dataset": dataset})


@bp.route("/api/knowledge/datasets/<int:dataset_id>", methods=["DELETE"])
@api
def remove(user: dict[str, Any], dataset_id: int) -> Any:
    with database_manager().connect() as conn:
        _store(conn).delete(dataset_id, user)
    return jsonify({"status": "SUCCESS"})


@bp.route("/api/knowledge/sheets/<int:sheet_id>/rows")
@api
def rows(user: dict[str, Any], sheet_id: int) -> Any:
    with database_manager().connect() as conn:
        body = _store(conn).rows(
            sheet_id,
            query=request.args.get("q", ""),
            offset=_int(request.args.get("offset"), 0),
            limit=_int(request.args.get("limit"), 50),
        )
    return jsonify({"status": "SUCCESS", **body})


@bp.route("/api/knowledge/search")
@api
def search(user: dict[str, Any]) -> Any:
    with database_manager().connect() as conn:
        body = _store(conn).search(request.args.get("q", ""), category=request.args.get("category", ""))
    return jsonify({"status": "SUCCESS", **body})


@bp.route("/api/knowledge/versions/<int:version_id>/download")
@api
def download(user: dict[str, Any], version_id: int) -> Any:
    with database_manager().connect() as conn:
        path, filename = _store(conn).download(version_id, user)
    return send_file(path, as_attachment=True, download_name=filename)


@bp.route("/api/knowledge/events")
@api
def events(user: dict[str, Any]) -> Any:
    if not is_admin(user):
        raise KnowledgeError("관리자만 볼 수 있습니다.", 403)
    with database_manager().connect() as conn:
        items = _store(conn).events(_int(request.args.get("limit"), 100))
    return jsonify({"status": "SUCCESS", "events": items})
