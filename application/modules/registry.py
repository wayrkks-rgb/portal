"""대메뉴(모듈) 레지스트리.

통합 웹은 대메뉴마다 담당 WAS 를 하나씩 갖는다. 그 목록을 코드가 아니라 설정에
두어야 대메뉴를 추가할 때 통합 웹 소스를 고치지 않는다.

``base_url`` 이 비어 있으면 통합 웹 안에 있는 내부 모듈이다. 나중에 그 기능을
별도 WAS 로 분리하더라도 ``base_url`` 만 채우면 되고 호출하는 쪽은 바뀌지 않는다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

LOGGER = logging.getLogger(__name__)

_MODULE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROLE_RANK = {"user": 0, "admin": 1}
_ACCESS_MODES = ("role", "explicit")
DEFAULT_ALLOWED_PREFIXES = ("/api/",)

# 통합 대시보드는 12칸 격자다. main.html 이 100 폭이라면 width 4 는 대략 33% 다.
DASHBOARD_COLUMNS = 12
DEFAULT_DASHBOARD_WIDTH = 4
DEFAULT_DASHBOARD_HEIGHT = 1
MAX_DASHBOARD_HEIGHT = 4


class ModuleConfigError(ValueError):
    pass


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass(slots=True)
class ModuleMenuItem:
    """소메뉴 한 칸.

    대메뉴 하나에 화면이 여러 개인 경우가 많다. 담당자가 ``children`` 을 적으면
    사이드바가 2단으로 그려지고, 통합 웹 화면 파일은 고치지 않는다.
    """

    id: str
    name: str
    icon: str = "•"
    page: str = ""
    required_role: str = "user"
    enabled: bool = True

    def visible_to(self, role: str) -> bool:
        required = _ROLE_RANK.get(str(self.required_role or "user").lower(), 0)
        actual = _ROLE_RANK.get(str(role or "user").lower(), 0)
        return self.enabled and actual >= required

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "page": self.page or self.id,
            "required_role": self.required_role,
        }


@dataclass(slots=True)
class ModuleDefinition:
    id: str
    name: str
    icon: str = "🧩"
    base_url: str = ""
    enabled: bool = True
    required_role: str = "user"
    menu_section: str = "운영"
    page: str = ""
    health_path: str = "/api/health"
    panel_path: str = "/api/dashboard/panel"
    timeout_seconds: float | None = None
    allowed_prefixes: tuple[str, ...] = field(default=DEFAULT_ALLOWED_PREFIXES)
    show_in_menu: bool = True
    # role: required_role 로 판정(기본) · explicit: 명시 부여가 있어야 접근 가능
    access: str = "role"
    # 통합 대시보드에서 차지할 칸 수(12칸 기준). 담당자별 축소 위젯 크기다.
    dashboard_width: int = DEFAULT_DASHBOARD_WIDTH
    dashboard_height: int = DEFAULT_DASHBOARD_HEIGHT
    show_in_dashboard: bool = True
    children: tuple[ModuleMenuItem, ...] = field(default=())

    @property
    def is_local(self) -> bool:
        """내부 모듈이면 HTTP 를 거치지 않는다."""
        return not self.base_url

    def visible_to(self, role: str) -> bool:
        required = _ROLE_RANK.get(str(self.required_role or "user").lower(), 0)
        actual = _ROLE_RANK.get(str(role or "user").lower(), 0)
        return self.enabled and actual >= required

    def menu_items(self, role: str) -> list[ModuleMenuItem]:
        """등급으로 걸러낸 소메뉴. 대메뉴 권한은 호출하는 쪽이 이미 판정했다."""
        return [child for child in self.children if child.visible_to(role)]

    def public(self) -> dict[str, Any]:
        """화면에 넘길 정보. base_url 같은 내부 주소는 노출하지 않는다."""
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "page": self.page or self.id,
            "menu_section": self.menu_section,
            "required_role": self.required_role,
            "location": "LOCAL" if self.is_local else "REMOTE",
            "show_in_menu": self.show_in_menu,
            "access": self.access,
            "dashboard_width": self.dashboard_width,
            "dashboard_height": self.dashboard_height,
            "show_in_dashboard": self.show_in_dashboard,
            "children": [child.public() for child in self.children],
        }

    def resolve(self, path: str) -> str:
        """모듈 기준 경로를 절대 URL 로 바꾼다.

        허용 접두어 검사는 통합 웹이 임의 경로로 가는 통로가 되지 않게 하기 위한 것이다.
        """
        if self.is_local:
            raise ModuleConfigError(f"내부 모듈은 HTTP 호출 대상이 아닙니다: {self.id}")
        candidate = "/" + str(path or "").lstrip("/")
        if ".." in candidate:
            raise ModuleConfigError(f"허용되지 않는 경로입니다: {path}")
        if not any(candidate.startswith(prefix) for prefix in self.allowed_prefixes):
            allowed = ", ".join(self.allowed_prefixes)
            raise ModuleConfigError(f"{self.id} 모듈에서 허용된 경로가 아닙니다: {candidate} (허용: {allowed})")
        return self.base_url.rstrip("/") + candidate


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "y", "yes", "on"}


def _build_children(module_id: str, raw: Any) -> tuple[ModuleMenuItem, ...]:
    """소메뉴 목록을 만든다. 항목 하나가 잘못돼도 대메뉴는 살린다."""
    children: list[ModuleMenuItem] = []
    seen: set[str] = set()
    for entry in raw or []:
        if not isinstance(entry, Mapping):
            LOGGER.warning("%s 소메뉴 정의가 객체가 아니어서 건너뜁니다: %r", module_id, entry)
            continue
        child_id = str(entry.get("id") or "").strip().lower()
        if not _MODULE_ID.fullmatch(child_id):
            LOGGER.warning("%s 소메뉴 id 형식이 올바르지 않아 건너뜁니다: %r", module_id, entry.get("id"))
            continue
        if child_id in seen:
            raise ModuleConfigError(f"{module_id} 소메뉴 id 가 중복되었습니다: {child_id}")
        seen.add(child_id)
        role = str(entry.get("required_role") or "user").strip().lower()
        if role not in _ROLE_RANK:
            raise ModuleConfigError(
                f"{module_id}.{child_id} required_role 은 {', '.join(_ROLE_RANK)} 중 하나여야 합니다: {role}"
            )
        children.append(
            ModuleMenuItem(
                id=child_id,
                name=str(entry.get("name") or child_id),
                icon=str(entry.get("icon") or "•"),
                # 기본 page 는 소메뉴 id 다. 통합 웹이 이미 갖고 있는 화면을 소메뉴로
                # 붙일 때 page 만 따로 적으면 된다.
                page=str(entry.get("page") or child_id),
                required_role=role,
                enabled=_as_bool(entry.get("enabled"), True),
            )
        )
    return tuple(children)


def _build(item: Mapping[str, Any]) -> ModuleDefinition:
    module_id = str(item.get("id") or "").strip().lower()
    if not _MODULE_ID.fullmatch(module_id):
        raise ModuleConfigError(f"모듈 id 형식이 올바르지 않습니다: {item.get('id')!r} (소문자/숫자/밑줄)")
    base_url = str(item.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ModuleConfigError(f"{module_id} base_url 은 http:// 또는 https:// 로 시작해야 합니다: {base_url}")
    role = str(item.get("required_role") or "user").strip().lower()
    if role not in _ROLE_RANK:
        raise ModuleConfigError(f"{module_id} required_role 은 {', '.join(_ROLE_RANK)} 중 하나여야 합니다: {role}")
    prefixes = tuple(
        "/" + str(prefix).strip().lstrip("/")
        for prefix in (item.get("allowed_prefixes") or DEFAULT_ALLOWED_PREFIXES)
        if str(prefix).strip()
    )
    access = str(item.get("access") or "role").strip().lower()
    if access not in _ACCESS_MODES:
        raise ModuleConfigError(f"{module_id} access 는 {', '.join(_ACCESS_MODES)} 중 하나여야 합니다: {access}")
    timeout = item.get("timeout_seconds")
    dashboard = item.get("dashboard") if isinstance(item.get("dashboard"), Mapping) else {}
    return ModuleDefinition(
        id=module_id,
        name=str(item.get("name") or module_id),
        icon=str(item.get("icon") or "🧩"),
        base_url=base_url,
        enabled=_as_bool(item.get("enabled"), True),
        required_role=role,
        menu_section=str(item.get("menu_section") or "운영"),
        page=str(item.get("page") or module_id),
        health_path=str(item.get("health_path") or "/api/health"),
        panel_path=str(item.get("panel_path") or "/api/dashboard/panel"),
        timeout_seconds=float(timeout) if timeout not in (None, "") else None,
        allowed_prefixes=prefixes or DEFAULT_ALLOWED_PREFIXES,
        show_in_menu=_as_bool(item.get("show_in_menu"), True),
        access=access,
        dashboard_width=_clamp(
            dashboard.get("width"), DEFAULT_DASHBOARD_WIDTH, 1, DASHBOARD_COLUMNS
        ),
        dashboard_height=_clamp(
            dashboard.get("height"), DEFAULT_DASHBOARD_HEIGHT, 1, MAX_DASHBOARD_HEIGHT
        ),
        show_in_dashboard=_as_bool(dashboard.get("enabled"), True),
        children=_build_children(module_id, item.get("children")),
    )


class ModuleRegistry:
    """설정에서 읽은 모듈 목록. 잘못된 항목 하나가 전체 기동을 막지 않는다."""

    def __init__(self, modules: Iterable[ModuleDefinition], settings: Mapping[str, Any] | None = None) -> None:
        self._modules: dict[str, ModuleDefinition] = {}
        for module in modules:
            if module.id in self._modules:
                raise ModuleConfigError(f"모듈 id 가 중복되었습니다: {module.id}")
            self._modules[module.id] = module
        self.settings: dict[str, Any] = dict(settings or {})

    @staticmethod
    def _build_all(settings: Mapping[str, Any]) -> list[ModuleDefinition]:
        built: list[ModuleDefinition] = []
        for item in settings.get("registry") or []:
            if not isinstance(item, Mapping):
                LOGGER.warning("모듈 정의가 객체가 아니어서 건너뜁니다: %r", item)
                continue
            try:
                built.append(_build(item))
            except ModuleConfigError:
                # 한 모듈의 설정 오류로 통합 웹 전체가 기동하지 못하면 안 된다.
                LOGGER.exception("모듈 정의를 건너뜁니다: %r", item.get("id"))
        return built

    @classmethod
    def from_config(cls, modules_cfg: Mapping[str, Any] | None) -> "ModuleRegistry":
        settings = dict(modules_cfg or {})
        return cls(cls._build_all(settings), settings)

    def reload_from_config(self, modules_cfg: Mapping[str, Any] | None) -> list[str]:
        """같은 객체의 내용만 바꾼다.

        클라이언트·블루프린트·컨텍스트 프로세서가 이 인스턴스를 참조하고 있으므로
        새 객체로 갈아끼울 수 없다. 내용을 교체해야 재기동 없이 반영된다.
        새 정의를 다 만든 뒤에 바꾸므로, 도중에 실패해도 기존 목록은 그대로다.
        """
        settings = dict(modules_cfg or {})
        rebuilt: dict[str, ModuleDefinition] = {}
        for module in self._build_all(settings):
            if module.id in rebuilt:
                raise ModuleConfigError(f"모듈 id 가 중복되었습니다: {module.id}")
            rebuilt[module.id] = module
        self._modules = rebuilt
        self.settings = settings
        return list(rebuilt)

    def __len__(self) -> int:
        return len(self._modules)

    def all(self) -> list[ModuleDefinition]:
        return list(self._modules.values())

    def enabled(self) -> list[ModuleDefinition]:
        return [module for module in self._modules.values() if module.enabled]

    def visible(self, role: str) -> list[ModuleDefinition]:
        """등급만 보고 판정한다. 명시 부여를 반영하려면 accessible() 을 쓴다."""
        return [
            module
            for module in self._modules.values()
            if module.access == "role" and module.visible_to(role)
        ]

    def accessible(self, user: Any, granted: Any = None) -> list[tuple[ModuleDefinition, str]]:
        """사용자가 접근할 수 있는 모듈과 실효 권한을 함께 돌려준다."""
        from application.permissions import PERMISSION_NONE, resolve_permission

        result: list[tuple[ModuleDefinition, str]] = []
        for module in self._modules.values():
            permission = resolve_permission(module, user, granted)
            if permission != PERMISSION_NONE:
                result.append((module, permission))
        return result

    def get(self, module_id: str) -> ModuleDefinition | None:
        return self._modules.get(str(module_id or "").strip().lower())

    def require(self, module_id: str) -> ModuleDefinition:
        module = self.get(module_id)
        if module is None:
            raise ModuleConfigError(f"등록되지 않은 모듈입니다: {module_id}")
        if not module.enabled:
            raise ModuleConfigError(f"사용 중지된 모듈입니다: {module_id}")
        return module

    # -- 공통 설정값 -------------------------------------------------------
    @property
    def request_timeout(self) -> float:
        return float(self.settings.get("request_timeout_seconds") or 5)

    @property
    def retry_count(self) -> int:
        return max(0, int(self.settings.get("retry_count") or 0))

    @property
    def dashboard_budget(self) -> float:
        return float(self.settings.get("dashboard_budget_seconds") or 12)

    @property
    def max_response_bytes(self) -> int:
        return int(self.settings.get("max_response_bytes") or 4 * 1024 * 1024)

    @property
    def verify_tls(self) -> bool:
        return _as_bool(self.settings.get("verify_tls"), True)

    @property
    def auth(self) -> dict[str, Any]:
        return dict(self.settings.get("auth") or {})
