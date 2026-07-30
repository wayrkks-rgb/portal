from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
DATA_DIR = BASE_DIR / "data"

USERS_FILE = DATA_DIR / "users.json"
MAPPINGS_FILE = DATA_DIR / "mappings.json"
HISTORY_FILE = DATA_DIR / "history.json"


def initialize_legacy_data() -> None:
    """Create the small JSON stores used by the existing report UI.

    Accounts are no longer created here: they live in the shared database so every
    WAS sees the same list. An existing USERS_FILE is only read once, to import it.
    """
    for directory in (UPLOAD_DIR, OUTPUT_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if not MAPPINGS_FILE.exists():
        MAPPINGS_FILE.write_text(
            json.dumps(
                {
                    "menus": [
                        {
                            "id": "vm_resource",
                            "name": "x86 통합 자원사용률 상세 현황",
                            "asis_col_vm": "A",
                            "asis_col_cpu_max": "B",
                            "asis_col_cpu_avg": "C",
                            "asis_col_mem_max": "D",
                            "asis_col_mem_avg": "E",
                            "asis_header_row": 1,
                            "tobe_col_vm": "B",
                            "tobe_col_cpu_max": "E",
                            "tobe_col_cpu_avg": "F",
                            "tobe_col_mem_max": "G",
                            "tobe_col_mem_avg": "H",
                            "tobe_data_start_row": 7,
                            "tobe_count_row": 3,
                            "sheet_vm_mappings": [],
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if not HISTORY_FILE.exists():
        HISTORY_FILE.write_text("[]", encoding="utf-8")
