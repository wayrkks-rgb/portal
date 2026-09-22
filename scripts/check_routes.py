"""화면이 부르는 주소가 지금 이 서버에 실제로 등록돼 있는지 확인한다.

파일을 덮어쓸 때 일부가 빠지면, 화면은 새것인데 서버는 옛것인 상태가 된다. 그러면
화면이 부르는 주소에 404 가 돌아온다. 오류 없이 조용히 멈추므로 원인을 찾기 어렵다.

이 스크립트는 템플릿에 적힌 모든 ``/api`` 주소를 모아 Flask 라우팅 표에 물어본다.
빠진 파일이 있으면 어느 주소가 없는지 바로 나온다.

쓰는 법::

    .venv\\Scripts\\python.exe scripts\\check_routes.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 따옴표 세 종류(' " `) 안에 든 /api/ 로 시작하는 주소.
_URL = re.compile(r"""['"`](/api/[^'"`\s]*)['"`]""")
# `${...}` 와 `{{ ... }}` 는 실행할 때 값이 채워진다. 무엇이든 온다고 본다.
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}")
_CONVERTER = re.compile(r"<[^>]*>")
_ANY = "\x00"

#: 주소창에 직접 넣어도 열려야 하는 화면.
PAGES = ("/dashboard", "/report", "/compare", "/history", "/asset-sync",
         "/daily-check", "/weekly-check", "/monthly-check", "/integration-settings")


def template_urls() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "templates").rglob("*.html")):
        if "modules" in path.parts:
            continue        # 담당자 모듈 화면은 자기 WAS 를 부른다
        for raw in _URL.findall(path.read_text(encoding="utf-8")):
            url = _PLACEHOLDER.sub(_ANY, raw).split("?")[0].rstrip("/") or "/"
            found.setdefault(url, []).append(str(path.relative_to(ROOT)))
    return found


def matches(url: str, rule: str) -> bool:
    left, right = url.strip("/").split("/"), rule.strip("/").split("/")
    if len(left) != len(right):
        return False
    return all(
        mine == theirs or _ANY in mine or _CONVERTER.fullmatch(theirs)
        for mine, theirs in zip(left, right)
    )


def main() -> int:
    from application import create_app

    app = create_app()
    rules = [rule.rule for rule in app.url_map.iter_rules()]

    missing_pages = [page for page in PAGES if page not in rules]
    urls = template_urls()
    missing_api = {
        url.replace(_ANY, "{...}"): sources
        for url, sources in urls.items()
        if not any(matches(url, rule) for rule in rules)
    }

    print(f"등록된 주소 {len(rules)}개 · 화면이 부르는 API 주소 {len(urls)}개")
    print("-" * 78)

    if missing_pages:
        print("[없는 화면 주소]")
        for page in missing_pages:
            print(f"  {page}")
        print()
    if missing_api:
        print("[화면이 부르는데 서버에 없는 API 주소]")
        for url, sources in missing_api.items():
            print(f"  {url}")
            for source in sorted(set(sources)):
                print(f"      ← {source}")
        print()

    if not missing_pages and not missing_api:
        print("이상 없음. 화면이 부르는 주소가 모두 등록돼 있습니다.")
        return 0

    print("-" * 78)
    print("빠진 주소가 있습니다. 파이썬 파일이 덜 덮어써진 경우가 대부분입니다.")
    print("특히 asset_sync\\routes\\ 아래 파일이 모두 최신인지 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
