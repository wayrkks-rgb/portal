from __future__ import annotations

import json
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

import openpyxl
from flask import jsonify, redirect, session, url_for

F = TypeVar("F", bound=Callable[..., Any])


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def save_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def col_letter_to_idx(letter: str) -> int:
    return openpyxl.utils.column_index_from_string(letter) - 1


def require_login(func: F) -> F:
    @wraps(func)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if "user" not in session:
            return redirect(url_for("auth.login"))
        return func(*args, **kwargs)
    return decorated  # type: ignore[return-value]


def require_admin(func: F) -> F:
    @wraps(func)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if "user" not in session:
            return redirect(url_for("auth.login"))
        if session["user"].get("role") != "admin":
            return jsonify({"error": "관리자 권한이 필요합니다."}), 403
        return func(*args, **kwargs)
    return decorated  # type: ignore[return-value]
