from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import jsonify, session

F = TypeVar("F", bound=Callable[..., Any])


def login_required(func: F) -> F:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "user" not in session:
            return jsonify({"error": "로그인이 필요합니다."}), 401
        return func(*args, **kwargs)
    return wrapper  # type: ignore[return-value]


def admin_required(func: F) -> F:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "user" not in session:
            return jsonify({"error": "로그인이 필요합니다."}), 401
        if session["user"].get("role") != "admin":
            return jsonify({"error": "관리자 권한이 필요합니다."}), 403
        return func(*args, **kwargs)
    return wrapper  # type: ignore[return-value]
