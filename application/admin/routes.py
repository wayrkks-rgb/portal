from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from application.common import load_json, require_admin, require_login, save_json
from application.settings import MAPPINGS_FILE, USERS_FILE

bp = Blueprint("admin", __name__)

@bp.route("/admin")
@require_login
def admin():
    if session['user']['role'] != 'admin':
        return redirect(url_for('auth.dashboard'))
    return render_template('main.html', user=session['user'], page='admin')

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

@bp.route("/api/users", methods=["GET"])
@require_admin
def get_users():
    users = load_json(USERS_FILE)
    return jsonify([{k: v for k, v in u.items() if k != 'password'} for u in users])

@bp.route("/api/users", methods=["POST"])
@require_admin
def add_user():
    data = request.get_json()
    users = load_json(USERS_FILE)
    new_id = max((u['id'] for u in users), default=0) + 1
    users.append({
        'id': new_id,
        'username': data['username'],
        'password': generate_password_hash(data['password']),
        'role': data.get('role', 'user'),
        'name': data.get('name', data['username'])
    })
    save_json(USERS_FILE, users)
    return jsonify({'success': True})

@bp.route("/api/users/<int:uid>", methods=["DELETE"])
@require_admin
def delete_user(uid):
    users = load_json(USERS_FILE)
    users = [u for u in users if u['id'] != uid]
    save_json(USERS_FILE, users)
    return jsonify({'success': True})

