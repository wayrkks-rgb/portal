from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*)?$")


def validate_oracle_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} 값이 Oracle 식별자 형식이 아닙니다: {value!r}")
    return value
