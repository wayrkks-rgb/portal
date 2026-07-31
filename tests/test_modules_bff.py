from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from application.modules.client import ModuleClient
from application.modules.panels import PanelAggregator, clear_local_panels, normalize_panel, register_local_panel
from application.modules.registry import ModuleConfigError, ModuleRegistry
from application.modules.tokens import CLOCK_SKEW_SECONDS, issue_token, mask_token, verify_token

SECRET = "shared-secret-for-tests"


# ---------------------------------------------------------------- 가짜 WAS
class _Handler(BaseHTTPRequestHandler):
    """도메인 WAS 역할. 실제 HTTP 로 검증하기 위해 mock 대신 서버를 띄운다."""

    behaviour: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # 테스트 출력 억제
        return

    def _respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "should-not-be-forwarded=1")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # 타임아웃 검증에서 통합 웹이 먼저 끊는 것이 정상 동작이다.
            pass

    def do_GET(self) -> None:  # noqa: N802
        token = self.headers.get("X-Portal-Token")
        self.behaviour.setdefault("tokens", []).append(token)
        if self.path.startswith("/api/slow"):
            time.sleep(self.behaviour.get("delay", 2.0))
            self._respond(200, {"title": "느린 모듈"})
            return
        if self.path.startswith("/api/broken"):
            self._respond(500, {"error": "모듈 내부 오류"})
            return
        if self.path.startswith("/api/secure"):
            payload = verify_token(token, SECRET, module_id="remote")
            if payload is None:
                self._respond(401, {"error": "토큰 검증 실패"})
                return
            self._respond(200, {"title": "인증됨", "metrics": [{"label": "사용자", "value": payload["sub"]}]})
            return
        if self.path.startswith("/api/health"):
            self._respond(200, {"status": "UP"})
            return
        if self.path.startswith("/api/dashboard/panel"):
            self._respond(200, {
                "title": "용량 현황",
                "metrics": [{"label": "총 VM", "value": 1234, "unit": "대", "state": "info"}],
                "table": {"columns": ["호스트", "CPU%"], "rows": [["esxi-01", 72]]},
                "note": "07:00 기준",
            })
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._respond(200, {"received": body.decode("utf-8"), "method": "POST"})


@pytest.fixture(scope="module")
def fake_was():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _registry(base_url: str, **overrides: Any) -> ModuleRegistry:
    settings = {
        "request_timeout_seconds": 1.0,
        "retry_count": 0,
        "dashboard_budget_seconds": 5,
        "auth": {"shared_secret": SECRET, "token_ttl_seconds": 60, "header": "X-Portal-Token"},
        "registry": [
            {"id": "asset_sync", "name": "자산 정합성", "base_url": "", "menu_section": "운영"},
            {"id": "remote", "name": "용량 관리", "base_url": base_url},
        ],
    }
    settings.update(overrides)
    return ModuleRegistry.from_config(settings)


# ---------------------------------------------------------------- 레지스트리
def test_registry_separates_local_and_remote(fake_was) -> None:
    registry = _registry(fake_was)
    assert registry.get("asset_sync").is_local is True
    assert registry.get("remote").is_local is False
    assert {m.id for m in registry.enabled()} == {"asset_sync", "remote"}


def test_registry_skips_invalid_entries_without_failing_startup() -> None:
    registry = ModuleRegistry.from_config(
        {
            "registry": [
                {"id": "Bad Id!", "name": "잘못된 id"},
                {"id": "no_scheme", "name": "스킴 없음", "base_url": "was-host:5000"},
                {"id": "good", "name": "정상"},
            ]
        }
    )
    # 설정 오류 하나로 통합 웹 전체가 못 뜨면 안 된다.
    assert [m.id for m in registry.all()] == ["good"]


def test_registry_enforces_role_and_duplicate_ids() -> None:
    registry = ModuleRegistry.from_config(
        {"registry": [{"id": "secret_menu", "name": "관리 전용", "required_role": "admin"}]}
    )
    assert [m.id for m in registry.visible("admin")] == ["secret_menu"]
    assert registry.visible("user") == []
    with pytest.raises(ModuleConfigError):
        ModuleRegistry([_registry("http://x").get("remote"), _registry("http://x").get("remote")])


def test_path_allowlist_blocks_traversal_and_other_prefixes(fake_was) -> None:
    module = _registry(fake_was).get("remote")
    assert module.resolve("/api/health").endswith("/api/health")
    for bad in ("/admin/secret", "/api/../../etc/passwd", "/"):
        with pytest.raises(ModuleConfigError):
            module.resolve(bad)


