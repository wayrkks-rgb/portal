"""대메뉴(모듈)가 저장소에 두는 화면·코드 파일을 자동으로 찾는다.

담당자가 늘어날 때마다 ``main.html`` 의 include 목록과 ``create_app()`` 의 블루프린트
목록을 고치면, branch 를 병합할 때마다 그 두 줄에서 충돌한다. 그래서 목록을 코드에
두지 않고 **파일이 있으면 자동으로 쓰인다**.

화면 (통합 웹 저장소의 ``templates/`` 아래):

    templates/modules/<module_id>/page.html      대메뉴 화면. 없으면 공통 화면을 쓴다
    templates/modules/<module_id>/scripts.html   그 화면용 <script>
    templates/modules/<module_id>/widget.html    통합 대시보드에 넣을 축소 위젯

내부 모듈 코드 (담당 WAS 를 따로 두지 않고 통합 웹 안에서 도는 경우):

    application/modules_local/<module_id>/routes.py   ``bp`` (Blueprint) 를 노출
    application/modules_local/<module_id>/panel.py    ``panel(user, params)`` 를 노출

어느 것도 필수가 아니다. 있는 것만 쓴다.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from jinja2 import FileSystemLoader

from application.module_styles import scope_template

LOGGER = logging.getLogger(__name__)

TEMPLATE_ROLES = ("page", "scripts", "widget")
LOCAL_PACKAGE = "application.modules_local"

#: ``<style>`` 을 가둘 범위. scripts.html 은 대상이 아니다(JS 에는 범위가 없다).
STYLE_SCOPES = {"page": "#page-{page}", "widget": "#module-widget-{module_id}"}


class ModuleAssets:
    """한 모듈이 저장소에 둔 템플릿 경로. 값은 Jinja include 에 쓸 상대 경로다."""

    __slots__ = ("module_id", "page", "scripts", "widget")

    def __init__(self, module_id: str, page: str = "", scripts: str = "", widget: str = "") -> None:
        self.module_id = module_id
        self.page = page
        self.scripts = scripts
        self.widget = widget

    def get(self, role: str) -> str:
        return str(getattr(self, role, "") or "")

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        present = [role for role in TEMPLATE_ROLES if self.get(role)]
        return f"ModuleAssets({self.module_id!r}, {'+'.join(present) or 'none'})"


def discover_module_templates(templates_dir: Path) -> dict[str, ModuleAssets]:
    """``templates/modules/<id>/`` 에서 역할별 템플릿을 찾는다."""
    base = Path(templates_dir) / "modules"
    if not base.is_dir():
        return {}
    found: dict[str, ModuleAssets] = {}
    for directory in sorted(path for path in base.iterdir() if path.is_dir()):
        module_id = directory.name
        if module_id.startswith(("_", ".")):
            continue
        assets = ModuleAssets(module_id)
        for role in TEMPLATE_ROLES:
            if (directory / f"{role}.html").is_file():
                setattr(assets, role, f"modules/{module_id}/{role}.html")
        if any(assets.get(role) for role in TEMPLATE_ROLES):
            found[module_id] = assets
        else:
            LOGGER.warning(
                "templates/modules/%s 에 page.html / scripts.html / widget.html 이 없습니다.", module_id
            )
    return found


class ModuleScopedLoader(FileSystemLoader):
    """모듈 화면의 ``<style>`` 을 그 화면 안으로 가둔 뒤 Jinja 에 넘긴다.

    템플릿을 읽는 시점에 한 번만 처리된다(Jinja 가 컴파일 결과를 캐시한다).
    담당자가 `.card` 를 재정의해도 다른 팀 화면에는 영향이 없다.
    """

    def __init__(self, searchpath: Any, page_of: dict[str, str] | None = None) -> None:
        super().__init__(searchpath)
        #: 모듈 ID → page 키. 대메뉴 화면의 범위 선택자를 만들 때 쓴다.
        self.page_of: dict[str, str] = dict(page_of or {})

    def _scope_for(self, template: str) -> tuple[str, str] | None:
        parts = template.replace("\\", "/").split("/")
        if len(parts) != 3 or parts[0] != "modules":
            return None
        module_id, filename = parts[1], parts[2]
        role = filename[:-5] if filename.endswith(".html") else filename
        pattern = STYLE_SCOPES.get(role)
        if pattern is None:
            return None
        return module_id, pattern.format(
            module_id=module_id, page=self.page_of.get(module_id, module_id)
        )

    def get_source(self, environment: Any, template: str):  # type: ignore[override]
        source, filename, uptodate = super().get_source(environment, template)
        scope = self._scope_for(template)
        if scope is None or "<style" not in source.lower():
            return source, filename, uptodate
        return scope_template(source, scope[1], name=template), filename, uptodate


def _local_module_dirs(root: Path) -> list[Path]:
    base = Path(root) / "application" / "modules_local"
    if not base.is_dir():
        return []
    return sorted(
        path
        for path in base.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    )


def register_local_modules(app: Any, root: Path) -> dict[str, list[str]]:
    """``application/modules_local/<id>/`` 의 라우트와 패널을 등록한다.

    한 모듈의 import 실패로 통합 웹 전체가 기동하지 못하면 안 되므로 그 모듈만
    건너뛰고 로그를 남긴다.
    """
    from application.modules.panels import register_local_panel

    registered: dict[str, list[str]] = {"routes": [], "panels": []}
    for directory in _local_module_dirs(root):
        module_id = directory.name
        if (directory / "routes.py").is_file():
            try:
                routes = importlib.import_module(f"{LOCAL_PACKAGE}.{module_id}.routes")
                blueprint = getattr(routes, "bp", None)
                if blueprint is None:
                    raise AttributeError("routes.py 에 bp (Blueprint) 가 없습니다.")
                app.register_blueprint(blueprint)
                registered["routes"].append(module_id)
            except Exception:
                LOGGER.exception("내부 모듈 라우트 등록을 건너뜁니다: %s", module_id)
        if (directory / "panel.py").is_file():
            try:
                panel_module = importlib.import_module(f"{LOCAL_PACKAGE}.{module_id}.panel")
                provider = getattr(panel_module, "panel", None)
                if not callable(provider):
                    raise AttributeError("panel.py 에 panel(user, params) 함수가 없습니다.")
                register_local_panel(module_id, provider)
                registered["panels"].append(module_id)
            except Exception:
                LOGGER.exception("내부 모듈 패널 등록을 건너뜁니다: %s", module_id)
    if registered["routes"] or registered["panels"]:
        LOGGER.info(
            "내부 모듈 자동 등록: 라우트 %s · 패널 %s",
            registered["routes"] or "없음",
            registered["panels"] or "없음",
        )
    return registered
