"""담당자가 파일만 추가하면 붙는지 확인한다.

목록을 코드에 두면 담당자가 늘 때마다 main.html · create_app() · app_config.yaml
같은 공용 파일에서 병합 충돌이 난다. 여기서 검증하는 것은 '파일이 있으면 쓰이고,
공용 파일은 고치지 않아도 된다' 는 것이다.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from application.module_assets import discover_module_templates, register_local_modules
from asset_sync.config import load_module_registry_files


# ------------------------------------------------------------ 설정 파일 발견
def _config_root(tmp_path: Path) -> Path:
    (tmp_path / "config" / "modules").mkdir(parents=True)
    return tmp_path


def test_a_module_file_becomes_a_registry_entry(tmp_path: Path) -> None:
    root = _config_root(tmp_path)
    (root / "config" / "modules" / "capacity.yaml").write_text(
        "name: 용량 관리\nbase_url: http://10.0.0.9:8081\nmenu_section: 운영\n", encoding="utf-8"
    )
    assert load_module_registry_files(root) == [
        {"id": "capacity", "name": "용량 관리", "base_url": "http://10.0.0.9:8081", "menu_section": "운영"}
    ]


def test_the_id_comes_from_the_file_name(tmp_path: Path) -> None:
    root = _config_root(tmp_path)
    (root / "config" / "modules" / "backup.yaml").write_text("name: 백업\n", encoding="utf-8")
    assert load_module_registry_files(root)[0]["id"] == "backup"


def test_a_mismatched_id_is_rejected(tmp_path: Path) -> None:
    """파일 하나가 대메뉴 하나다. 이름이 어긋나면 어느 파일이 무엇인지 알 수 없다."""
    root = _config_root(tmp_path)
    (root / "config" / "modules" / "backup.yaml").write_text("id: capacity\nname: 백업\n", encoding="utf-8")
    with pytest.raises(ValueError, match="파일 이름과 다릅니다"):
        load_module_registry_files(root)


def test_local_overrides_win(tmp_path: Path) -> None:
    """개발 장비 주소가 저장소에 섞이지 않게 .local.yaml 로 덮어쓴다."""
    root = _config_root(tmp_path)
    modules = root / "config" / "modules"
    (modules / "capacity.yaml").write_text("name: 용량\nbase_url: http://prod:8081\n", encoding="utf-8")
    (modules / "capacity.local.yaml").write_text("base_url: http://127.0.0.1:8081\n", encoding="utf-8")
    entries = load_module_registry_files(root)
    assert len(entries) == 1  # .local.yaml 은 별도 모듈이 아니다
    assert entries[0]["base_url"] == "http://127.0.0.1:8081"
    assert entries[0]["name"] == "용량"


def test_files_are_sorted_and_underscore_is_skipped(tmp_path: Path) -> None:
    root = _config_root(tmp_path)
    modules = root / "config" / "modules"
    for name in ("zeta", "alpha", "_template"):
        (modules / f"{name}.yaml").write_text("name: x\n", encoding="utf-8")
    assert [item["id"] for item in load_module_registry_files(root)] == ["alpha", "zeta"]


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert load_module_registry_files(tmp_path) == []


def test_module_files_are_merged_into_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config() 을 거쳐 실제 레지스트리에 들어가는지 본다."""
    from asset_sync.config import load_config

    root = _config_root(tmp_path)
    (root / "config" / "app_config.yaml").write_text(
        textwrap.dedent(
            """
            app:
              timezone: Asia/Seoul
              sqlite_path: data/x.db
              log_level: INFO
            modules:
              registry:
              - id: asset_sync
                name: 자산 정합성
            """
        ),
        encoding="utf-8",
    )
    (root / "config" / "modules" / "capacity.yaml").write_text(
        "name: 용량 관리\nbase_url: http://10.0.0.9:8081\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(root))
    config = load_config(root / "config" / "app_config.yaml")
    ids = [item["id"] for item in config.modules["registry"]]
    # app_config.yaml 의 기존 항목은 유지되고 파일이 뒤에 붙는다.
    assert ids == ["asset_sync", "capacity"]

    from application.modules.registry import ModuleRegistry

    registry = ModuleRegistry.from_config(config.modules)
    assert registry.require("capacity").base_url == "http://10.0.0.9:8081"


def test_a_module_file_overrides_the_same_id_in_app_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from asset_sync.config import load_config

    root = _config_root(tmp_path)
    (root / "config" / "app_config.yaml").write_text(
        "app:\n  timezone: Asia/Seoul\n  sqlite_path: data/x.db\n  log_level: INFO\n"
        "modules:\n  registry:\n  - id: capacity\n    name: 옛 이름\n",
        encoding="utf-8",
    )
    (root / "config" / "modules" / "capacity.yaml").write_text("name: 새 이름\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(root))
    registry = load_config(root / "config" / "app_config.yaml").modules["registry"]
    assert [item["name"] for item in registry] == ["새 이름"]


# ------------------------------------------------------------ 템플릿 발견
def _template_module(tmp_path: Path, module_id: str, *roles: str) -> Path:
    directory = tmp_path / "modules" / module_id
    directory.mkdir(parents=True)
    for role in roles:
        (directory / f"{role}.html").write_text(f"<!-- {module_id} {role} -->", encoding="utf-8")
    return directory


def test_templates_are_found_by_role(tmp_path: Path) -> None:
    _template_module(tmp_path, "capacity", "page", "scripts", "widget")
    found = discover_module_templates(tmp_path)
    assets = found["capacity"]
    assert assets.page == "modules/capacity/page.html"
    assert assets.scripts == "modules/capacity/scripts.html"
    assert assets.widget == "modules/capacity/widget.html"


def test_partial_sets_are_allowed(tmp_path: Path) -> None:
    """위젯만 제공하고 대메뉴 화면은 공통 화면을 쓰는 경우가 있다."""
    _template_module(tmp_path, "backup", "widget")
    assets = discover_module_templates(tmp_path)["backup"]
    assert assets.widget and not assets.page and not assets.scripts


def test_an_empty_directory_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "modules" / "empty").mkdir(parents=True)
    assert discover_module_templates(tmp_path) == {}


def test_underscore_directories_are_skipped(tmp_path: Path) -> None:
    _template_module(tmp_path, "_shared", "page")
    assert discover_module_templates(tmp_path) == {}


def test_missing_templates_directory_is_not_an_error(tmp_path: Path) -> None:
    assert discover_module_templates(tmp_path / "nope") == {}


# ------------------------------------------------------------ 내부 모듈 코드
class _FakeApp:
    def __init__(self) -> None:
        self.blueprints: list[Any] = []

    def register_blueprint(self, blueprint: object) -> None:
        self.blueprints.append(blueprint)


@pytest.fixture()
def local_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """import 가능한 임시 application.modules_local 패키지를 만든다."""
    import sys

    import application.module_assets as assets_module

    (tmp_path / "application").mkdir()
    (tmp_path / "application" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "application" / "modules_local").mkdir()
    (tmp_path / "application" / "modules_local" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(assets_module, "LOCAL_PACKAGE", "fakeapp.modules_local")
    (tmp_path / "fakeapp").mkdir()
    (tmp_path / "fakeapp" / "__init__.py").write_text("", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for name in [key for key in sys.modules if key.startswith("fakeapp")]:
        del sys.modules[name]


def test_local_routes_and_panel_are_registered(local_package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from application.modules.panels import _LOCAL_PROVIDERS, clear_local_panels

    # fakeapp 아래에도 같은 파일을 둬야 import 가 된다.
    for base in ("application", "fakeapp"):
        directory = local_package / base / "modules_local" / "capacity"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
        (directory / "routes.py").write_text(
            "from flask import Blueprint\nbp = Blueprint('capacity_local', __name__)\n", encoding="utf-8"
        )
        (directory / "panel.py").write_text(
            "def panel(user, params):\n    return {'title': '용량'}\n", encoding="utf-8"
        )

    clear_local_panels()
    app = _FakeApp()
    result = register_local_modules(app, local_package)
    assert result == {"routes": ["capacity"], "panels": ["capacity"]}
    assert [bp.name for bp in app.blueprints] == ["capacity_local"]
    assert "capacity" in _LOCAL_PROVIDERS
    clear_local_panels()


def test_one_broken_module_does_not_stop_startup(local_package: Path) -> None:
    """한 담당자의 실수로 통합 웹 전체가 기동하지 못하면 안 된다."""
    for base in ("application", "fakeapp"):
        for module_id, body in (("bad", "raise RuntimeError('boom')\n"), ("good", "from flask import Blueprint\nbp = Blueprint('good_local', __name__)\n")):
            directory = local_package / base / "modules_local" / module_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "__init__.py").write_text("", encoding="utf-8")
            (directory / "routes.py").write_text(body, encoding="utf-8")

    app = _FakeApp()
    result = register_local_modules(app, local_package)
    assert result["routes"] == ["good"]
    assert [bp.name for bp in app.blueprints] == ["good_local"]


def test_routes_without_a_blueprint_is_reported_not_raised(local_package: Path) -> None:
    for base in ("application", "fakeapp"):
        directory = local_package / base / "modules_local" / "nobp"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
        (directory / "routes.py").write_text("value = 1\n", encoding="utf-8")

    app = _FakeApp()
    assert register_local_modules(app, local_package)["routes"] == []


def test_no_local_modules_directory_is_not_an_error(tmp_path: Path) -> None:
    assert register_local_modules(_FakeApp(), tmp_path) == {"routes": [], "panels": []}


# ------------------------------------------------------------ 화면 렌더링
PROBE_ID = "zz_probe"


@pytest.fixture()
def probe_templates():
    """실제 templates/modules/ 에 담당자 파일을 두고 정리한다.

    템플릿 폴더는 통합 웹 저장소의 실제 경로이므로 여기서만 종단 확인이 된다.
    """
    import shutil

    from application.settings import BASE_DIR

    directory = BASE_DIR / "templates" / "modules" / PROBE_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.html").write_text(
        '<div class="page" id="page-' + PROBE_ID + '">PROBE_PAGE</div>', encoding="utf-8"
    )
    (directory / "scripts.html").write_text("<script>/* PROBE_SCRIPT */</script>", encoding="utf-8")
    (directory / "widget.html").write_text('<div class="card">PROBE_WIDGET</div>', encoding="utf-8")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_templates: Path):
    (tmp_path / "config" / "modules").mkdir(parents=True)
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    # 담당자는 이 파일 하나만 추가한다. app_config.yaml 은 손대지 않는다.
    (tmp_path / "config" / "modules" / f"{PROBE_ID}.yaml").write_text(
        "name: 검증 모듈\nbase_url: http://127.0.0.1:1\naccess: explicit\n", encoding="utf-8"
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


def _client(app, role: str, user_id: int, username: str):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": user_id, "username": username, "role": role, "name": username}
    return client


def test_owner_files_render_without_touching_main_html(portal) -> None:
    html = _client(portal, "admin", 1, "admin").get("/dashboard").get_data(as_text=True)
    assert "PROBE_PAGE" in html
    assert "PROBE_SCRIPT" in html
    assert "PROBE_WIDGET" in html
    # 담당자 화면이 있으면 공통 기본 화면은 렌더링하지 않는다.
    assert html.count(f'id="page-{PROBE_ID}"') == 1


def test_templates_are_not_rendered_without_permission(portal) -> None:
    """화면 파일이 저장소에 있다는 것이 접근 권한을 주지는 않는다."""
    html = _client(portal, "user", 2, "user").get("/dashboard").get_data(as_text=True)
    assert "PROBE_PAGE" not in html
    assert "PROBE_SCRIPT" not in html
    assert "PROBE_WIDGET" not in html
    assert "검증 모듈" not in html