# ---------------------------------------------------------------- 토큰
def test_token_round_trip_carries_user_and_module() -> None:
    token = issue_token(SECRET, user={"id": 7, "username": "hong", "role": "admin", "name": "홍길동"}, module_id="remote")
    payload = verify_token(token, SECRET, module_id="remote")
    assert payload["sub"] == "hong" and payload["uid"] == 7 and payload["role"] == "admin"


def test_token_is_rejected_when_tampered_or_wrong_secret() -> None:
    token = issue_token(SECRET, user={"username": "hong"}, module_id="remote")
    assert verify_token(token, "other-secret") is None
    assert verify_token(token[:-2] + "xx", SECRET) is None
    payload_b64, signature = token.split(".")
    assert verify_token(f"{payload_b64}x.{signature}", SECRET) is None
    assert verify_token(None, SECRET) is None
    assert verify_token(token, "") is None


def test_token_cannot_be_reused_for_another_module() -> None:
    token = issue_token(SECRET, user={"username": "hong"}, module_id="remote")
    assert verify_token(token, SECRET, module_id="remote") is not None
    assert verify_token(token, SECRET, module_id="other") is None


def test_expired_token_is_rejected_with_skew_allowance() -> None:
    now = time.time()
    token = issue_token(SECRET, user={"username": "hong"}, module_id="remote", ttl_seconds=10, now=now)
    assert verify_token(token, SECRET, now=now + 5) is not None
    # 시계 오차 허용치 안이면 통과, 넘으면 거부한다.
    assert verify_token(token, SECRET, now=now + 10 + CLOCK_SKEW_SECONDS - 1) is not None
    assert verify_token(token, SECRET, now=now + 10 + CLOCK_SKEW_SECONDS + 1) is None


def test_mask_token_hides_signature() -> None:
    token = issue_token(SECRET, user={"username": "hong"}, module_id="remote")
    masked = mask_token(token)
    assert token not in masked and "***" in masked


# ---------------------------------------------------------------- 클라이언트
def test_client_calls_remote_module_and_receives_json(fake_was) -> None:
    client = ModuleClient(_registry(fake_was))
    response = client.call("remote", "/api/dashboard/panel", user={"username": "hong", "role": "user"})
    assert response.ok and response.http_status == 200
    assert response.data["title"] == "용량 현황"
    client.close()


def test_remote_module_can_verify_the_portal_token(fake_was) -> None:
    """WAS 쪽에서 verify_token 으로 사용자를 확인할 수 있어야 한다."""
    client = ModuleClient(_registry(fake_was))
    response = client.call("remote", "/api/secure", user={"id": 3, "username": "hong", "role": "admin"})
    assert response.ok, response.error
    assert response.data["metrics"][0]["value"] == "hong"
    client.close()


def test_missing_secret_sends_no_token(fake_was) -> None:
    registry = _registry(fake_was, auth={"shared_secret": "", "header": "X-Portal-Token"})
    client = ModuleClient(registry)
    _Handler.behaviour["tokens"] = []
    client.call("remote", "/api/health", user={"username": "hong"})
    assert _Handler.behaviour["tokens"][-1] is None
    client.close()


def test_timeout_and_http_error_become_status_not_exception(fake_was) -> None:
    client = ModuleClient(_registry(fake_was))
    _Handler.behaviour["delay"] = 2.0
    slow = client.call("remote", "/api/slow", user={"username": "hong"})
    assert slow.status == "TIMEOUT" and "응답하지 않았습니다" in slow.error

    broken = client.call("remote", "/api/broken", user={"username": "hong"})
    assert broken.status == "FAILED" and broken.http_status == 500
    assert "모듈 내부 오류" in broken.error
    client.close()


def test_unreachable_module_is_reported(fake_was) -> None:
    registry = _registry("http://127.0.0.1:1")
    client = ModuleClient(registry)
    response = client.call("remote", "/api/health", user={"username": "hong"})
    assert response.status == "UNREACHABLE"
    client.close()


def test_local_module_is_not_proxied(fake_was) -> None:
    client = ModuleClient(_registry(fake_was))
    response = client.call("asset_sync", "/api/health")
    assert response.status == "FAILED" and "내부 모듈" in response.error
    assert client.health("asset_sync").data == {"status": "UP", "location": "LOCAL"}
    client.close()


def test_disallowed_method_is_refused(fake_was) -> None:
    client = ModuleClient(_registry(fake_was))
    response = client.call("remote", "/api/health", method="PATCH")
    assert response.status == "FAILED" and "허용되지 않는 메서드" in response.error
    client.close()


def test_response_size_limit_is_enforced(fake_was) -> None:
    registry = _registry(fake_was, max_response_bytes=10)
    client = ModuleClient(registry)
    response = client.call("remote", "/api/dashboard/panel", user={"username": "hong"})
    assert response.status == "FAILED" and "허용 크기" in response.error
    client.close()


