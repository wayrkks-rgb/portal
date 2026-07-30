"""통합 웹과 도메인 WAS 사이의 서명 토큰.

통합 웹이 세션으로 사용자를 인증하고, WAS 를 호출할 때 이 토큰을 헤더에 붙인다.
WAS 는 세션 저장소를 공유하지 않고도 "누가 요청했는지"와 "정당한 호출인지"를
확인할 수 있다.

폐쇄망이라는 전제에서 공유 시크릿 HMAC-SHA256 으로 충분하고, ``hmac``/``hashlib``
은 표준 라이브러리라 추가 반입물이 없다.

각 WAS 쪽 검증 코드는 이 파일의 ``verify_token`` 을 그대로 옮겨 쓰면 된다.
의존성이 없도록 일부러 자기완결적으로 작성했다.

    payload = verify_token(request.headers.get("X-Portal-Token"), secret)
    if payload is None:
        return jsonify({"error": "unauthorized"}), 401
    user = payload["sub"]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Mapping

TOKEN_VERSION = 1
DEFAULT_TTL_SECONDS = 60
# 시계 오차 허용치. 폐쇄망에서도 NTP 가 어긋난 서버가 흔하다.
CLOCK_SKEW_SECONDS = 30


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def issue_token(
    secret: str,
    *,
    user: Mapping[str, Any],
    module_id: str,
    permission: str = "VIEW",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """호출 1건에 쓰이는 단기 토큰을 만든다.

    ``mod`` 를 넣기 때문에 A 모듈에 발급된 토큰을 B 모듈에 재사용할 수 없다.
    ``perm`` 은 그 모듈에서의 실효 권한이라 WAS 가 쓰기 요청을 자체 판단할 수 있다.
    """
    if not secret:
        raise ValueError("모듈 공유 시크릿이 설정되지 않았습니다.")
    issued_at = int(now if now is not None else time.time())
    payload = {
        "v": TOKEN_VERSION,
        "sub": str(user.get("username") or ""),
        "uid": user.get("id"),
        "role": str(user.get("role") or "user"),
        "name": str(user.get("name") or ""),
        "mod": str(module_id),
        "perm": str(permission or "VIEW").upper(),
        "iat": issued_at,
        "exp": issued_at + max(1, int(ttl_seconds)),
        "jti": secrets.token_hex(8),
    }
    payload_b64 = _b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_token(
    token: str | None,
    secret: str,
    *,
    module_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """검증에 성공하면 payload, 실패하면 None 을 돌려준다.

    실패 이유를 구분해서 돌려주지 않는 것은 의도적이다. 호출자에게 알려줄수록
    토큰을 맞춰보기 쉬워진다.
    """
    if not token or not secret:
        return None
    raw = str(token).strip()
    if raw.count(".") != 1:
        return None
    payload_b64, signature = raw.split(".", 1)
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        return None
    current = int(now if now is not None else time.time())
    try:
        expires_at = int(payload.get("exp", 0))
        issued_at = int(payload.get("iat", 0))
    except (TypeError, ValueError):
        return None
    if current > expires_at + CLOCK_SKEW_SECONDS:
        return None
    if issued_at - CLOCK_SKEW_SECONDS > current:
        return None
    if module_id is not None and str(payload.get("mod")) != str(module_id):
        return None
    return payload


def mask_token(token: str | None) -> str:
    """로그에 남길 형태. 서명 전체를 남기면 재사용될 수 있다."""
    if not token:
        return "-"
    raw = str(token)
    return f"{raw[:8]}***{raw[-4:]}" if len(raw) > 16 else "***"
