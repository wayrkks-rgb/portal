from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors.oracle_itsm_collector import OracleITSMCollector
from asset_sync.config import load_config


if __name__ == "__main__":
    print(json.dumps(OracleITSMCollector(load_config()).test_connection(), ensure_ascii=False, indent=2))
