"""화면의 칸(id)과 버튼(onclick)이 스크립트와 맞는지 확인한다.

화면과 스크립트는 문자열로만 이어져 있다. ``getElementById('weekly-detail-body')``
처럼 더 이상 없는 칸을 부르면 ``null`` 이 돌아오고, 그다음 줄에서 조용히 터진다 --
표가 비어 있을 뿐 오류 메시지는 어디에도 안 뜬다. 버튼의 ``onclick="exportX()"``
도 마찬가지로, 함수 이름을 고치면 누르는 순간까지 아무도 모른다.

파이썬도 Jinja 도 이 어긋남을 막아주지 않으므로 여기서 잡는다. 담당자가 자기
화면을 추가할 때도 같은 규칙이다.
"""

from __future__ import annotations

import re
from pathlib import Path

from application.settings import BASE_DIR

TEMPLATES = BASE_DIR / "templates"

#: ``id="..."``. ``{{ }}`` · ``${ }`` 가 든 것은 실행 시점에 정해지므로 뺀다.
_ID = re.compile(r'\bid="([^"{}$]+)"')
#: ``getElementById('...')``. 같은 이유로 고정 문자열만 본다.
_GET_BY_ID = re.compile(r"""getElementById\(\s*['"]([^'"{}$]+)['"]""")
_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
_HANDLER = re.compile(r'\bon(?:click|change|input|submit|keyup)\s*=\s*"([^"]*)"')
#: 문장 맨 앞의 호출. ``a.b()`` 같은 메서드 호출은 앞의 점으로 걸러낸다.
_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
#: 함수가 아니라 문법·내장인 것들.
_NOT_OURS = {"if", "for", "while", "return", "alert", "confirm", "String", "Number", "Boolean"}


def _templates() -> list[Path]:
    # 담당자 모듈 화면은 자기 WAS 가 그린다. 통합 웹 스크립트와 짝이 아니다.
    return [p for p in sorted(TEMPLATES.rglob("*.html")) if "modules" not in p.parts]


def _all_ids() -> set[str]:
    ids: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        ids.update(_ID.findall(path.read_text(encoding="utf-8")))
    return ids


def _all_functions() -> set[str]:
    names: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        names.update(_FUNCTION.findall(path.read_text(encoding="utf-8")))
    return names


def test_scripts_only_touch_cells_that_exist() -> None:
    ids = _all_ids()
    missing: dict[str, list[str]] = {}
    for path in sorted((TEMPLATES / "partials" / "js").rglob("*.html")):
        for name in _GET_BY_ID.findall(path.read_text(encoding="utf-8")):
            if name not in ids:
                missing.setdefault(name, []).append(str(path.relative_to(BASE_DIR)))
    assert not missing, "화면에 없는 칸을 부릅니다: " + "; ".join(
        f"{name} ({', '.join(files)})" for name, files in sorted(missing.items())
    )


def test_buttons_call_functions_that_exist() -> None:
    functions = _all_functions()
    missing: dict[str, list[str]] = {}
    for path in _templates():
        for code in _HANDLER.findall(path.read_text(encoding="utf-8")):
            for statement in code.split(";"):
                match = _CALL.search(statement)
                if match is None:
                    continue
                name = match.group(1)
                if name in _NOT_OURS or name in functions:
                    continue
                missing.setdefault(name, []).append(str(path.relative_to(BASE_DIR)))
    assert not missing, "없는 함수를 부르는 버튼이 있습니다: " + "; ".join(
        f"{name} ({', '.join(sorted(set(files)))})" for name, files in sorted(missing.items())
    )


# ── 증감 현황(서버 단위) 표가 두 화면에 똑같이 있는지 ──────────────────────
# 일간·주간 둘 다 같은 묶음 표를 쓴다. 한쪽만 고쳐 두면 다른 쪽이 조용히 빈다.
DIGEST_SCREENS = {
    "daily_check": "daily-digest",
    "weekly_check": "weekly-digest",
}


