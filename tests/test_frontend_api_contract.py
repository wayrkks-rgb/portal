"""화면이 부르는 주소가 서버에 실제로 있는지 확인한다.

화면은 ``fetch('/api/...')`` 로 서버를 부른다. 그 주소가 서버에 없으면 404 가
돌아오고, 화면은 오류 없이 "조회 중..." 에서 멈춘다. 파이썬도 Jinja 도 이 어긋남을
막아 주지 않는다 -- 라우트를 옮기다 지워도, 주소를 오타 내도 조용하다.

그래서 여기서 잡는다. 템플릿에 적힌 모든 ``/api/`` 주소를 모아 Flask 의 라우팅
표에 물어본다. 담당자가 자기 화면을 추가할 때도 같은 규칙이다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from application.settings import BASE_DIR

# 따옴표 세 종류(' " `) 안에 든 /api/ 로 시작하는 주소.
_URL = re.compile(r"""['"`](/api/[^'"`\s]*)['"`]""")

# `${...}` 와 `{{ ... }}` 는 실행 시점에 값이 채워진다. 값을 알 수 없으므로
# "이 칸은 무엇이든 온다" 는 표시로 바꾼다.
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}")
_ANY = "\x00"
#: Flask 라우트의 <int:id> 같은 자리.
_CONVERTER = re.compile(r"<[^>]*>")


def _template_urls() -> dict[str, list[str]]:
    """{주소: [적힌 파일...]}"""
    found: dict[str, list[str]] = {}
    for path in sorted((BASE_DIR / "templates").rglob("*.html")):
        # 담당자 모듈 화면은 자기 WAS 를 부르므로 통합 웹 라우팅에 없을 수 있다.
        if "modules" in path.parts:
            continue
        for raw in _URL.findall(path.read_text(encoding="utf-8")):
            url = _PLACEHOLDER.sub(_ANY, raw).split("?")[0].rstrip("/") or "/"
            found.setdefault(url, []).append(str(path.relative_to(BASE_DIR)))
    return found


def _matches(url: str, rule: str) -> bool:
    """칸 단위로 맞춰 본다.

    값이 실행 시점에 정해지는 칸(``${source}``)과 라우트의 자리(``<report_type>``)는
    무엇이든 맞는 것으로 본다. ``/collect/${source}`` 는 ``/collect/itsm`` 과
    ``/collect/vcenter`` 중 하나이므로 둘 중 하나만 있으면 통과다.
    """
    left, right = url.strip("/").split("/"), rule.strip("/").split("/")
    if len(left) != len(right):
        return False
    return all(
        mine == theirs or _ANY in mine or _CONVERTER.fullmatch(theirs)
        for mine, theirs in zip(left, right)
    )


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


def test_the_scan_actually_finds_the_known_calls() -> None:
    """정규식이 아무것도 못 찾으면 아래 테스트가 항상 통과한다."""
    urls = set(_template_urls())
    assert {"/api/asset-sync/dashboard", "/api/asset-sync/server-status"} <= urls


def test_every_address_the_screens_call_exists_on_the_server(portal) -> None:
    rules = [rule.rule for rule in portal.url_map.iter_rules()]
    missing = {
        url.replace(_ANY, "{...}"): sources
        for url, sources in _template_urls().items()
        if not any(_matches(url, rule) for rule in rules)
    }
    assert not missing, (
        "화면이 부르는데 서버에 없는 주소입니다. 그대로 두면 404 가 나고 화면은 "
        f"조용히 멈춥니다: {missing}"
    )
