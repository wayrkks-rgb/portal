from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from application.common import load_json, require_login, save_json
from application.settings import USERS_FILE

bp = Blueprint("auth", __name__)

@bp.route("/")
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('auth.dashboard'))

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == 'POST':
        data = request.get_json()
        users = load_json(USERS_FILE)
        user = None
        for candidate in users:
            if candidate.get('username') != data.get('username'):
                continue
            stored = str(candidate.get('password', ''))
            supplied = str(data.get('password', ''))
            try:
                valid = check_password_hash(stored, supplied) if ':' in stored else stored == supplied
            except ValueError:
                valid = stored == supplied
            if valid:
                user = candidate
                if ':' not in stored:
                    candidate['password'] = generate_password_hash(supplied)
                    save_json(USERS_FILE, users)
                break
        if user:
            session['user'] = {k: v for k, v in user.items() if k != 'password'}
            return jsonify({'success': True, 'role': user['role']})
        return jsonify({'success': False, 'message': '아이디 또는 비밀번호가 올바르지 않습니다.'})
    return render_template('login.html')

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('auth.login'))

@bp.route("/dashboard")
@require_login
def dashboard():
    return render_template('main.html', user=session['user'], page='dashboard')

