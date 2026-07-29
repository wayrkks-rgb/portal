from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.config import load_config


def main() -> None:
    cfg = load_config()
    value = str(cfg.rvtools.get("powershell_path") or "powershell.exe")
    executable = str(Path(value)) if Path(value).is_absolute() and Path(value).exists() else shutil.which(value)
    if not executable:
        print(json.dumps({"status": "FAILED", "stage": "POWERSHELL", "error": "PowerShell executable was not found."}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    script = cfg.resolve("scripts/check_powercli.ps1")
    proc = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(json.dumps({"status": "FAILED", "stage": "POWERCLI_MODULE", "error": (proc.stderr or proc.stdout).strip()}, ensure_ascii=False, indent=2))
        raise SystemExit(proc.returncode)
    print(proc.stdout.strip())


if __name__ == "__main__":
    main()
