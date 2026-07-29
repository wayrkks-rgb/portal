from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors.rvtools_command_runner import RVToolsCommandRunner
from asset_sync.config import load_config


if __name__ == "__main__":
    print(json.dumps(RVToolsCommandRunner(load_config()).test_all(), ensure_ascii=False, indent=2, default=str))
