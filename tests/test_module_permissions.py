from __future__ import annotations

from pathlib import Path

import pytest

from application.accounts import UserRepository, ensure_accounts
from application.modules.registry import ModuleRegistry
from application.permissions import (
    PERMISSION_MANAGE,
    PERMISSION_NONE,
    PERMISSION_VIEW,
    ModulePermissionRepository,
    PermissionError_,
    at_least,
    resolve_permission,
)
from asset_sync.config import load_config
from asset_sync.db.manager import create_manager

REGISTRY = ModuleRegistry.from_config(
    {
        "registry": [
            {"id": "asset_sync", "name": "자산 정합성"},
            {"id": "capacity", "name": "용량 관리", "base_url": "http://was-capacity:5301"},
            {"id": "secrets_menu", "name": "대외비", "base_url": "http://was-x:1", "access": "explicit"},
            {"id": "admin_only", "name": "관리 전용", "required_role": "admin"},
            {"id": "off", "name": "중지됨", "enabled": False},
        ]
    }
)


def _manager(tmp_path: Path, monkeypatch):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    manager = create_manager(load_config())
    manager.initialize()
    ensure_accounts(manager)
    return manager


# ---------------------------------------------------------------- 판정 규칙
def test_admin_gets_manage_on_every_module() -> None:
    admin = {"role": "admin"}
    for module in REGISTRY.all():
        expected = PERMISSION_NONE if not module.enabled else PERMISSION_MANAGE
        assert resolve_permission(module, admin) == expected


def test_role_mode_falls_back_to_required_role() -> None:
    """이 테이블을 도입해도 기존 계정이 대메뉴를 잃지 않아야 한다."""
    user = {"role": "user"}
    assert resolve_permission(REGISTRY.get("asset_sync"), user) == PERMISSION_VIEW
    assert resolve_permission(REGISTRY.get("capacity"), user) == PERMISSION_VIEW
    assert resolve_permission(REGISTRY.get("admin_only"), user) == PERMISSION_NONE


def test_explicit_mode_denies_without_a_grant() -> None:
    user = {"role": "user"}
    module = REGISTRY.get("secrets_menu")
    assert resolve_permission(module, user) == PERMISSION_NONE
    assert resolve_permission(module, user, {"secrets_menu": "VIEW"}) == PERMISSION_VIEW
    assert resolve_permission(module, user, {"secrets_menu": "MANAGE"}) == PERMISSION_MANAGE


def test_explicit_grant_overrides_role_fallback() -> None:
    user = {"role": "user"}
    # role 모드 모듈도 명시 부여로 등급을 올릴 수 있다.
    assert resolve_permission(REGISTRY.get("capacity"), user, {"capacity": "MANAGE"}) == PERMISSION_MANAGE


def test_disabled_module_is_never_accessible() -> None:
    assert resolve_permission(REGISTRY.get("off"), {"role": "admin"}, {"off": "MANAGE"}) == PERMISSION_NONE


def test_permission_ranking() -> None:
    assert at_least(PERMISSION_MANAGE, PERMISSION_VIEW) is True
    assert at_least(PERMISSION_VIEW, PERMISSION_MANAGE) is False
    assert at_least(PERMISSION_NONE, PERMISSION_VIEW) is False


def test_registry_accessible_reflects_grants() -> None:
    user = {"role": "user"}
    assert {m.id for m, _ in REGISTRY.accessible(user)} == {"asset_sync", "capacity"}
    with_grant = REGISTRY.accessible(user, {"secrets_menu": "VIEW", "admin_only": "MANAGE"})
    assert {m.id for m, _ in with_grant} == {"asset_sync", "capacity", "secrets_menu", "admin_only"}
    assert dict((m.id, p) for m, p in with_grant)["admin_only"] == PERMISSION_MANAGE


