from __future__ import annotations

import json
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from application.accounts import AccountError, UserRepository, ensure_accounts
from asset_sync.config import load_config
from asset_sync.db.manager import create_manager


def _manager(tmp_path: Path, monkeypatch):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    manager = create_manager(load_config())
    manager.initialize()
    return manager


def test_defaults_are_seeded_when_no_accounts_exist(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    result = ensure_accounts(manager)
    assert result == {"imported": 0, "seeded": 2, "existing": 0}
    with manager.connect() as conn:
        users = UserRepository(conn).list_users()
    assert [u["username"] for u in users] == ["admin", "user"]
    assert all("password_hash" not in u for u in users)


def test_seeding_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    second = ensure_accounts(manager)
    assert second["seeded"] == 0 and second["existing"] == 2


def test_legacy_json_accounts_are_imported_and_plaintext_is_hashed(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    legacy = tmp_path / "users.json"
    legacy.write_text(
        json.dumps(
            [
                {"id": 1, "username": "operator", "password": generate_password_hash("hashed-pw"), "role": "admin", "name": "운영자"},
                {"id": 2, "username": "plain", "password": "plain-pw", "role": "user", "name": "평문계정"},
            ]
        ),
        encoding="utf-8",
    )
    result = ensure_accounts(manager, legacy)
    assert result["imported"] == 2 and result["seeded"] == 0

    with manager.connect() as conn:
        repo = UserRepository(conn)
        # 두 계정 모두 원래 비밀번호로 로그인되어야 한다.
        assert repo.verify("operator", "hashed-pw")["role"] == "admin"
        assert repo.verify("plain", "plain-pw")["name"] == "평문계정"
        # 평문은 저장되지 않는다.
        stored = repo.find_by_username("plain")["password_hash"]
    assert stored != "plain-pw" and ":" in stored


def test_verify_rejects_wrong_password_and_unknown_user(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    with manager.connect() as conn:
        repo = UserRepository(conn)
        assert repo.verify("admin", "wrong") is None
        assert repo.verify("nobody", "admin123") is None
        payload = repo.verify("admin", "admin123")
    assert payload == {"id": payload["id"], "username": "admin", "role": "admin", "name": "관리자"}


def test_disabled_account_cannot_log_in(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    with manager.connect() as conn:
        repo = UserRepository(conn)
        user_id = repo.find_by_username("user")["id"]
        repo.update(user_id, enabled=False)
        with pytest.raises(AccountError):
            repo.verify("user", "user123")


def test_login_records_last_login(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    with manager.connect() as conn:
        repo = UserRepository(conn)
        assert repo.find_by_username("admin")["last_login_at"] is None
        repo.verify("admin", "admin123")
        assert repo.find_by_username("admin")["last_login_at"] is not None


def test_password_change_takes_effect(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    with manager.connect() as conn:
        repo = UserRepository(conn)
        user_id = repo.find_by_username("user")["id"]
        repo.update(user_id, password="new-password")
        assert repo.verify("user", "user123") is None
        assert repo.verify("user", "new-password") is not None


def test_duplicate_username_and_weak_password_are_rejected(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    with manager.connect() as conn:
        repo = UserRepository(conn)
        with pytest.raises(AccountError):
            repo.create(username="admin", password="whatever", role="user")
        with pytest.raises(AccountError):
            repo.create(username="newuser", password="123", role="user")
        with pytest.raises(AccountError):
            repo.create(username="  ", password="goodpassword", role="user")
        with pytest.raises(AccountError):
            repo.create(username="newuser", password="goodpassword", role="superuser")


def test_last_admin_cannot_be_removed_disabled_or_demoted(tmp_path: Path, monkeypatch) -> None:
    """관리자가 하나도 남지 않으면 아무도 관리 화면에 들어갈 수 없다."""
    manager = _manager(tmp_path, monkeypatch)
    ensure_accounts(manager)
    with manager.connect() as conn:
        repo = UserRepository(conn)
        admin_id = repo.find_by_username("admin")["id"]
        for call in (
            lambda: repo.delete(admin_id),
            lambda: repo.update(admin_id, enabled=False),
            lambda: repo.update(admin_id, role="user"),
        ):
            with pytest.raises(AccountError) as error:
                call()
            assert "마지막 관리자" in str(error.value)

        # 관리자를 하나 더 만들면 기존 관리자를 정리할 수 있다.
        repo.create(username="admin2", password="admin2-password", role="admin", name="관리자2")
        assert repo.delete(admin_id)["username"] == "admin"


def test_account_api_end_to_end(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)

    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    app = create_app()
    client = app.test_client()

    # DB 계정으로 로그인된다.
    assert client.post("/login", json={"username": "admin", "password": "admin123"}).get_json()["success"] is True
    assert client.post("/login", json={"username": "admin", "password": "nope"}).get_json()["success"] is False

    created = client.post("/api/users", json={
        "username": "tester", "password": "tester-password", "name": "테스터", "role": "user"
    }).get_json()
    assert created["success"] is True
    user_id = created["id"]

    listed = client.get("/api/users").get_json()
    assert {u["username"] for u in listed} == {"admin", "user", "tester"}
    assert all("password_hash" not in u for u in listed)

    assert client.put(f"/api/users/{user_id}", json={"role": "admin"}).get_json()["user"]["role"] == "admin"
    assert client.put(f"/api/users/{user_id}", json={"password": "12345"}).status_code == 400
    assert client.delete(f"/api/users/{user_id}").get_json()["success"] is True
    assert client.delete("/api/users/9999").status_code == 400

    # 계정 변경이 감사로그에 남는다.
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "관리자"}
    actions = {row["action"] for row in client.get("/api/admin/audit-log").get_json() if row["target_type"] == "app_user"}
    assert {"CREATE", "UPDATE", "DELETE"} <= actions
    reset_database_manager()
