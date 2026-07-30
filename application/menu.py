"""사이드바 구성.

메뉴를 ``base.html`` 안에 하드코딩하면 두 가지 문제가 생긴다.

1. 담당자가 대메뉴를 추가할 때마다 같은 파일을 고쳐 병합에서 충돌한다.
2. 통합 웹 자신의 화면과 모듈 화면이 서로 다른 방식으로 그려져, 소메뉴 같은 기능을
   양쪽에 따로 구현해야 한다.

그래서 메뉴는 여기 한 곳에서 만든다. 통합 웹의 기본 화면(``PORTAL_MENU``)과 모듈
레지스트리를 같은 형태로 합치고, ``base.html`` 은 그 결과만 반복해 그린다.

같은 ``section`` 이름을 쓰면 한 묶음으로 합쳐진다. 순서는 ``SECTION_ORDER`` 를 따르고
거기에 없는 이름은 뒤에 사전순으로 붙는다.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 사이드바 묶음 순서. 여기 없는 이름은 뒤에 사전순으로 붙는다.
SECTION_ORDER = ("운영", "자산 관리", "연계 모듈", "설정")

#: 통합 웹이 직접 갖고 있는 화면. 모듈이 아니므로 레지스트리에 두지 않는다.
#: ``children`` 은 소메뉴이고, ``role`` 이 admin 이면 관리자에게만 보인다.
PORTAL_MENU: tuple[dict[str, Any], ...] = (
    {"section": "운영", "page": "dashboard", "name": "통합 대시보드", "icon": "🏠"},
    {"section": "운영", "page": "report", "name": "보고서", "icon": "📝"},
    {
        "section": "설정",
        "page": "integration_settings",
        "name": "관리",
        "icon": "⚙️",
        "role": "admin",
        "children": (
            {"page": "integration_settings", "name": "연계 설정", "icon": "🔌"},
            {"page": "admin_mapping", "name": "메뉴·매핑", "icon": "🗂️"},
            {"page": "admin_users", "name": "사용자·권한", "icon": "👥"},
        ),
    },
)

_ROLE_RANK = {"user": 0, "admin": 1}


def _allowed(role: str, required: str | None) -> bool:
    actual = _ROLE_RANK.get(str(role or "user").lower(), 0)
    return actual >= _ROLE_RANK.get(str(required or "user").lower(), 0)


def _portal_entry(item: Mapping[str, Any], role: str) -> dict[str, Any] | None:
    if not _allowed(role, item.get("role")):
        return None
    children = [
        {
            "page": str(child["page"]),
            "name": str(child["name"]),
            "icon": str(child.get("icon") or "•"),
            "module_id": "",
            "child_id": "",
        }
        for child in item.get("children") or ()
        if _allowed(role, child.get("role"))
    ]
    return {
        "kind": "portal",
        "module_id": "",
        "page": str(item["page"]),
        "name": str(item["name"]),
        "icon": str(item.get("icon") or "🧩"),
        "children": children,
    }


def _module_entry(module: Mapping[str, Any], role: str) -> dict[str, Any]:
    """``ModuleDefinition.public()`` 결과를 메뉴 항목으로 바꾼다."""
    children = [
        {
            "page": str(child.get("page") or child.get("id")),
            "name": str(child.get("name") or child.get("id")),
            "icon": str(child.get("icon") or "•"),
            # 소메뉴를 눌렀을 때 어느 모듈의 어느 메뉴인지 알아야 패널을 다시 받는다.
            "module_id": str(module.get("id")),
            "child_id": str(child.get("id")),
        }
        for child in module.get("children") or []
        if _allowed(role, child.get("required_role"))
    ]
    return {
        "kind": "module",
        "module_id": str(module.get("id")),
        "page": str(module.get("page") or module.get("id")),
        "name": str(module.get("name") or module.get("id")),
        "icon": str(module.get("icon") or "🧩"),
        "children": children,
    }


def build_sidebar(portal_modules: Iterable[Mapping[str, Any]], role: str = "user") -> list[dict[str, Any]]:
    """묶음 목록을 돌려준다: ``[{"label": ..., "entries": [...]}, ...]``

    ``portal_modules`` 는 이미 권한으로 걸러진 목록이다(``inject_modules``).
    여기서는 등급으로 소메뉴만 더 걸러낸다.
    """
    sections: dict[str, list[dict[str, Any]]] = {}

    for item in PORTAL_MENU:
        entry = _portal_entry(item, role)
        if entry is not None:
            sections.setdefault(str(item["section"]), []).append(entry)

    for module in portal_modules or []:
        if not module.get("show_in_menu", True):
            continue
        label = str(module.get("menu_section") or "연계 모듈")
        sections.setdefault(label, []).append(_module_entry(module, role))

    known = [name for name in SECTION_ORDER if name in sections]
    extra = sorted(name for name in sections if name not in SECTION_ORDER)
    # 키 이름이 "items" 면 Jinja 에서 dict.items 메서드로 해석된다.
    return [{"label": name, "entries": sections[name]} for name in known + extra if sections[name]]
