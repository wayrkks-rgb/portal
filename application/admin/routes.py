from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from application.accounts import AccountError, UserRepository
from application.common import load_json, require_admin, require_login, save_json
from application.db import database_manager
from application.permissions import (
    ModulePermissionRepository,
    PermissionError_,
    resolve_permission,
)
from asset_sync.repositories import AssetRepository
from application.settings import MAPPINGS_FILE

bp = Blueprint("admin", __name__)

@bp.route("/admin")
@require_login
def admin():
    if session['user']['role'] != 'admin':
        return redirect(url_for('auth.dashboard'))
    # page 값은 화면 id 와 같아야 한다. 'admin' 은 대응하는 화면이 없어 빈 화면이 떴다.
    return render_template('main.html', user=session['user'], page='admin_mapping')

@bp.route("/api/mappings", methods=["GET"])
@require_login
def get_mappings():
    return jsonify(load_json(MAPPINGS_FILE))

@bp.route("/api/mappings", methods=["POST"])
@require_admin
def save_mappings():
    data = request.get_json()
    save_json(MAPPINGS_FILE, data)
    return jsonify({'success': True})

@bp.route("/api/mappings/menu", methods=["POST"])
@require_admin
def add_menu():
    data = request.get_json()
    mappings = load_json(MAPPINGS_FILE)
    new_menu = {
        'id': data.get('id', f'menu_{datetime.now().strftime("%Y%m%d%H%M%S")}'),
        'name': data.get('name', '새 메뉴'),
        'asis_col_vm': data.get('asis_col_vm', 'A'),
        'asis_col_cpu_max': data.get('asis_col_cpu_max', 'B'),
        'asis_col_cpu_avg': data.get('asis_col_cpu_avg', 'C'),
        'asis_col_mem_max': data.get('asis_col_mem_max', 'D'),
        'asis_col_mem_avg': data.get('asis_col_mem_avg', 'E'),
        'asis_header_row': int(data.get('asis_header_row', 1)),
        'tobe_col_vm': data.get('tobe_col_vm', 'B'),
        'tobe_col_cpu_max': data.get('tobe_col_cpu_max', 'E'),
        'tobe_col_cpu_avg': data.get('tobe_col_cpu_avg', 'F'),
        'tobe_col_mem_max': data.get('tobe_col_mem_max', 'G'),
        'tobe_col_mem_avg': data.get('tobe_col_mem_avg', 'H'),
        'tobe_data_start_row': int(data.get('tobe_data_start_row', 7)),
        'tobe_count_row': int(data.get('tobe_count_row', 3)),
        'sheet_vm_mappings': []
    }
    mappings['menus'].append(new_menu)
    save_json(MAPPINGS_FILE, mappings)
    return jsonify({'success': True, 'menu': new_menu})

@bp.route("/api/mappings/menu/<menu_id>", methods=["PUT"])
@require_admin
def update_menu(menu_id):
    data = request.get_json()
    mappings = load_json(MAPPINGS_FILE)
    for i, m in enumerate(mappings['menus']):
        if m['id'] == menu_id:
            mappings['menus'][i].update(data)
            break
    save_json(MAPPINGS_FILE, mappings)
    return jsonify({'success': True})

@bp.route("/api/mappings/menu/<menu_id>", methods=["DELETE"])
@require_admin
def delete_menu(menu_id):
    mappings = load_json(MAPPINGS_FILE)
    mappings['menus'] = [m for m in mappings['menus'] if m['id'] != menu_id]
    save_json(MAPPINGS_FILE, mappings)
    return jsonify({'success': True})

@bp.route("/api/mappings/sheet-vm", methods=["POST"])
@require_admin
def save_sheet_vm_mapping():
    data = request.get_json()
    menu_id = data.get('menu_id')
    sheet_name = data.get('sheet_name')
    vm_list = data.get('vm_list', [])

    mappings = load_json(MAPPINGS_FILE)
    for menu in mappings['menus']:
        if menu['id'] == menu_id:
            existing = next((s for s in menu['sheet_vm_mappings'] if s['sheet_name'] == sheet_name), None)
            if existing:
                existing['vm_list'] = vm_list
            else:
                menu['sheet_vm_mappings'].append({'sheet_name': sheet_name, 'vm_list': vm_list})
            break
    save_json(MAPPINGS_FILE, mappings)
    return jsonify({'success': True})

