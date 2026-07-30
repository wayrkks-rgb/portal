"""소메뉴와 통합 대시보드 위젯 크기.

대메뉴 하나에 화면이 여러 개인 경우가 많고(소메뉴), 통합 대시보드는 담당자마다
차지하는 크기가 달라야 한다(main.html 이 12 칸이면 각자 4 칸 식). 둘 다 설정으로
정해지고 통합 웹 화면 파일은 고치지 않는다.
"""

from __future__ import annotations

import pytest

from application.menu import PORTAL_MENU, SECTION_ORDER, build_sidebar
from application.modules.registry import (
    DASHBOARD_COLUMNS,
    DEFAULT_DASHBOARD_WIDTH,
    ModuleRegistry,
)


def _registry(*items) -> ModuleRegistry:
    return ModuleRegistry.from_config({"registry": list(items)})


# ---------------------------------------------------------------- 위젯 크기
def test_dashboard_size_defaults() -> None:
    module = _registry({"id": "capacity", "name": "용량"}).require("capacity")
    assert module.dashboard_width == DEFAULT_DASHBOARD_WIDTH
    assert module.dashboard_height == 1
    assert module.show_in_dashboard is True


def test_dashboard_size_is_read_from_config() -> None:
    module = _registry(
        {"id": "capacity", "name": "용량", "dashboard": {"width": 6, "height": 2}}
    ).require("capacity")
    assert (module.dashboard_width, module.dashboard_height) == (6, 2)
    assert module.public()["dashboard_width"] == 6


@pytest.mark.parametrize(
    "given,expected",
    [(0, 1), (-3, 1), (13, DASHBOARD_COLUMNS), (99, DASHBOARD_COLUMNS), ("abc", DEFAULT_DASHBOARD_WIDTH)],
)
def test_width_is_clamped_to_the_grid(given, expected) -> None:
    """격자 밖 값이 들어오면 화면이 깨진다. 설정 오류로 기동을 막지는 않는다."""
    module = _registry({"id": "c", "name": "c", "dashboard": {"width": given}}).require("c")
    assert module.dashboard_width == expected


def test_height_is_capped() -> None:
    module = _registry({"id": "c", "name": "c", "dashboard": {"height": 9}}).require("c")
    assert module.dashboard_height == 4


def test_a_module_can_stay_out_of_the_integrated_dashboard() -> None:
    module = _registry(
        {"id": "c", "name": "c", "dashboard": {"enabled": False}}
    ).require("c")
    assert module.show_in_dashboard is False


# ---------------------------------------------------------------- 소메뉴 정의
def test_children_are_parsed_with_defaults() -> None:
    module = _registry(
        {
            "id": "capacity",
            "name": "용량",
            "children": [
                {"id": "trend", "name": "추이"},
                {"id": "detail", "name": "상세", "icon": "🔍", "page": "capacity_detail"},
            ],
        }
    ).require("capacity")
    assert [(c.id, c.page, c.icon) for c in module.children] == [
        ("trend", "trend", "•"),
        ("detail", "capacity_detail", "🔍"),
    ]


def test_children_appear_in_public_payload() -> None:
    module = _registry(
        {"id": "c", "name": "c", "children": [{"id": "x", "name": "X"}]}
    ).require("c")
    assert module.public()["children"] == [
        {"id": "x", "name": "X", "icon": "•", "page": "x", "required_role": "user"}
    ]


def test_a_malformed_child_is_skipped_not_fatal() -> None:
    """소메뉴 하나가 잘못됐다고 대메뉴 전체가 사라지면 안 된다."""
    module = _registry(
        {
            "id": "c",
            "name": "c",
            "children": [{"id": "GOOD-ID!", "name": "잘못됨"}, {"id": "ok", "name": "정상"}],
        }
    ).require("c")
    assert [child.id for child in module.children] == ["ok"]


def test_duplicate_child_ids_are_rejected() -> None:
    """중복은 조용히 덮어쓰면 안 된다. 그 모듈만 건너뛴다."""
    registry = _registry(
        {"id": "c", "name": "c", "children": [{"id": "x", "name": "1"}, {"id": "x", "name": "2"}]}
    )
    assert registry.get("c") is None  # 정의 오류로 건너뛰었다


def test_children_are_filtered_by_role() -> None:
    module = _registry(
        {
            "id": "c",
            "name": "c",
            "children": [
                {"id": "open", "name": "공개"},
                {"id": "secret", "name": "관리자용", "required_role": "admin"},
                {"id": "off", "name": "중지", "enabled": False},
            ],
        }
    ).require("c")
    assert [child.id for child in module.menu_items("user")] == ["open"]
    assert [child.id for child in module.menu_items("admin")] == ["open", "secret"]


def test_an_unknown_child_role_is_rejected() -> None:
    registry = _registry(
        {"id": "c", "name": "c", "children": [{"id": "x", "name": "X", "required_role": "root"}]}
    )
    assert registry.get("c") is None


# ---------------------------------------------------------------- 사이드바 조립
def _sidebar(modules, role="user"):
    return {section["label"]: section["entries"] for section in build_sidebar(modules, role)}


