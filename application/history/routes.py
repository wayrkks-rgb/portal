from __future__ import annotations

from flask import Blueprint, jsonify, render_template, session

from application.common import load_json, require_login
from application.settings import HISTORY_FILE

bp = Blueprint("history", __name__)

@bp.route("/history")
@require_login
def history():
    return render_template('main.html', user=session['user'], page='history')

@bp.route("/api/history")
@require_login
def get_history():
    history = load_json(HISTORY_FILE)
    return jsonify(sorted(history, key=lambda x: x['uploaded_at'], reverse=True))