def _audit_account(conn, action: str, target_id: str, before, after) -> None:
    """Account changes are security relevant, so they go to the shared audit log."""
    AssetRepository(conn).audit(
        str(session['user'].get('username') or session['user'].get('id')),
        action, 'app_user', target_id, None, before or {}, after or {},
    )


@bp.route("/api/users", methods=["GET"])
@require_admin
def get_users():
    with database_manager().connect() as conn:
        return jsonify(UserRepository(conn).list_users())

@bp.route("/api/users", methods=["POST"])
@require_admin
def add_user():
    data = request.get_json(silent=True) or {}
    try:
        with database_manager().connect() as conn:
            repo = UserRepository(conn)
            user_id = repo.create(
                username=data.get('username'),
                password=data.get('password'),
                name=data.get('name') or '',
                role=data.get('role') or 'user',
            )
            created = repo.find_by_id(user_id) or {}
            _audit_account(conn, 'CREATE', str(user_id), {}, {k: created.get(k) for k in ('username', 'name', 'role')})
        return jsonify({'success': True, 'id': user_id})
    except AccountError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

@bp.route("/api/users/<int:uid>", methods=["PUT"])
@require_admin
def update_user(uid):
    data = request.get_json(silent=True) or {}
    try:
        with database_manager().connect() as conn:
            repo = UserRepository(conn)
            before = repo.find_by_id(uid)
            if not before:
                return jsonify({'success': False, 'error': '대상 계정을 찾을 수 없습니다.'}), 404
            updated = repo.update(
                uid,
                name=data.get('name'),
                role=data.get('role'),
                enabled=data.get('enabled'),
                password=data.get('password') or None,
            )
            _audit_account(
                conn, 'UPDATE', str(uid),
                {k: before.get(k) for k in ('name', 'role', 'enabled')},
                {**{k: updated.get(k) for k in ('name', 'role', 'enabled')},
                 'password_changed': bool(data.get('password'))},
            )
        return jsonify({'success': True, 'user': updated})
    except AccountError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

@bp.route("/api/users/<int:uid>/modules", methods=["GET", "PUT"])
@require_admin
def user_modules(uid):
    """대메뉴별 권한 조회/부여. 등록된 모듈만 대상으로 한다."""
    from flask import current_app

    registry = current_app.extensions.get("module_registry")
    known = [module.id for module in registry.all()] if registry else []
    try:
        with database_manager().connect() as conn:
            users = UserRepository(conn)
            target = users.find_by_id(uid)
            if not target:
                return jsonify({"success": False, "error": "대상 계정을 찾을 수 없습니다."}), 404
            permissions = ModulePermissionRepository(conn)
            if request.method == "GET":
                granted = permissions.for_user(uid)
                modules = []
                for module in (registry.all() if registry else []):
                    modules.append({
                        **module.public(),
                        "granted": granted.get(module.id),
                        "effective": resolve_permission(module, target, granted),
                    })
                return jsonify({"user_id": uid, "username": target["username"], "modules": modules})

            payload = request.get_json(silent=True) or {}
            before = permissions.for_user(uid)
            permissions.replace_for_user(
                uid,
                payload.get("permissions") or {},
                granted_by=str(session["user"].get("username") or ""),
                known_modules=known,
            )
            after = permissions.for_user(uid)
            _audit_account(conn, "PERMISSION", str(uid), before, after)
        return jsonify({"success": True, "permissions": after})
    except (AccountError, PermissionError_) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@bp.route("/api/users/<int:uid>", methods=["DELETE"])
@require_admin
def delete_user(uid):
    try:
        with database_manager().connect() as conn:
            ModulePermissionRepository(conn).delete_for_user(uid)
            removed = UserRepository(conn).delete(uid)
            _audit_account(conn, 'DELETE', str(uid), {k: removed.get(k) for k in ('username', 'name', 'role')}, {})
        return jsonify({'success': True})
    except AccountError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

