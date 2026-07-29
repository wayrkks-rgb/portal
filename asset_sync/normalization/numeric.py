from __future__ import annotations

import re


def normalize_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.lower() in {"", "-", "nan", "none", "null"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def memory_to_mb(value: object, default_unit: str = "MB") -> int | None:
    if value is None:
        return None
    text = str(value).strip().upper().replace(",", "")
    if text.lower() in {"", "-", "nan", "none", "null"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    unit = "GB" if "GB" in text or "GIB" in text else "MB" if "MB" in text or "MIB" in text else default_unit.upper()
    return int(round(number * 1024)) if unit == "GB" else int(round(number))


def normalize_bool(value: object) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "1.0", "y", "yes", "true", "예"}:
        return True
    if text in {"0", "0.0", "n", "no", "false", "아니오"}:
        return False
    return None
