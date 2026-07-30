"""모듈 화면의 CSS 를 그 모듈 안으로 가둔다.

모든 화면이 ``base.html`` 의 전역 스타일을 공유한다. 담당자가 자기 화면에서
``.card { padding: 0 }`` 같은 규칙을 쓰면 **다른 팀 화면까지 바뀐다.** 규칙을 문서로
알리는 것만으로는 막히지 않으므로, 모듈 템플릿을 읽을 때 선택자 앞에 그 모듈의
범위를 붙인다.

    .card { ... }                 →  #page-capacity .card { ... }
    .capacity-chart, .x { ... }   →  #page-capacity .capacity-chart, #page-capacity .x { ... }
    @media (max-width:800px){...} →  안쪽 선택자에 같은 처리를 한다

범위는 화면 파일마다 다르다.

    modules/<id>/page.html    → ``#page-<page 키>``   (대메뉴 화면 전체)
    modules/<id>/widget.html  → ``#module-widget-<id>`` (통합 대시보드 위젯)

``html``, ``body``, ``:root`` 처럼 범위를 붙여도 의미가 없는 선택자는 그대로 두면
전역을 덮으므로 렌더링에서 제외하고 로그를 남긴다. CI 는 이것을 실패로 본다
(``scripts/check_module_contract.py``).

``scripts.html`` 은 건드리지 않는다. 자바스크립트에는 이런 범위 개념이 없다.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

LOGGER = logging.getLogger(__name__)

#: 범위를 붙여도 의미가 없어 전역을 덮게 되는 선택자.
GLOBAL_SELECTORS = frozenset({"html", "body", ":root", "*", ":host"})

_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.IGNORECASE | re.DOTALL)
#: 중첩되지 않은 선언 블록 하나. 선택자 부분과 본문을 나눈다.
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_AT_BLOCK = re.compile(r"(@[a-zA-Z-]+[^{]*)\{", re.DOTALL)
#: 안쪽에 다시 규칙이 들어가는 at-rule. 이 안쪽만 재귀 처리한다.
_NESTING_AT_RULES = ("@media", "@supports", "@container", "@layer")


def strip_comments(css: str) -> str:
    """``/* ... */`` 를 지운다.

    주석은 선택자 바로 앞에 붙어 있는 경우가 많아 그대로 두면 선택자에 섞여 들어가고,
    주석 안의 ``{`` ``}`` 가 블록 구분을 망가뜨린다. 문자열 리터럴 안은 건드리지 않는다.
    """
    out: list[str] = []
    index = 0
    length = len(css)
    while index < length:
        char = css[index]
        if char in "'\"":
            end = index + 1
            while end < length:
                if css[end] == "\\":
                    end += 2
                    continue
                if css[end] == char:
                    end += 1
                    break
                end += 1
            out.append(css[index:end])
            index = end
            continue
        if css[index : index + 2] == "/*":
            end = css.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_selectors(selector_list: str) -> list[str]:
    """쉼표로 나눈다. 괄호 안(:is(), :not() 등)의 쉼표는 건드리지 않는다."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in selector_list:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _is_global(selector: str) -> bool:
    """선택자가 문서 전체를 겨냥하는지."""
    head = re.split(r"[\s>+~]", selector, maxsplit=1)[0].strip()
    # html.dark, body#main 같은 형태도 전역이다.
    head = re.split(r"[.#:\[]", head, maxsplit=1)[0] or head
    return selector.strip() in GLOBAL_SELECTORS or head in GLOBAL_SELECTORS


def _scope_selector(selector: str, scope: str) -> str | None:
    if _is_global(selector):
        return None
    return f"{scope} {selector}"


def _split_top_level_blocks(css: str) -> Iterator[tuple[str, str, str]]:
    """(prelude, body, kind) 를 차례로 내놓는다. kind 는 'rule' 또는 'at'."""
    index = 0
    length = len(css)
    while index < length:
        brace = css.find("{", index)
        if brace == -1:
            tail = css[index:].strip()
            if tail:
                yield tail, "", "text"
            return
        prelude = css[index:brace]
        depth = 1
        position = brace + 1
        while position < length and depth:
            if css[position] == "{":
                depth += 1
            elif css[position] == "}":
                depth -= 1
            position += 1
        body = css[brace + 1 : position - 1]
        yield prelude, body, "at" if prelude.lstrip().startswith("@") else "rule"
        index = position


def scope_css(css: str, scope: str, *, source: str = "") -> str:
    """CSS 안의 모든 선택자를 ``scope`` 아래로 한정한다."""
    return _scope_css(strip_comments(css), scope, source=source)


def _scope_css(css: str, scope: str, *, source: str) -> str:
    out: list[str] = []
    for prelude, body, kind in _split_top_level_blocks(css):
        if kind == "text":
            out.append(prelude)
            continue
        if kind == "at":
            name = prelude.strip().split(None, 1)[0].lower()
            if name in _NESTING_AT_RULES:
                out.append(f"{prelude}{{{_scope_css(body, scope, source=source)}}}")
            else:
                # @keyframes / @font-face 등은 선택자가 없어 범위 개념이 없다.
                # 이름이 겹치지 않게 하는 것은 담당자 몫이라 문서로 안내한다.
                out.append(f"{prelude}{{{body}}}")
            continue
        scoped = [_scope_selector(selector, scope) for selector in _split_selectors(prelude)]
        dropped = [
            selector
            for selector, result in zip(_split_selectors(prelude), scoped)
            if result is None
        ]
        for selector in dropped:
            LOGGER.error(
                "%s: 선택자 %r 는 전역에 영향을 주므로 제외했습니다. 자기 화면 안에서만 "
                "쓰이도록 고쳐야 합니다.",
                source or scope,
                selector.strip(),
            )
        kept = [item for item in scoped if item]
        if kept:
            out.append(f"{', '.join(kept)}{{{body}}}")
    return "".join(out)


def scope_template(source: str, scope: str, *, name: str = "") -> str:
    """템플릿 안의 ``<style>`` 블록만 범위 처리한다. 나머지는 그대로 둔다."""

    def replace(match: re.Match[str]) -> str:
        return match.group(1) + scope_css(match.group(2), scope, source=name) + match.group(3)

    return _STYLE_BLOCK.sub(replace, source)


def _iter_css_selectors(css: str) -> Iterator[str]:
    for prelude, body, kind in _split_top_level_blocks(css):
        if kind == "rule":
            yield from _split_selectors(prelude)
        elif kind == "at" and prelude.strip().split(None, 1)[0].lower() in _NESTING_AT_RULES:
            # @media 안쪽 선택자도 검사 대상이다.
            yield from _iter_css_selectors(body)


def iter_style_selectors(source: str) -> Iterator[str]:
    """템플릿의 ``<style>`` 안에 있는 선택자를 하나씩 내놓는다(점검용)."""
    for block in _STYLE_BLOCK.finditer(source):
        yield from _iter_css_selectors(strip_comments(block.group(2)))
