from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors.rvtools_file_collector import RVToolsFileCollector
from asset_sync.config import load_config


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()
    rows, metadata = RVToolsFileCollector(load_config()).collect(args.files or None)
    print(f"RVTools 검증 성공: {len(rows):,}건")
    print(metadata)
