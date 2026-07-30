from __future__ import annotations

import importlib
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

    required = ["flask", "openpyxl", "yaml"]
    modules = {}
    for name in required:
        try:
            importlib.import_module(name)
            modules[name] = "OK"
        except Exception as exc:
            modules[name] = f"FAILED: {exc}"
    try:
        importlib.import_module("oracledb")
        modules["oracledb"] = "OK"
    except Exception:
        modules["oracledb"] = "NOT_INSTALLED (FILE_ONLY/DEMO 가능)"

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
    if any(value.startswith("FAILED") for value in modules.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
