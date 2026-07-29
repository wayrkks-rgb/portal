from __future__ import annotations

import ipaddress
import re
from typing import Iterable

_INVALID = {"", "-", "nan", "none", "null", "0.0.0.0", "::"}


def normalize_ip(value: object) -> str | None:
    text = "" if value is None else str(value).strip().lower()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    if text in _INVALID:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def split_ips(values: Iterable[object]) -> list[str]:
    result: set[str] = set()
    for value in values:
        if value is None:
            continue
        for token in re.split(r"[,;\n\r\t ]+", str(value)):
            ip = normalize_ip(token)
            if ip:
                result.add(ip)
    return sorted(result, key=lambda x: (":" in x, x))
