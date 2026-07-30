"""Login accounts stored in the shared database.

Accounts used to live in ``data/users.json``, which is local to one host. Once the
integrated web front and the domain WAS modules share one database, a file-based
store means each process sees a different account list: a password changed on one
host does not apply on another.

Passwords are stored as Werkzeug hashes only. Values imported from the legacy JSON
file are hashed during the import, so no plaintext ever reaches the table.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

LOGGER = logging.getLogger(__name__)

PUBLIC_FIELDS = (
    "id", "username", "name", "role", "enabled",
    "created_at", "updated_at", "last_login_at", "password_updated_at",
)
VALID_ROLES = ("admin", "user")
MIN_PASSWORD_LENGTH = 6

# Kept identical to the previous JSON seed so existing operators can still log in.
DEFAULT_USERS = (
    {"username": "admin", "password": "admin123", "role": "admin", "name": "관리자"},
    {"username": "user", "password": "user123", "role": "user", "name": "일반사용자"},
)


class AccountError(ValueError):
    pass


def _public(row: Any) -> dict[str, Any]:
    data = dict(row)
    result = {key: data.get(key) for key in PUBLIC_FIELDS}
    result["enabled"] = bool(data.get("enabled", 1))
    return result


def _validate_role(role: str) -> str:
    normalized = str(role or "user").strip().lower()
    if normalized not in VALID_ROLES:
        raise AccountError(f"권한은 {', '.join(VALID_ROLES)} 중 하나여야 합니다: {role!r}")
    return normalized


def _validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    return value


def _validate_username(username: str) -> str:
    value = str(username or "").strip()
    if not value:
        raise AccountError("아이디를 입력하세요.")
    if len(value) > 64:
        raise AccountError("아이디는 64자 이내여야 합니다.")
    return value


class UserRepository:
    """CRUD over ``app_user``, guarding against locking every admin out."""

    def __init__(self, connection: Any) -> None:
        self.conn = connection

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM app_user ORDER BY id").fetchall()
        return [_public(row) for row in rows]

    def find_by_username(self, username: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM app_user WHERE username=?", (str(username or "").strip(),)
        ).fetchone()
        return dict(row) if row else None

    def find_by_id(self, user_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM app_user WHERE id=?", (int(user_id),)).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM app_user").fetchone()
        return int(row["cnt"]) if row else 0

    def _active_admin_ids(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM app_user WHERE role='admin' AND enabled=1 ORDER BY id"
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _guard_last_admin(self, user_id: int, *, action: str) -> None:
        """Refuse a change that would leave the portal with no way in."""
        admins = self._active_admin_ids()
        if admins == [int(user_id)]:
            raise AccountError(f"마지막 관리자 계정은 {action}할 수 없습니다. 다른 관리자를 먼저 등록하세요.")

    def create(self, *, username: str, password: str, name: str = "", role: str = "user") -> int:
        username = _validate_username(username)
        password = _validate_password(password)
        role = _validate_role(role)
        if self.find_by_username(username):
            raise AccountError(f"이미 존재하는 아이디입니다: {username}")
        now = datetime.now().isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO app_user(username, password_hash, name, role, enabled, created_at, password_updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (username, generate_password_hash(password), str(name).strip() or username, role, now, now),
        )
        return int(cursor.lastrowid)

    def update(
        self,
        user_id: int,
        *,
        name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        current = self.find_by_id(user_id)
        if not current:
            raise AccountError("대상 계정을 찾을 수 없습니다.")
        if role is not None and _validate_role(role) != "admin" and current["role"] == "admin":
            self._guard_last_admin(user_id, action="권한 변경")
        if enabled is not None and not enabled and current["role"] == "admin":
            self._guard_last_admin(user_id, action="비활성화")

        assignments: list[str] = []
        params: list[Any] = []
        now = datetime.now().isoformat()
        if name is not None:
            assignments.append("name=?")
            params.append(str(name).strip() or current["username"])
        if role is not None:
            assignments.append("role=?")
            params.append(_validate_role(role))
        if enabled is not None:
            assignments.append("enabled=?")
            params.append(1 if enabled else 0)
        if password is not None:
            assignments.append("password_hash=?")
            params.append(generate_password_hash(_validate_password(password)))
            assignments.append("password_updated_at=?")
            params.append(now)
        if not assignments:
            return _public(current)
        assignments.append("updated_at=?")
        params.append(now)
        params.append(int(user_id))
        self.conn.execute(f"UPDATE app_user SET {', '.join(assignments)} WHERE id=?", params)
        return _public(self.find_by_id(user_id) or {})

    def delete(self, user_id: int) -> dict[str, Any]:
        current = self.find_by_id(user_id)
        if not current:
            raise AccountError("대상 계정을 찾을 수 없습니다.")
        if current["role"] == "admin":
            self._guard_last_admin(user_id, action="삭제")
        self.conn.execute("DELETE FROM app_user WHERE id=?", (int(user_id),))
        return _public(current)

    def touch_login(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE app_user SET last_login_at=? WHERE id=?",
            (datetime.now().isoformat(), int(user_id)),
        )

    def verify(self, username: str, password: str) -> dict[str, Any] | None:
        """Return the session payload when the credentials match an enabled account."""
        current = self.find_by_username(username)
        if not current:
            return None
        if not bool(current.get("enabled", 1)):
            raise AccountError("비활성화된 계정입니다. 관리자에게 문의하세요.")
        try:
            valid = check_password_hash(str(current["password_hash"]), str(password or ""))
        except ValueError:
            valid = False
        if not valid:
            return None
        self.touch_login(int(current["id"]))
        return {
            "id": int(current["id"]),
            "username": str(current["username"]),
            "role": str(current["role"]),
            "name": str(current["name"]),
        }

    def import_legacy(self, users: list[dict[str, Any]]) -> int:
        """Copy accounts from the old JSON file, hashing any plaintext password."""
        imported = 0
        for item in users:
            username = str(item.get("username") or "").strip()
            if not username or self.find_by_username(username):
                continue
            stored = str(item.get("password") or "")
            # Werkzeug hashes contain the method and salt separated by ':'.
            password_hash = stored if ":" in stored else generate_password_hash(stored or "changeme")
            now = datetime.now().isoformat()
            self.conn.execute(
                """
                INSERT INTO app_user(username, password_hash, name, role, enabled, created_at, password_updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    username,
                    password_hash,
                    str(item.get("name") or username),
                    _validate_role(str(item.get("role") or "user")),
                    now,
                    now,
                ),
            )
            imported += 1
        return imported

    def seed_defaults(self) -> int:
        created = 0
        for item in DEFAULT_USERS:
            if self.find_by_username(item["username"]):
                continue
            self.create(
                username=item["username"],
                password=item["password"],
                name=item["name"],
                role=item["role"],
            )
            created += 1
        return created


def ensure_accounts(manager: Any, legacy_file: Path | None = None) -> dict[str, int]:
    """Prepare the account table at startup.

    Order matters: an existing JSON file is imported first so operators keep their
    own accounts, and the built-in defaults are only seeded when nothing exists.
    """
    result = {"imported": 0, "seeded": 0, "existing": 0}
    with manager.connect() as conn:
        repo = UserRepository(conn)
        result["existing"] = repo.count()
        if result["existing"] == 0 and legacy_file and Path(legacy_file).exists():
            try:
                users = json.loads(Path(legacy_file).read_text(encoding="utf-8"))
                if isinstance(users, list):
                    result["imported"] = repo.import_legacy(users)
            except (OSError, ValueError):
                LOGGER.exception("레거시 계정 파일을 읽지 못했습니다: %s", legacy_file)
        if repo.count() == 0:
            result["seeded"] = repo.seed_defaults()
    if result["imported"]:
        LOGGER.warning(
            "레거시 계정 %d건을 DB로 이관했습니다. %s 파일은 더 이상 사용되지 않습니다.",
            result["imported"],
            legacy_file,
        )
    return result
