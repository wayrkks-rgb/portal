from __future__ import annotations

import importlib
import importlib.metadata as metadata
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.config import load_config
from asset_sync.db.manager import create_manager


def main() -> None:
    if sys.version_info[:2] != (3, 13):
        raise SystemExit(f"Python 3.13이 필요합니다. 현재 버전: {sys.version.split()[0]}")

    # 없으면 통합 웹이 아예 못 뜨는 것들.
    required = ["flask", "openpyxl", "yaml", "werkzeug", "jinja2"]
    # 없으면 해당 기능만 못 쓰는 것들. 무엇을 못 하게 되는지 함께 알려준다.
    optional = {
        "oracledb": ("requirements-oracle.txt", "Oracle 직접조회 (FILE_ONLY/DEMO 는 가능)"),
        "requests": ("requirements-bff.txt", "원격 대메뉴(담당 WAS) 호출"),
        "pymysql": ("requirements-mysql.txt", "여러 WAS 가 공유하는 MySQL 연결"),
    }

    # import 이름과 배포 이름이 다른 것들. 버전은 배포 이름으로 조회한다.
    distributions = {"yaml": "PyYAML", "pymysql": "PyMySQL", "jinja2": "Jinja2"}

    def _version(name: str) -> str:
        try:
            return metadata.version(distributions.get(name, name))
        except metadata.PackageNotFoundError:
            return ""

    modules: dict[str, str] = {}
    for name in required:
        try:
            importlib.import_module(name)
            modules[name] = f"OK {_version(name)}".strip()
        except Exception as exc:
            modules[name] = f"FAILED: {exc}"
    for name, (source, purpose) in optional.items():
        try:
            importlib.import_module(name)
            modules[name] = f"OK {_version(name)}".strip()
        except Exception:
            modules[name] = f"NOT_INSTALLED — {purpose} 불가 · {source} 설치"

    cfg = load_config()
    manager = create_manager(cfg)
    manager.initialize()
    directories = [
        cfg.resolve("data/incoming/itsm"), cfg.resolve("data/incoming/vcenter"),
        cfg.resolve("data/archive/vcenter"), cfg.resolve("data/temp/powercli"),
        cfg.resolve("data/export"), cfg.resolve("logs"),
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    result = {
        "python": sys.version,
        "modules": modules,
        "database": manager.describe(),
        "database_engine": manager.engine,
        "itsm_mode": cfg.itsm.get("collection_mode"),
        "vcenter_mode": cfg.rvtools.get("collection_mode"),
        "directories": [str(path) for path in directories],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    missing = [name for name, value in modules.items() if value.startswith("NOT_INSTALLED")]
    if missing:
        # 선택 패키지는 실패로 보지 않는다. 다만 무엇을 못 쓰게 되는지는 눈에 띄어야 한다.
        print(f"\n[WARN] 설치되지 않은 선택 패키지: {', '.join(missing)}", file=sys.stderr)
    if any(value.startswith("FAILED") for value in modules.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
