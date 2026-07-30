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


class ModuleConfigError(ValueError):
    pass


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

    @property
    def is_local(self) -> bool:
        """내부 모듈이면 HTTP 를 거치지 않는다."""
        return not self.base_url

    def visible_to(self, role: str) -> bool:
        required = _ROLE_RANK.get(str(self.required_role or "user").lower(), 0)
        actual = _ROLE_RANK.get(str(role or "user").lower(), 0)
        return self.enabled and actual >= required

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

    @classmethod
    def from_config(cls, modules_cfg: Mapping[str, Any] | None) -> "ModuleRegistry":
        settings = dict(modules_cfg or {})
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
        return cls(built, settings)

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
