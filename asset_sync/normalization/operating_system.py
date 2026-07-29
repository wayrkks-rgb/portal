from __future__ import annotations

import re

_FAMILY_PATTERNS = [
    ("Linux Redhat", ("red hat", "redhat", "rhel")),
    ("WINDOWS", ("windows",)),
    ("CentOS", ("centos",)),
    ("Rocky", ("rocky",)),
    ("Ubuntu", ("ubuntu",)),
    ("HP-UX", ("hp-ux", "hpux")),
    ("AIX", ("aix",)),
    ("ESXi", ("esxi", "vmware photon")),
    ("XenOS", ("xen",)),
    ("NUTANIX", ("nutanix",)),
    ("Appliance", ("appliance",)),
]


def normalize_os(value: object) -> tuple[str | None, str | None]:
    text = "" if value is None else str(value).strip()
    if text.lower() in {"", "-", "nan", "none", "null"}:
        return None, None
    lower = text.lower()
    family = next((name for name, tokens in _FAMILY_PATTERNS if any(token in lower for token in tokens)), "기타")
    match = re.search(r"(?<!\d)(\d{1,2})(?:\.(\d{1,2}))?", text)
    version = None
    if match:
        version = match.group(1) + (f".{match.group(2)}" if match.group(2) else "")
    return family, version