def test_portal_own_screens_are_in_the_sidebar() -> None:
    sections = _sidebar([], "admin")
    assert [entry["page"] for entry in sections["운영"]] == ["dashboard", "report"]
    # 통합 웹 자신의 화면도 소메뉴를 갖는다.
    assert [child["page"] for child in sections["설정"][0]["children"]] == [
        "integration_settings",
        "admin_mapping",
        "admin_users",
    ]


def test_admin_only_sections_are_hidden_from_users() -> None:
    assert "설정" not in _sidebar([], "user")


def test_modules_join_the_section_named_in_their_config() -> None:
    """같은 menu_section 을 쓰면 통합 웹 화면과 한 묶음으로 합쳐진다."""
    modules = [
        {"id": "capacity", "name": "용량", "page": "capacity", "menu_section": "운영", "children": []},
        {"id": "backup", "name": "백업", "page": "backup", "menu_section": "연계 모듈", "children": []},
    ]
    sections = _sidebar(modules)
    assert [entry["page"] for entry in sections["운영"]] == ["dashboard", "report", "capacity"]
    assert [entry["page"] for entry in sections["연계 모듈"]] == ["backup"]


def test_section_order_is_stable_and_unknown_sections_go_last() -> None:
    modules = [
        {"id": "z", "name": "z", "page": "z", "menu_section": "기타", "children": []},
        {"id": "a", "name": "a", "page": "a", "menu_section": "연계 모듈", "children": []},
    ]
    labels = [section["label"] for section in build_sidebar(modules, "admin")]
    assert labels == ["운영", "연계 모듈", "설정", "기타"]
    assert [name for name in labels if name in SECTION_ORDER] == [
        name for name in SECTION_ORDER if name in labels
    ]


def test_module_children_carry_the_module_id() -> None:
    """소메뉴를 눌렀을 때 어느 모듈의 어느 메뉴인지 알아야 패널을 다시 받는다."""
    modules = [
        {
            "id": "capacity",
            "name": "용량",
            "page": "capacity",
            "menu_section": "연계 모듈",
            "children": [{"id": "trend", "name": "추이", "page": "capacity_trend", "required_role": "user"}],
        }
    ]
    child = _sidebar(modules)["연계 모듈"][0]["children"][0]
    assert (child["module_id"], child["child_id"], child["page"]) == ("capacity", "trend", "capacity_trend")


def test_hidden_modules_stay_out_of_the_menu() -> None:
    modules = [
        {"id": "c", "name": "c", "page": "c", "menu_section": "연계 모듈", "show_in_menu": False, "children": []}
    ]
    assert "연계 모듈" not in _sidebar(modules)


def test_submenu_role_filter_applies_to_modules_too() -> None:
    modules = [
        {
            "id": "c",
            "name": "c",
            "page": "c",
            "menu_section": "연계 모듈",
            "children": [
                {"id": "open", "name": "공개", "page": "open", "required_role": "user"},
                {"id": "secret", "name": "관리자용", "page": "secret", "required_role": "admin"},
            ],
        }
    ]
    assert [c["child_id"] for c in _sidebar(modules, "user")["연계 모듈"][0]["children"]] == ["open"]
    assert [c["child_id"] for c in _sidebar(modules, "admin")["연계 모듈"][0]["children"]] == [
        "open",
        "secret",
    ]


def test_portal_menu_pages_exist_as_templates() -> None:
    """메뉴가 가리키는 화면이 실제로 있어야 클릭했을 때 빈 화면이 되지 않는다."""
    from application.settings import BASE_DIR

    rendered = (BASE_DIR / "templates" / "main.html").read_text(encoding="utf-8")
    pages = {path.stem for path in (BASE_DIR / "templates" / "pages").glob("*.html")}
    for item in PORTAL_MENU:
        for page in [item["page"], *(child["page"] for child in item.get("children") or ())]:
            assert page in pages or f"page-{page}" in rendered, f"{page} 화면이 없습니다"


# ---------------------------------------------------------------- 패널 전달
def _aggregate(registry_items, module_ids=None):
    from application.modules.client import ModuleClient
    from application.modules.panels import PanelAggregator, clear_local_panels, register_local_panel

    registry = ModuleRegistry.from_config({"dashboard_budget_seconds": 5, "registry": registry_items})
    clear_local_panels()
    for item in registry_items:
        register_local_panel(item["id"], lambda user, params: {"title": "t"})
    try:
        return PanelAggregator(registry, ModuleClient(registry)).collect(
            {"username": "hong", "role": "admin"}, module_ids=module_ids
        )
    finally:
        clear_local_panels()


def test_panel_response_carries_the_widget_size() -> None:
    """화면이 span 을 주려면 크기가 응답에 있어야 한다."""
    result = _aggregate([{"id": "capacity", "name": "용량", "dashboard": {"width": 6, "height": 2}}])
    panel = result["panels"][0]
    assert (panel["dashboard_width"], panel["dashboard_height"]) == (6, 2)


def test_a_module_can_opt_out_of_the_integrated_dashboard() -> None:
    items = [
        {"id": "shown", "name": "보임"},
        {"id": "hidden", "name": "숨김", "dashboard": {"enabled": False}},
    ]
    assert [p["module_id"] for p in _aggregate(items)["panels"]] == ["shown"]
    # 대메뉴 화면에서 직접 요청하면 그대로 받는다.
    assert [p["module_id"] for p in _aggregate(items, ["hidden"])["panels"]] == ["hidden"]
