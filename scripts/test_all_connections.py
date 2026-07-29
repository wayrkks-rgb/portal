from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors import OracleITSMCollector, PowerCLICollector, VCenterSnapshotFileCollector
from asset_sync.config import load_config


def main() -> None:
    cfg = load_config()
    results: dict[str, object] = {}
    itsm_mode = str(cfg.itsm.get("collection_mode", "ORACLE")).upper()
    vc_mode = str(cfg.rvtools.get("collection_mode", "POWERCLI")).upper()
    if itsm_mode == "ORACLE":
        try:
            results["oracle"] = OracleITSMCollector(cfg).test_connection()
        except Exception as exc:
            results["oracle"] = {"status": "FAILED", "error": str(exc)}
    else:
        results["oracle"] = {"status": "SKIPPED", "reason": f"ITSM mode={itsm_mode}"}

    try:
        if vc_mode == "POWERCLI":
            results["vcenter"] = PowerCLICollector(cfg).test_all()
        elif vc_mode == "FILE_ONLY":
            rows, meta = VCenterSnapshotFileCollector(cfg).collect()
            results["vcenter"] = {"status": "SUCCESS", "count": len(rows), "metadata": meta}
        else:
            results["vcenter"] = {"status": "SKIPPED", "reason": f"vCenter mode={vc_mode}"}
    except Exception as exc:
        results["vcenter"] = {"status": "FAILED", "error": str(exc)}
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
