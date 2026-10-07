"""ITSM 자산과 vCenter VM 을 짝지우는 규칙. 한 곳에만 둔다.

정합성 화면과 교차 점검이 **같은 규칙**으로 짝을 지어야 한다. 따로 두면 한쪽은
짝을 찾고 다른 쪽은 못 찾는 일이 생기고, 그러면 어느 쪽을 믿어야 하는지 알 수
없다. 이 프로젝트에서 대수가 화면마다 달랐던 이유가 늘 그런 중복이었다.

짝짓는 순서는 믿을 만한 것부터다.

1. ``identity_map`` — 사람이 한 번 확인해 기억해 둔 짝. 가장 믿을 만하다.
2. 호스트명 + IP 둘 다 맞음
3. 호스트명만 맞음
4. IP 만 맞음
5. (설정으로 켠 경우에만) VM 이름이 호스트명과 같음

후보가 둘 이상이면 짝으로 보지 않는다. 틀린 짝을 지으면 양쪽 대수가 다 틀린다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Match:
    """짝짓기 결과. 후보가 없거나 여럿이면 ``key`` 가 None 이다."""

    key: str | None
    method: str | None
    score: int
    candidates: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.key is None and len(self.candidates) > 1


@dataclass
class MatchIndex:
    """vCenter VM 을 호스트명·IP·UUID·VM 이름으로 찾을 수 있게 세운 색인."""

    by_uuid: dict[str, str] = field(default_factory=dict)
    by_hostname: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_ip: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_vm_name: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


def build_index(records: Mapping[str, Mapping[str, Any]]) -> MatchIndex:
    index = MatchIndex()
    for key, record in records.items():
        uuid = str(record.get("vm_uuid") or "")
        if uuid:
            index.by_uuid[uuid] = key
        hostname = record.get("normalized_hostname")
        if hostname:
            index.by_hostname[str(hostname)].add(key)
        for ip in record.get("ip_addresses") or []:
            if ip:
                index.by_ip[str(ip)].add(key)
        name = record.get("vm_name")
        if name:
            index.by_vm_name[str(name).strip().lower()].add(key)
    return index


def find(
    cm_id: str,
    asset: Mapping[str, Any],
    index: MatchIndex,
    *,
    identity_map: Mapping[str, str] | None = None,
    allow_vm_name: bool = False,
) -> Match:
    """자산 하나에 맞는 VM 을 찾는다. 믿을 만한 기준부터 차례로 본다."""
    mapped = (identity_map or {}).get(cm_id)
    if mapped and mapped in index.by_uuid:
        return Match(index.by_uuid[mapped], "IDENTITY_MAP", 100, (index.by_uuid[mapped],))

    hostname = asset.get("normalized_hostname")
    ips = {str(ip) for ip in (asset.get("ip_addresses") or []) if ip}
    by_host = set(index.by_hostname.get(str(hostname), set())) if hostname else set()
    by_ip: set[str] = set()
    for ip in ips:
        by_ip |= index.by_ip.get(ip, set())

    both = by_host & by_ip
    if both:
        return _decide(both, "IP_HOSTNAME", 95)
    if by_host:
        return _decide(by_host, "HOSTNAME", 80)
    if by_ip:
        return _decide(by_ip, "IP", 70)
    if allow_vm_name and hostname:
        # VM 이름은 사람이 붙인 것이라 틀릴 수 있다. 설정으로 켠 곳에서만 쓴다.
        return _decide(set(index.by_vm_name.get(str(hostname).strip().lower(), set())),
                       "VM_NAME", 50)
    return Match(None, None, 0, ())


def _decide(candidates: set[str], method: str, score: int) -> Match:
    ordered = tuple(sorted(candidates))
    if len(ordered) != 1:
        # 후보가 여럿이면 짝으로 보지 않는다. 틀린 짝은 양쪽 대수를 다 틀리게 한다.
        return Match(None, method, score, ordered)
    return Match(ordered[0], method, score, ordered)
