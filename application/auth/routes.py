from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from application.accounts import AccountError, UserRepository
from application.common import require_login
from application.db import database_manager

bp = Blueprint("auth", __name__)

@bp.route("/")
def index():
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return redirect(url_for('auth.dashboard'))

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        try:
            with database_manager().connect() as conn:
                user = UserRepository(conn).verify(data.get('username'), data.get('password'))
        except AccountError as exc:
            return jsonify({'success': False, 'message': str(exc)}), 403
        if user:
            session['user'] = user
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