# ---------------------------------------------------------------- 저장소
def test_grant_update_and_revoke(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        users, permissions = UserRepository(conn), ModulePermissionRepository(conn)
        uid = users.find_by_username("user")["id"]

        permissions.grant(uid, "capacity", "VIEW", "admin")
        assert permissions.for_user(uid) == {"capacity": "VIEW"}
        # 같은 모듈에 다시 부여하면 갱신된다.
        permissions.grant(uid, "capacity", "MANAGE", "admin")
        assert permissions.for_user(uid) == {"capacity": "MANAGE"}
        permissions.revoke(uid, "capacity")
        assert permissions.for_user(uid) == {}


def test_grant_normalizes_and_validates(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        permissions = ModulePermissionRepository(conn)
        uid = UserRepository(conn).find_by_username("user")["id"]
        permissions.grant(uid, "  Capacity  ", "view")
        assert permissions.for_user(uid) == {"capacity": "VIEW"}
        with pytest.raises(PermissionError_):
            permissions.grant(uid, "capacity", "OWNER")
        with pytest.raises(PermissionError_):
            permissions.grant(uid, "", "VIEW")


def test_replace_rejects_unknown_module(tmp_path: Path, monkeypatch) -> None:
    """오타로 만든 권한 행이 남으면 나중에 그 id 로 모듈을 만들 때 살아난다."""
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        permissions = ModulePermissionRepository(conn)
        uid = UserRepository(conn).find_by_username("user")["id"]
        known = [module.id for module in REGISTRY.all()]
        with pytest.raises(PermissionError_):
            permissions.replace_for_user(uid, {"typo_module": "VIEW"}, known_modules=known)


def test_replace_applies_and_clears(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        permissions = ModulePermissionRepository(conn)
        uid = UserRepository(conn).find_by_username("user")["id"]
        known = [module.id for module in REGISTRY.all()]
        permissions.replace_for_user(uid, {"capacity": "MANAGE", "secrets_menu": "VIEW"}, known_modules=known)
        assert permissions.for_user(uid) == {"capacity": "MANAGE", "secrets_menu": "VIEW"}
        # 빈 값은 회수로 처리한다.
        permissions.replace_for_user(uid, {"capacity": "", "secrets_menu": "VIEW"}, known_modules=known)
        assert permissions.for_user(uid) == {"secrets_menu": "VIEW"}


def test_permissions_are_removed_with_the_account(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        users, permissions = UserRepository(conn), ModulePermissionRepository(conn)
        uid = users.find_by_username("user")["id"]
        permissions.grant(uid, "capacity", "VIEW")
        permissions.delete_for_user(uid)
        users.delete(uid)
        assert permissions.for_user(uid) == {}


def test_users_for_module_lists_holders(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with manager.connect() as conn:
        users, permissions = UserRepository(conn), ModulePermissionRepository(conn)
        uid = users.find_by_username("user")["id"]
        permissions.grant(uid, "capacity", "MANAGE", "admin")
        holders = permissions.users_for_module("capacity")
    assert [(item["username"], item["permission"]) for item in holders] == [("user", "MANAGE")]


# ---------------------------------------------------------------- API / 화면
@pytest.fixture()
def portal(tmp_path: Path, monkeypatch):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n"
        "modules:\n"
        "  registry:\n"
        "    - id: asset_sync\n"
        "      name: 자산 정합성\n"
        "      base_url: ''\n"
        "    - id: secrets_menu\n"
        "      name: 대외비\n"
        "      base_url: http://127.0.0.1:1\n"
        "      access: explicit\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    app = create_app()
    yield app
    reset_database_manager()


def _client(app, role: str = "admin", user_id: int = 1, username: str = "admin"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": user_id, "username": username, "role": role, "name": username}
    return client


def test_permission_api_grants_and_revokes(portal) -> None:
    admin = _client(portal)
    users = admin.get("/api/users").get_json()
    target = next(item for item in users if item["username"] == "user")

    listed = admin.get(f"/api/users/{target['id']}/modules").get_json()
    by_id = {item["id"]: item for item in listed["modules"]}
    assert by_id["secrets_menu"]["granted"] is None
    assert by_id["secrets_menu"]["effective"] == PERMISSION_NONE
    assert by_id["asset_sync"]["effective"] == PERMISSION_VIEW

    granted = admin.put(
        f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": "MANAGE"}}
    ).get_json()
    assert granted["permissions"] == {"secrets_menu": "MANAGE"}

    after = admin.get(f"/api/users/{target['id']}/modules").get_json()
    assert {item["id"]: item["effective"] for item in after["modules"]}["secrets_menu"] == PERMISSION_MANAGE

    # 빈 값으로 회수된다.
    admin.put(f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": ""}})
    assert admin.get(f"/api/users/{target['id']}/modules").get_json()["modules"][1]["granted"] is None


def test_permission_api_rejects_unknown_module_and_user(portal) -> None:
    admin = _client(portal)
    assert admin.put("/api/users/1/modules", json={"permissions": {"nope": "VIEW"}}).status_code == 400
    assert admin.get("/api/users/9999/modules").status_code == 404


def test_permission_api_requires_admin(portal) -> None:
    plain = _client(portal, role="user", user_id=2, username="user")
    assert plain.get("/api/users/1/modules").status_code == 403


def test_module_list_reflects_explicit_grant(portal) -> None:
    admin = _client(portal)
    target = next(item for item in admin.get("/api/users").get_json() if item["username"] == "user")
    as_user = _client(portal, role="user", user_id=target["id"], username="user")

    before = {m["id"] for m in as_user.get("/api/modules").get_json()["modules"]}
    assert before == {"asset_sync"}

    admin.put(f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": "VIEW"}})
    after = {m["id"]: m["permission"] for m in as_user.get("/api/modules").get_json()["modules"]}
    assert after == {"asset_sync": PERMISSION_VIEW, "secrets_menu": PERMISSION_VIEW}


def test_proxy_requires_manage_for_write(portal) -> None:
    admin = _client(portal)
    target = next(item for item in admin.get("/api/users").get_json() if item["username"] == "user")
    as_user = _client(portal, role="user", user_id=target["id"], username="user")

    admin.put(f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": "VIEW"}})
    # VIEW 로는 조회만 가능하다. 대상 WAS 가 죽어 있어도 권한 검사가 먼저 걸린다.
    assert as_user.post("/api/modules/secrets_menu/proxy/api/x").status_code == 403
    assert as_user.get("/api/modules/secrets_menu/proxy/api/x").status_code == 504

    admin.put(f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": "MANAGE"}})
    assert as_user.post("/api/modules/secrets_menu/proxy/api/x").status_code == 504


def test_menu_only_shows_permitted_modules(portal) -> None:
    admin = _client(portal)
    target = next(item for item in admin.get("/api/users").get_json() if item["username"] == "user")
    as_user = _client(portal, role="user", user_id=target["id"], username="user")

    html = as_user.get("/dashboard").get_data(as_text=True)
    assert "대외비" not in html

    admin.put(f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": "VIEW"}})
    html = as_user.get("/dashboard").get_data(as_text=True)
    assert "대외비" in html


def test_permission_change_is_audited(portal) -> None:
    admin = _client(portal)
    target = next(item for item in admin.get("/api/users").get_json() if item["username"] == "user")
    admin.put(f"/api/users/{target['id']}/modules", json={"permissions": {"secrets_menu": "VIEW"}})
    rows = admin.get("/api/admin/audit-log").get_json()
    assert any(row["action"] == "PERMISSION" and row["target_type"] == "app_user" for row in rows)
