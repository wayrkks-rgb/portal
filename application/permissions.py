"""대메뉴별 사용자 권한.

대메뉴마다 담당자가 다르므로 ``admin``/``user`` 두 단계로는 "누가 어느 대메뉴를
보는가"를 표현할 수 없다. ``user_module_permission`` 에 사용자-모듈 단위로 권한을
부여한다.

판정 규칙은 세 줄이다.

1. ``admin`` 은 모든 모듈에 MANAGE 권한을 갖는다. 그러지 않으면 관리자에게도
   대메뉴를 하나하나 부여해야 하고, 부여를 잊으면 관리 화면조차 못 들어간다.
2. 명시 부여가 있으면 그 값을 쓴다.
3. 없으면 모듈의 ``access`` 설정으로 판정한다.
   - ``role`` (기본): ``required_role`` 로 판정 — 기존 동작과 같다.
   - ``explicit``: 명시 부여가 없으면 접근 불가.

기본값을 ``role`` 로 둔 이유는 이 테이블을 도입해도 기존 계정이 갑자기 모든
대메뉴를 잃지 않게 하기 위한 것이다. 엄격하게 통제할 대메뉴만 ``explicit`` 로
바꾸면 된다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Mapping

LOGGER = logging.getLogger(__name__)

PERMISSION_NONE = "NONE"
PERMISSION_VIEW = "VIEW"
PERMISSION_MANAGE = "MANAGE"

_RANK = {PERMISSION_NONE: 0, PERMISSION_VIEW: 1, PERMISSION_MANAGE: 2}
GRANTABLE = (PERMISSION_VIEW, PERMISSION_MANAGE)


class PermissionError_(ValueError):
    """권한 설정 오류. 내장 PermissionError 와 구분하기 위해 이름 뒤에 밑줄을 둔다."""


def normalize_permission(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in _RANK:
        raise PermissionError_(f"권한 값은 {', '.join(GRANTABLE)} 중 하나여야 합니다: {value!r}")
    return normalized


def at_least(actual: str, required: str) -> bool:
    return _RANK.get(str(actual).upper(), 0) >= _RANK.get(str(required).upper(), 0)


class ModulePermissionRepository:
    """``user_module_permission`` CRUD."""

    def __init__(self, connection: Any) -> None:
        self.conn = connection

    def for_user(self, user_id: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT module_id, permission FROM user_module_permission WHERE user_id=?",
            (int(user_id),),
        ).fetchall()
        return {str(row["module_id"]): str(row["permission"]).upper() for row in rows}

    def detail_for_user(self, user_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT module_id, permission, granted_by, granted_at
            FROM user_module_permission WHERE user_id=? ORDER BY module_id
            """,
            (int(user_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def users_for_module(self, module_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.user_id, p.permission, u.username, u.name, u.enabled
            FROM user_module_permission p
            JOIN app_user u ON u.id = p.user_id
            WHERE p.module_id=?
            ORDER BY u.username
            """,
            (str(module_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def grant(self, user_id: int, module_id: str, permission: str, granted_by: str = "") -> str:
        level = normalize_permission(permission)
        if level == PERMISSION_NONE:
            self.revoke(user_id, module_id)
            return PERMISSION_NONE
        module = str(module_id or "").strip().lower()
        if not module:
            raise PermissionError_("모듈 id 가 비어 있습니다.")
        now = datetime.now().isoformat()
        existing = self.conn.execute(
            "SELECT id FROM user_module_permission WHERE user_id=? AND module_id=?",
            (int(user_id), module),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE user_module_permission SET permission=?, granted_by=?, granted_at=? WHERE id=?",
                (level, granted_by, now, int(existing["id"])),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO user_module_permission(user_id, module_id, permission, granted_by, granted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(user_id), module, level, granted_by, now),
            )
        return level

    def revoke(self, user_id: int, module_id: str) -> None:
        self.conn.execute(
            "DELETE FROM user_module_permission WHERE user_id=? AND module_id=?",
            (int(user_id), str(module_id or "").strip().lower()),
        )

    def replace_for_user(
        self,
        user_id: int,
        permissions: Mapping[str, Any],
        *,
        granted_by: str = "",
        known_modules: Iterable[str] | None = None,
    ) -> dict[str, str]:
        """화면에서 넘어온 모듈-권한 묶음을 그대로 반영한다.

        등록되지 않은 모듈 id 는 거부한다. 오타로 만들어진 권한 행이 남아 있으면
        나중에 그 id 로 모듈을 등록했을 때 의도치 않게 권한이 살아난다.
        """
        allowed = {str(item).strip().lower() for item in (known_modules or [])}
        result: dict[str, str] = {}
        for module_id, permission in permissions.items():
            module = str(module_id).strip().lower()
            if allowed and module not in allowed:
                raise PermissionError_(f"등록되지 않은 모듈입니다: {module_id}")
            level = normalize_permission(permission) if str(permission or "").strip() else PERMISSION_NONE
            if level == PERMISSION_NONE:
                self.revoke(user_id, module)
                continue
            self.grant(user_id, module, level, granted_by)
            result[module] = level
        return result

    def delete_for_user(self, user_id: int) -> None:
        """계정 삭제 시 함께 지운다. SQLite 는 외래키가 켜져 있어야 자동 삭제된다."""
        self.conn.execute("DELETE FROM user_module_permission WHERE user_id=?", (int(user_id),))


def resolve_permission(
    module: Any,
    user: Mapping[str, Any],
    granted: Mapping[str, str] | None = None,
) -> str:
    """한 모듈에 대한 사용자의 실효 권한."""
    if not getattr(module, "enabled", True):
        return PERMISSION_NONE
    role = str(user.get("role") or "user").lower()
    if role == "admin":
        return PERMISSION_MANAGE
    explicit = (granted or {}).get(getattr(module, "id", ""))
    if explicit:
        return str(explicit).upper()
    if str(getattr(module, "access", "role")).lower() == "explicit":
        return PERMISSION_NONE
    # role 모드: 모듈이 요구하는 등급을 만족하면 VIEW 를 준다.
    return PERMISSION_VIEW if module.visible_to(role) else PERMISSION_NONE


def user_context(user: Mapping[str, Any], granted: Mapping[str, str] | None = None) -> dict[str, Any]:
    """레지스트리·클라이언트에 함께 넘기는 사용자 컨텍스트."""
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "name": user.get("name"),
        "role": str(user.get("role") or "user"),
        "modules": dict(granted or {}),
    }
