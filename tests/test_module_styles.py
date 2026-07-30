"""모듈 CSS 격리와 레지스트리 재적용.

모든 화면이 base.html 의 전역 스타일을 공유한다. 담당자가 자기 화면에서 `.card` 를
재정의하면 다른 팀 화면까지 바뀌므로, 모듈 템플릿의 <style> 을 그 화면 안으로 가둔다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from application.module_assets import ModuleScopedLoader
from application.module_styles import iter_style_selectors, scope_css, scope_template


# ---------------------------------------------------------------- 선택자 범위
def test_a_bare_class_is_confined_to_the_screen() -> None:
    assert scope_css(".card{padding:0}", "#page-capacity") == "#page-capacity .card{padding:0}"


def test_every_selector_in_a_list_is_scoped() -> None:
    """쉼표 목록에서 앞의 하나만 처리하면 나머지가 전역으로 샌다."""
    out = scope_css(".a, .b .c{color:red}", "#s")
    assert out == "#s .a, #s .b .c{color:red}"


def test_commas_inside_parentheses_are_not_split() -> None:
    out = scope_css(":is(.a, .b) span{color:red}", "#s")
    assert out == "#s :is(.a, .b) span{color:red}"


def test_media_queries_are_scoped_inside() -> None:
    out = scope_css("@media (max-width:800px){.card{gap:0}}", "#s")
    assert out == "@media (max-width:800px){#s .card{gap:0}}"


def test_nested_at_rules_are_handled() -> None:
    out = scope_css("@supports (display:grid){@media screen{.x{gap:0}}}", "#s")
    assert out == "@supports (display:grid){@media screen{#s .x{gap:0}}}"


def test_keyframes_are_left_alone() -> None:
    """0%/100% 는 선택자가 아니다. 범위를 붙이면 애니메이션이 깨진다."""
    css = "@keyframes cap-spin{0%{opacity:0}100%{opacity:1}}"
    assert scope_css(css, "#s") == css


@pytest.mark.parametrize("selector", ["html", "body", ":root", "*", "body.dark", "html > .x"])
def test_global_selectors_are_dropped(selector: str) -> None:
    """범위를 붙여도 의미가 없어 전역을 덮는 선택자는 렌더링에서 뺀다."""
    assert scope_css(f"{selector}{{color:red}}", "#s") == ""


def test_a_dropped_selector_does_not_take_the_others_with_it() -> None:
    out = scope_css("body, .mine{color:red}", "#s")
    assert out == "#s .mine{color:red}"


def test_comments_do_not_leak_into_selectors() -> None:
    """주석이 선택자 앞에 붙어 있으면 그대로 선택자에 섞여 들어간다."""
    out = scope_css("/* 담당자 메모 */\n.card{gap:0}", "#s")
    assert out.strip() == "#s .card{gap:0}"


def test_a_brace_inside_a_comment_does_not_break_parsing() -> None:
    out = scope_css("/* 예: .x { y } */ .card{gap:0}", "#s")
    assert out.strip() == "#s .card{gap:0}"


def test_a_comment_marker_inside_a_string_is_kept() -> None:
    out = scope_css('.card::after{content:"/* 진짜 아님 */"}', "#s")
    assert out == '#s .card::after{content:"/* 진짜 아님 */"}'


def test_only_style_blocks_are_touched() -> None:
    source = '<div class="card">.card{x}</div><style>.card{gap:0}</style>'
    out = scope_template(source, "#s")
    assert '<div class="card">.card{x}</div>' in out
    assert "<style>#s .card{gap:0}</style>" in out


def test_a_template_without_styles_is_unchanged() -> None:
    source = "<div>안녕</div>"
    assert scope_template(source, "#s") == source


def test_selectors_can_be_listed_for_checking() -> None:
    source = "<style>.a,.b{x:1}@media screen{.c{y:2}}</style>"
    assert sorted(iter_style_selectors(source)) == [".a", ".b", ".c"]


# ---------------------------------------------------------------- 로더 연결
@pytest.fixture()
def module_template(tmp_path: Path) -> Path:
    for role in ("page", "widget", "scripts"):
        directory = tmp_path / "modules" / "capacity"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{role}.html").write_text(
            f"<style>.card{{gap:{role}}}</style>", encoding="utf-8"
        )
    (tmp_path / "plain.html").write_text("<style>.card{gap:0}</style>", encoding="utf-8")
    return tmp_path


def _source(loader: ModuleScopedLoader, name: str) -> str:
    return loader.get_source(None, name)[0]


def test_the_loader_scopes_page_and_widget(module_template: Path) -> None:
    loader = ModuleScopedLoader(str(module_template), page_of={"capacity": "capacity_main"})
    assert _source(loader, "modules/capacity/page.html") == "<style>#page-capacity_main .card{gap:page}</style>"
    assert _source(loader, "modules/capacity/widget.html") == "<style>#module-widget-capacity .card{gap:widget}</style>"


def test_scripts_are_not_scoped(module_template: Path) -> None:
    """JS 에는 범위 개념이 없다. 스타일을 넣었다면 담당자 실수이므로 그대로 둔다."""
    loader = ModuleScopedLoader(str(module_template))
    assert _source(loader, "modules/capacity/scripts.html") == "<style>.card{gap:scripts}</style>"


def test_portal_own_templates_are_not_scoped(module_template: Path) -> None:
    """통합 웹 자신의 화면은 전역 스타일을 그대로 쓴다."""
    loader = ModuleScopedLoader(str(module_template))
    assert _source(loader, "plain.html") == "<style>.card{gap:0}</style>"


def test_the_page_key_falls_back_to_the_module_id(module_template: Path) -> None:
    loader = ModuleScopedLoader(str(module_template))
    assert "#page-capacity " in _source(loader, "modules/capacity/page.html")


# ---------------------------------------------------------------- 실제 렌더링
PROBE = "zz_style"


@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from application.settings import BASE_DIR

    directory = BASE_DIR / "templates" / "modules" / PROBE
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "page.html").write_text(
        f'<style>.card{{border:1px solid red}} body{{margin:9px}}</style>'
        f'<div class="page" id="page-{PROBE}">PROBE</div>',
        encoding="utf-8",
    )
    (tmp_path / "config" / "modules").mkdir(parents=True)
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    (tmp_path / "config" / "modules" / f"{PROBE}.yaml").write_text(
        "name: 스타일 검증\nbase_url: http://127.0.0.1:1\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    app = create_app()
    yield app, tmp_path
    reset_database_manager()
    shutil.rmtree(directory, ignore_errors=True)


def _client(app, role: str = "admin"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": role, "role": role, "name": role}
    return client


def test_module_styles_are_scoped_in_the_rendered_page(portal) -> None:
    app, _ = portal
    html = _client(app).get("/dashboard").get_data(as_text=True)
    assert f"#page-{PROBE} .card{{border:1px solid red}}" in html
    # 전역 선택자는 렌더링에서 빠진다.
    assert "body{margin:9px}" not in html


# ---------------------------------------------------------------- 재적용
def test_a_new_module_appears_without_a_restart(portal) -> None:
    app, root = portal
    client = _client(app)
    assert "새 대메뉴" not in client.get("/dashboard").get_data(as_text=True)

    (root / "config" / "modules" / "zz_added.yaml").write_text(
        "name: 새 대메뉴\nbase_url: http://127.0.0.1:1\n", encoding="utf-8"
    )
    result = client.post("/api/modules/reload").get_json()
    assert result["status"] == "SUCCESS"
    assert result["added"] == ["zz_added"]
    assert "새 대메뉴" in client.get("/dashboard").get_data(as_text=True)


def test_reload_reports_removals(portal) -> None:
    app, root = portal
    client = _client(app)
    (root / "config" / "modules" / f"{PROBE}.yaml").unlink()
    result = client.post("/api/modules/reload").get_json()
    assert result["removed"] == [PROBE]
    assert PROBE not in result["modules"]


def test_reload_is_admin_only(portal) -> None:
    app, _ = portal
    assert _client(app, "user").post("/api/modules/reload").status_code in (302, 403)


def test_a_broken_config_does_not_wipe_the_registry(portal) -> None:
    """재적용이 실패해도 돌던 대메뉴는 그대로 있어야 한다."""
    app, root = portal
    client = _client(app)
    (root / "config" / "modules" / "zz_broken.yaml").write_text("name: [\n", encoding="utf-8")
    response = client.post("/api/modules/reload")
    assert response.status_code == 400
    assert PROBE in {m.id for m in app.extensions["module_registry"].all()}