# ---------------------------------------------------------------- 패널 집계
def test_panel_spec_is_normalized_and_capped() -> None:
    panel = normalize_panel(
        {
            "title": "테스트",
            "metrics": [{"label": f"m{i}", "value": i} for i in range(20)],
            "table": {"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}, [3, 4, 5]] + [[0, 0]] * 100},
            "note": "메모",
        }
    )
    assert len(panel["metrics"]) == 8
    assert len(panel["table"]["rows"]) == 50
    assert panel["table"]["rows"][0] == [1, 2]
    assert panel["table"]["rows"][1] == [3, 4]
    # 스펙이 아닌 값이 와도 형태는 유지된다.
    assert normalize_panel(None)["metrics"] == []
    assert normalize_panel({"table": {"rows": [[1]]}})["table"] is None


def test_dashboard_keeps_working_when_one_module_fails(fake_was) -> None:
    """모듈 하나가 죽어도 나머지 패널은 그려져야 한다."""
    clear_local_panels()
    register_local_panel("asset_sync", lambda user, params: {"title": "자산", "metrics": [{"label": "건수", "value": 5}]})
    registry = ModuleRegistry.from_config(
        {
            "request_timeout_seconds": 1.0,
            "dashboard_budget_seconds": 5,
            "auth": {"shared_secret": SECRET},
            "registry": [
                {"id": "asset_sync", "name": "자산 정합성", "base_url": ""},
                {"id": "down", "name": "죽은 모듈", "base_url": "http://127.0.0.1:1"},
                {"id": "remote", "name": "용량 관리", "base_url": fake_was},
            ],
        }
    )
    client = ModuleClient(registry)
    result = PanelAggregator(registry, client).collect({"username": "hong", "role": "admin"})
    by_id = {panel["module_id"]: panel for panel in result["panels"]}

    assert by_id["asset_sync"]["status"] == "SUCCESS"
    assert by_id["asset_sync"]["panel"]["metrics"][0]["value"] == 5
    assert by_id["remote"]["status"] == "SUCCESS"
    assert by_id["down"]["status"] == "UNREACHABLE"
    assert result["degraded"] is True
    assert result["counts"] == {"total": 3, "success": 2, "failed": 1, "skipped": 0}
    client.close()
    clear_local_panels()


def test_local_module_without_provider_is_skipped_not_failed(fake_was) -> None:
    clear_local_panels()
    registry = _registry(fake_was)
    client = ModuleClient(registry)
    result = PanelAggregator(registry, client).collect({"username": "hong", "role": "user"})
    by_id = {panel["module_id"]: panel for panel in result["panels"]}
    assert by_id["asset_sync"]["status"] == "SKIPPED"
    assert result["counts"]["failed"] == 0
    client.close()


def test_dashboard_runs_modules_in_parallel(fake_was) -> None:
    """순차 호출이면 지연이 합산된다. 병렬이어야 한 모듈 지연에 묶이지 않는다."""
    clear_local_panels()
    _Handler.behaviour["delay"] = 0.6
    entries = [{"id": f"m{index}", "name": f"모듈{index}", "base_url": fake_was, "panel_path": "/api/slow"} for index in range(4)]
    registry = ModuleRegistry.from_config(
        {"request_timeout_seconds": 3.0, "dashboard_budget_seconds": 5, "auth": {"shared_secret": SECRET}, "registry": entries}
    )
    client = ModuleClient(registry)
    started = time.monotonic()
    result = PanelAggregator(registry, client).collect({"username": "hong", "role": "user"})
    elapsed = time.monotonic() - started
    assert all(panel["status"] == "SUCCESS" for panel in result["panels"])
    # 순차라면 2.4초 이상이 걸린다.
    assert elapsed < 1.8, f"병렬 호출이 아닙니다: {elapsed:.2f}s"
    client.close()


def test_dashboard_budget_caps_total_wait(fake_was) -> None:
    clear_local_panels()
    _Handler.behaviour["delay"] = 3.0
    registry = ModuleRegistry.from_config(
        {
            "request_timeout_seconds": 10.0,
            "dashboard_budget_seconds": 1.0,
            "auth": {"shared_secret": SECRET},
            "registry": [{"id": "slow", "name": "느린 모듈", "base_url": fake_was, "panel_path": "/api/slow"}],
        }
    )
    client = ModuleClient(registry)
    started = time.monotonic()
    result = PanelAggregator(registry, client).collect({"username": "hong", "role": "user"})
    elapsed = time.monotonic() - started
    assert result["panels"][0]["status"] == "TIMEOUT"
    assert elapsed < 2.5, f"예산을 초과해 기다렸습니다: {elapsed:.2f}s"
    client.close()


# ---------------------------------------------------------------- BFF 라우트
@pytest.fixture()
def portal(tmp_path: Path, monkeypatch, fake_was):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n"
        "modules:\n"
        "  request_timeout_seconds: 1.0\n"
        "  dashboard_budget_seconds: 5\n"
        "  auth:\n"
        f"    shared_secret: {SECRET}\n"
        "  registry:\n"
        "    - id: asset_sync\n"
        "      name: 자산 정합성\n"
        "      base_url: ''\n"
        "    - id: remote\n"
        "      name: 용량 관리\n"
        f"      base_url: {fake_was}\n"
        "    - id: admin_only\n"
        "      name: 관리 전용\n"
        f"      base_url: {fake_was}\n"
        "      required_role: admin\n",
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
    clear_local_panels()


def _client(app, role: str = "admin"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "hong", "role": role, "name": "홍길동"}
    return client


def test_module_list_respects_role(portal) -> None:
    admin_modules = {m["id"] for m in _client(portal, "admin").get("/api/modules").get_json()["modules"]}
    user_modules = {m["id"] for m in _client(portal, "user").get("/api/modules").get_json()["modules"]}
    assert "admin_only" in admin_modules
    assert "admin_only" not in user_modules
    assert {"asset_sync", "remote"} <= user_modules


def test_health_endpoint_aggregates_modules(portal) -> None:
    body = _client(portal).get("/api/modules/health").get_json()
    statuses = {item["module_id"]: item["status"] for item in body["modules"]}
    assert statuses["asset_sync"] == "SUCCESS"
    assert statuses["remote"] == "SUCCESS"
    assert body["status"] == "UP"


def test_dashboard_endpoint_returns_panels(portal) -> None:
    body = _client(portal).get("/api/modules/dashboard").get_json()
    by_id = {panel["module_id"]: panel for panel in body["panels"]}
    # 내부 모듈은 등록된 제공자로, 원격 모듈은 HTTP 로 채워진다.
    assert by_id["asset_sync"]["status"] == "SUCCESS"
    assert by_id["asset_sync"]["panel"]["title"] == "자산 정합성 요약"
    assert by_id["remote"]["panel"]["title"] == "용량 현황"


def test_proxy_forwards_and_strips_cookies(portal) -> None:
    response = _client(portal).get("/api/modules/remote/proxy/api/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "UP"}
    assert "Set-Cookie" not in response.headers


def test_proxy_enforces_permission_and_registration(portal) -> None:
    assert _client(portal, "user").get("/api/modules/admin_only/proxy/api/health").status_code == 403
    assert _client(portal).get("/api/modules/nosuch/proxy/api/health").status_code == 404
    # 허용 접두어 밖의 경로는 거부된다.
    assert _client(portal).get("/api/modules/remote/proxy/admin/secret").status_code == 502


def test_proxy_reports_gateway_failure(portal) -> None:
    _Handler.behaviour["delay"] = 2.0
    response = _client(portal).get("/api/modules/remote/proxy/api/slow")
    assert response.status_code == 504
    assert response.get_json()["status"] == "TIMEOUT"


def test_login_required_on_module_endpoints(portal) -> None:
    anonymous = portal.test_client()
    for path in ("/api/modules", "/api/modules/health", "/api/modules/dashboard"):
        assert anonymous.get(path).status_code in (302, 401)


# ------------------------------------------------- 선택 의존성 (requests)
def test_client_can_be_created_without_requests(monkeypatch) -> None:
    """requests 가 없어도 통합 웹은 기동해야 한다.

    최상위 import 였을 때는 원격 대메뉴를 하나도 쓰지 않는 설치에서도 앱 자체가
    뜨지 않았다(ModuleNotFoundError: No module named 'requests').
    """
    import application.modules.client as client_module

    registry = ModuleRegistry.from_config({"registry": [{"id": "asset_sync", "name": "자산", "base_url": ""}]})
    monkeypatch.setattr(client_module, "_import_requests", _raise_missing)
    # 생성 시점에 세션을 만들지 않으므로 예외가 없어야 한다.
    ModuleClient(registry)


def _raise_missing():
    from application.modules.registry import ModuleConfigError

    raise ModuleConfigError(client_missing_message())


def client_missing_message() -> str:
    import application.modules.client as client_module

    return client_module._REQUESTS_MISSING


def test_a_remote_call_without_requests_fails_only_that_module(monkeypatch) -> None:
    """설치 안내가 담긴 실패 응답이어야 하고, 예외로 화면을 깨면 안 된다."""
    import application.modules.client as client_module

    registry = ModuleRegistry.from_config(
        {"registry": [{"id": "remote", "name": "원격", "base_url": "http://127.0.0.1:9"}]}
    )
    monkeypatch.setattr(client_module, "_import_requests", _raise_missing)
    response = ModuleClient(registry).call("remote", "/api/health")
    assert response.ok is False
    assert "requirements-bff.txt" in (response.error or "")
