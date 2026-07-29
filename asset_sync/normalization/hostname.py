from __future__ import annotations

import re
from typing import Iterable


def normalize_hostname(value: object, suffixes: Iterable[str] = ()) -> str | None:
    text = "" if value is None else str(value).strip().lower().rstrip(".")
    if text in {"", "-", "nan", "none", "null"}:
        return None
    for suffix in suffixes:
        normalized_suffix = str(suffix).strip().lower().rstrip(".")
        if normalized_suffix and text.endswith(normalized_suffix):
            text = text[: -len(normalized_suffix)].rstrip(".")
            break
    if "." in text:
        text = text.split(".", 1)[0]
    text = re.sub(r"[^a-z0-9_-]", "", text)
    return text or None
