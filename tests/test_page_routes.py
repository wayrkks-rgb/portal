"""서버가 그리는 화면이 실제로 존재하는지 확인한다.

``render_template('main.html', page='x')`` 의 ``page`` 값은 화면 div 의 id 와 같아야
한다(``id="page-x"``). 어긋나면 오류 없이 **빈 화면**이 뜬다 -- 파이썬도 Jinja 도
막아주지 않으므로 여기서 잡는다. 담당자가 자기 화면을 추가할 때도 같은 규칙이다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from application.settings import BASE_DIR

# render_template('main.html', ..., page='x')  ―  따옴표 종류는 상관없다.
_RENDER = re.compile(
    r"""render_template\(\s*['"]main\.html['"][^)]*?\bpage\s*=\s*['"](?P<page>[a-z_]+)['"]""",
    re.IGNORECASE | re.DOTALL,
)
_PAGE_DIV = re.compile(r'id="page-([a-z_]+)"')


def _screen_ids() -> set[str]:
    ids: set[str] = set()
    for path in (BASE_DIR / "templates").rglob("*.html"):
        ids.update(_PAGE_DIV.findall(path.read_text(encoding="utf-8")))
    return ids


def _rendered_pages() -> dict[str, list[str]]:
    """{page 값: [소스 파일...]}"""
    found: dict[str, list[str]] = {}
    for path in sorted((BASE_DIR / "application").rglob("*.py")) + sorted(
        (BASE_DIR / "asset_sync").rglob("*.py")
    ):
        text = path.read_text(encoding="utf-8")
        for match in _RENDER.finditer(text):
            found.setdefault(match.group("page"), []).append(str(path.relative_to(BASE_DIR)))
    return found


def test_every_rendered_page_has_a_screen() -> None:
    screens = _screen_ids()
    missing = {
        page: sources for page, sources in _rendered_pages().items() if page not in screens
    }
    assert not missing, (
        "page 값에 해당하는 화면(id=\"page-...\")이 없습니다. 그대로 두면 빈 화면이 뜹니다: "
        f"{missing}"
    )


def test_the_scan_actually_finds_the_known_routes() -> None:
    """정규식이 아무것도 못 찾으면 위 테스트가 항상 통과한다."""
    pages = _rendered_pages()
    assert {"dashboard", "report", "compare", "history"} <= set(pages)


@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n", encoding="utf-8"
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


def _client(app, role: str = "admin"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": role, "role": role, "name": role}
    return client


@pytest.mark.parametrize(
    "url,expected",
    [
        ("/dashboard", "page-dashboard"),
        ("/report", "page-report"),
        ("/compare", "page-compare"),
        ("/history", "page-history"),
        ("/admin", "page-admin_mapping"),
        ("/integration-settings", "page-integration_settings"),
    ],
)
def test_direct_urls_open_a_visible_screen(portal, url: str, expected: str) -> None:
    """주소를 직접 입력해 들어와도 화면이 보여야 한다."""
    html = _client(portal).get(url, follow_redirects=True).get_data(as_text=True)
    active = re.findall(r'<div class="page\s+active"\s+id="(page-[a-z_]+)"', html)
    assert active == [expected], f"{url} 에서 열린 화면: {active or '없음'}"


def test_a_non_admin_is_redirected_away_from_admin(portal) -> None:
    response = _client(portal, "user").get("/admin")
    assert response.status_code == 302
