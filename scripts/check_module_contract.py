"""대메뉴(모듈)가 통합 웹의 계약을 지키는지 점검한다.

담당자가 자기 branch 를 올리기 전에 스스로 확인하는 용도이고, CI 도 같은 것을
돌린다. 통합 웹을 기동하지 않고 파일만 읽으므로 어디서든 돌릴 수 있다.

    python scripts/check_module_contract.py                # 전체
    python scripts/check_module_contract.py --module capacity
    python scripts/check_module_contract.py --live         # WAS 까지 호출
    python scripts/check_module_contract.py --mysql-schema # MySQL 에 스키마 적용

실패하면 exit code 1 이다.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.module_assets import TEMPLATE_ROLES, discover_module_templates  # noqa: E402
from application.modules.registry import ModuleRegistry  # noqa: E402
from asset_sync.config import load_config, load_module_registry_files  # noqa: E402
from asset_sync.db.migrations import all_migrations  # noqa: E402
from asset_sync.db.module_schema import MODULES_DIR, discover_module_schemas  # noqa: E402

OK, WARN, FAIL = "  OK  ", " 경고 ", " 실패 "


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        print(f"[{OK}] {message}")

    def warn(self, message: str) -> None:
        print(f"[{WARN}] {message}")
        self.warnings.append(message)

    def fail(self, message: str) -> None:
        print(f"[{FAIL}] {message}")
        self.failures.append(message)

    def check(self, condition: bool, ok_message: str, fail_message: str) -> bool:
        (self.ok if condition else self.fail)(ok_message if condition else fail_message)
        return condition


def check_registry(report: Report, wanted: str | None) -> list:
    """설정 파일이 레지스트리에 실제로 반영되는지 본다."""
    print("\n== 대메뉴 등록 ==")
    declared = {str(item.get("id")) for item in load_module_registry_files(ROOT)}
    config = load_config()
    registry = ModuleRegistry.from_config(config.modules)
    loaded = {module.id for module in registry.all()}

    # from_config 는 잘못된 정의를 건너뛰고 로그만 남긴다. 여기서는 실패로 본다.
    for module_id in sorted(declared - loaded):
        report.fail(f"config/modules/{module_id}.yaml 이 레지스트리에 등록되지 않았습니다(정의 오류).")
    for module_id in sorted(declared & loaded):
        report.ok(f"{module_id}: config/modules/{module_id}.yaml 등록됨")

    modules = [m for m in registry.all() if wanted is None or m.id == wanted]
    if wanted and not modules:
        report.fail(f"{wanted} 모듈이 레지스트리에 없습니다.")
    for module in modules:
        if module.children:
            pages = [child.page for child in module.children]
            if len(pages) != len(set(pages)):
                report.fail(f"{module.id}: 소메뉴 page 가 중복됩니다: {pages}")
            else:
                report.ok(f"{module.id}: 소메뉴 {len(module.children)}개")
        if not module.is_local and not module.base_url:
            report.fail(f"{module.id}: base_url 이 비었는데 원격 모듈로 등록되었습니다.")
    return modules


def check_schemas(report: Report, wanted: str | None) -> None:
    """SQLite/MySQL 파일 짝과 테이블 이름 규칙. 규칙 위반은 예외로 올라온다."""
    print("\n== DB 스키마 파일 ==")
    for engine in ("sqlite", "mysql"):
        try:
            files = discover_module_schemas(engine)
        except Exception as exc:
            report.fail(f"{engine} 스키마: {exc}")
            continue
        found = [f for f in files if wanted is None or f.module_id == wanted]
        if not found:
            report.ok(f"{engine}: 모듈 스키마 파일 없음 (선택 사항)")
            continue
        for schema in found:
            report.ok(f"{schema.module_id}: {schema.path.name} ({len(schema.statements)} 문장)")


def check_migrations(report: Report, wanted: str | None) -> None:
    print("\n== 마이그레이션 ==")
    try:
        migrations = all_migrations()
    except Exception as exc:
        report.fail(f"마이그레이션을 모으지 못했습니다: {exc}")
        return
    names = [name for name, _, _ in migrations]
    mine = [name for name in names if wanted is None or name.startswith(f"{wanted}/")]
    if not mine:
        report.ok("모듈 마이그레이션 없음 (선택 사항)")
    for name in mine:
        report.ok(f"{name}")
    # 이름을 바꾸면 이미 배포된 DB 에서 다시 실행된다. 숫자 이름은 그 사고를 부른다.
    numeric = [name for name in names if re.fullmatch(r"[^/]+/\d+", name)]
    if numeric:
        report.warn(f"숫자만으로 된 마이그레이션 이름이 있습니다(충돌 위험): {numeric}")


def check_templates(report: Report, modules: list) -> None:
    print("\n== 화면 파일 ==")
    found = discover_module_templates(ROOT / "templates")
    for module in modules:
        assets = found.get(module.id)
        if assets is None:
            report.ok(f"{module.id}: 화면 파일 없음 → 공통 화면(패널 스펙)으로 표시")
            continue
        roles = [role for role in TEMPLATE_ROLES if assets.get(role)]
        report.ok(f"{module.id}: {', '.join(roles)}")
        if assets.page:
            text = (ROOT / "templates" / assets.page).read_text(encoding="utf-8")
            expected = f'id="page-{module.page or module.id}"'
            if expected not in text:
                report.fail(
                    f"{module.id}: page.html 에 {expected} 이 없습니다. "
                    "사이드바가 이 id 로 화면을 전환하므로 빈 화면이 됩니다."
                )
            # 소메뉴가 자기 화면을 갖는 경우도 같은 규칙이다.
            for child in module.children:
                if f'id="page-{child.page}"' not in text and child.page != module.page:
                    report.warn(
                        f"{module.id}.{child.id}: page-{child.page} 화면이 없습니다 → "
                        f"?menu={child.id} 로 패널을 받아 그립니다."
                    )
    for module_id in sorted(set(found) - {m.id for m in modules}):
        if not any(m.id == module_id for m in modules):
            report.warn(f"templates/modules/{module_id}/ 가 있는데 등록된 대메뉴가 아닙니다.")


def check_styles(report: Report) -> None:
    """모듈 CSS 가 다른 팀 화면을 건드리지 않는지."""
    print("\n== 모듈 CSS ==")
    from application.module_styles import GLOBAL_SELECTORS, iter_style_selectors

    base = ROOT / "templates" / "modules"
    if not base.is_dir():
        report.ok("모듈 화면 없음")
        return
    checked = 0
    for path in sorted(base.rglob("*.html")):
        if path.name.startswith("_"):
            continue
        for selector in iter_style_selectors(path.read_text(encoding="utf-8")):
            checked += 1
            head = selector.split()[0].split(">")[0].strip()
            if head in GLOBAL_SELECTORS:
                report.fail(
                    f"{path.relative_to(ROOT)}: '{selector}' 는 전역에 영향을 줍니다. "
                    "모듈 화면 안에서만 쓰이도록 자기 클래스를 앞에 두세요."
                )
    report.ok(f"선택자 {checked}개 확인 (나머지는 렌더링 시 모듈 범위로 한정됨)")


def check_live(report: Report, modules: list) -> None:
    """실제 WAS 를 호출해 패널 스펙까지 확인한다."""
    print("\n== WAS 호출 ==")
    from application.modules.client import ModuleClient
    from application.modules.panels import normalize_panel

    config = load_config()
    registry = ModuleRegistry.from_config(config.modules)
    client = ModuleClient(registry)
    user = {"username": "check-script", "role": "admin", "id": 0, "name": "점검"}
    for module in modules:
        if module.is_local:
            report.ok(f"{module.id}: 내부 모듈 (HTTP 호출 없음)")
            continue
        health = client.health(module.id, user=user)
        report.check(
            health.ok,
            f"{module.id}: {module.health_path} 정상 ({health.elapsed_ms}ms)",
            f"{module.id}: {module.health_path} 실패 - {health.status} {health.error}",
        )
        panel = client.call(module.id, module.panel_path, user=user)
        if not panel.ok:
            report.fail(f"{module.id}: {module.panel_path} 실패 - {panel.status} {panel.error}")
            continue
        spec = normalize_panel(panel.data)
        if not spec["title"] and not spec["metrics"] and not spec["table"]:
            report.fail(f"{module.id}: 패널 응답에 title/metrics/table 이 모두 없습니다.")
        else:
            report.ok(
                f"{module.id}: 패널 정상 (지표 {len(spec['metrics'])}개, "
                f"표 {len(spec['table']['rows']) if spec['table'] else 0}행)"
            )
        for child in module.children:
            sub = client.call(module.id, module.panel_path, user=user, params={"menu": child.id})
            report.check(
                sub.ok,
                f"{module.id}?menu={child.id}: 정상",
                f"{module.id}?menu={child.id}: 실패 - {sub.status} {sub.error}",
            )


def check_mysql_schema(report: Report) -> None:
    """MySQL 에 스키마를 실제로 적용해 본다.

    SQLite 로만 돌리면 MySQL 전용 파일의 문법 오류를 놓친다.
    """
    print("\n== MySQL 스키마 적용 ==")
    from asset_sync.db.manager import MySQLManager
    from asset_sync.db.migrations import pending_names

    settings = (load_config().database or {}).get("mysql") or {}
    if not settings.get("host") or not settings.get("database"):
        report.fail("MySQL 접속 정보가 없습니다. MYSQL_HOST/MYSQL_DATABASE 환경변수를 설정하세요.")
        return
    try:
        manager = MySQLManager(settings)
        manager.initialize()
        # 두 번째 기동에서 아무것도 다시 적용되지 않아야 한다.
        manager.initialize()
        with manager.connect() as conn:
            remaining = pending_names(conn)
    except Exception as exc:
        report.fail(f"스키마 적용 실패: {exc}")
        return
    report.check(
        not remaining,
        f"스키마와 마이그레이션 적용 완료 · 재적용 없음 ({settings['database']})",
        f"재기동 후에도 미적용 마이그레이션이 남습니다: {remaining}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="대메뉴 연동 계약 점검")
    parser.add_argument("--module", help="이 모듈만 점검한다")
    parser.add_argument("--live", action="store_true", help="WAS 를 실제로 호출한다")
    parser.add_argument("--mysql-schema", action="store_true", help="MySQL 에 스키마를 적용해 본다")
    args = parser.parse_args()

    report = Report()
    if args.mysql_schema:
        check_mysql_schema(report)
    else:
        modules = check_registry(report, args.module)
        check_schemas(report, args.module)
        check_migrations(report, args.module)
        check_templates(report, modules)
        check_styles(report)
        if args.live:
            check_live(report, modules)

    print("\n" + "=" * 60)
    if report.failures:
        print(f"실패 {len(report.failures)}건 · 경고 {len(report.warnings)}건")
        for message in report.failures:
            print(f"  - {message}")
        return 1
    print(f"통과 · 경고 {len(report.warnings)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