def test_both_check_screens_have_the_digest_table() -> None:
    for page, prefix in DIGEST_SCREENS.items():
        text = (TEMPLATES / "pages" / f"{page}.html").read_text(encoding="utf-8")
        assert f'id="{prefix}-table"' in text, f"{page} 에 묶음 표가 없습니다"


def test_digest_controls_exist_on_both_screens() -> None:
    """걸러내기 수단이 한쪽에만 생기는 것을 막는다."""
    for page in DIGEST_SCREENS:
        text = (TEMPLATES / "pages" / f"{page}.html").read_text(encoding="utf-8")
        ids = set(_ID.findall(text))
        head = page.split("_")[0]                      # daily / weekly
        prefix = f"{head}-digest" if head == "daily" else f"{head}-detail"
        for suffix in ("pending", "filter", "source", "search"):
            assert f"{prefix}-{suffix}" in ids, f"{page} 에 {prefix}-{suffix} 가 없습니다"
        assert "exportDailyDigest()" in text or "exportWeeklyDigest()" in text, (
            f"{page} 에 엑셀 내려받기 버튼이 없습니다"
        )


def test_digest_renderer_is_shared() -> None:
    """두 화면이 각자 표를 그리면 또 어긋난다. 그리는 곳은 하나여야 한다."""
    common = (TEMPLATES / "partials" / "js" / "common.html").read_text(encoding="utf-8")
    checks = (TEMPLATES / "partials" / "js" / "checks.html").read_text(encoding="utf-8")
    for name in ("renderChangeDigest", "filterDigestRows", "loadChangeDigest",
                 "exportChangeDigest"):
        assert f"function {name}" in common, f"{name} 은 공용 스크립트에 있어야 합니다"
        assert f"function {name}" not in checks, f"{name} 을 화면 스크립트가 또 만들었습니다"
    for table in ("daily-digest-table", "weekly-digest-table"):
        assert f"renderChangeDigest('{table}'" in checks, f"{table} 을 공용 renderer 로 안 그립니다"


#: 스크립트가 문자열로 넘기는 핸들러. ``{onShow: 'showDailyPending()'}`` 처럼
#: 나중에 ``onclick`` 으로 들어가므로 속성 검사로는 안 잡힌다.
_STRING_HANDLER = re.compile(r"""\bon(?:Show|Export|Click)\s*:\s*['"]([A-Za-z_$][\w$]*)\(""")


def test_handlers_passed_as_strings_exist_too() -> None:
    functions = _all_functions()
    missing: dict[str, list[str]] = {}
    for path in sorted((TEMPLATES / "partials" / "js").rglob("*.html")):
        for name in _STRING_HANDLER.findall(path.read_text(encoding="utf-8")):
            if name not in functions:
                missing.setdefault(name, []).append(str(path.relative_to(BASE_DIR)))
    assert not missing, "문자열로 넘긴 핸들러가 없습니다: " + "; ".join(
        f"{name} ({', '.join(files)})" for name, files in sorted(missing.items())
    )


def test_the_pending_notice_has_somewhere_to_go_on_both_screens() -> None:
    """ITSM 미반영이 있으면 표 위에 띄우고, 거기서 바로 걸러보거나 내려받게 한다."""
    checks = (TEMPLATES / "partials" / "js" / "checks.html").read_text(encoding="utf-8")
    for prefix in ("daily", "weekly"):
        page = (TEMPLATES / "pages" / f"{prefix}_check.html").read_text(encoding="utf-8")
        assert f'id="{prefix}-digest-pending-notice"' in page, f"{prefix} 에 알림 자리가 없습니다"
        assert f"renderDigestPendingNotice('{prefix}-digest-pending-notice'" in checks


def test_daily_screen_does_not_show_the_group_event_twice() -> None:
    """ITSM 묶음 이벤트는 항목별 이벤트와 같은 내용이다. 표에서 빼야 한다."""
    checks = (TEMPLATES / "partials" / "js" / "checks.html").read_text(encoding="utf-8")
    assert "event_type !== 'ITSM_ASSET_UPDATED'" in checks
