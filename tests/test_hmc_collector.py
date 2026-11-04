"""HMC 연동을 실제 장비 없이 확인한다.

HMC 가 돌려주는 XML 을 그대로 흉내 낸 가짜 응답으로 검사한다. 중요한 성질은
세 가지다.

1. 네임스페이스가 붙어 있어도, 펌웨어 버전마다 달라도 값을 찾아야 한다.
   경로로 찾으면 장비를 올릴 때마다 깨진다.
2. 세션 토큰을 받아 이후 요청 헤더에 넣고, 끝나면 로그오프해야 한다.
   지우지 않으면 HMC 쪽 동시 세션 수 제한에 걸린다.
3. 한 대가 실패해도 나머지 HMC 는 계속 수집해야 한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from asset_sync.config import AppConfig
from asset_sync.collectors.hmc_client import HMCError
from asset_sync.collectors.hmc_collector import HMCCollector, normalize_endpoint

UOM = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"

MANAGED_SYSTEM_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://hmc/rest/api/uom/ManagedSystem/sys-uuid-1</id>
    <content><ManagedSystem:ManagedSystem xmlns:ManagedSystem="{UOM}">
      <SystemName xmlns="{UOM}">P9-FRAME-01</SystemName>
      <State xmlns="{UOM}">operating</State>
      <MachineTypeModelAndSerialNumber xmlns="{UOM}">
        <MachineType>9080</MachineType><Model>M9S</Model><SerialNumber>78AB123</SerialNumber>
      </MachineTypeModelAndSerialNumber>
      <AssociatedSystemMemoryConfiguration xmlns="{UOM}">
        <InstalledSystemMemory>2097152</InstalledSystemMemory>
        <CurrentAvailableSystemMemory>524288</CurrentAvailableSystemMemory>
      </AssociatedSystemMemoryConfiguration>
      <AssociatedSystemProcessorConfiguration xmlns="{UOM}">
        <InstalledSystemProcessorUnits>48.0</InstalledSystemProcessorUnits>
        <CurrentAvailableSystemProcessorUnits>12.5</CurrentAvailableSystemProcessorUnits>
      </AssociatedSystemProcessorConfiguration>
    </ManagedSystem:ManagedSystem></content>
  </entry>
</feed>"""

LPAR_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://hmc/rest/api/uom/LogicalPartition/lpar-uuid-1</id>
    <content><LogicalPartition xmlns="{UOM}">
      <PartitionName>AIX-WAS-01</PartitionName>
      <PartitionID>3</PartitionID>
      <PartitionState>running</PartitionState>
      <PartitionType>AIX/Linux</PartitionType>
      <OperatingSystemVersion>AIX 7.2 7200-05-04-2220</OperatingSystemVersion>
      <ResourceMonitoringIPAddress>10.10.20.31</ResourceMonitoringIPAddress>
      <PartitionMemoryConfiguration><CurrentMemory>65536</CurrentMemory></PartitionMemoryConfiguration>
      <PartitionProcessorConfiguration>
        <CurrentProcessingMode>shared</CurrentProcessingMode>
        <SharedProcessorConfiguration>
          <CurrentProcessingUnits>2.5</CurrentProcessingUnits>
          <CurrentMaximumVirtualProcessors>8</CurrentMaximumVirtualProcessors>
        </SharedProcessorConfiguration>
      </PartitionProcessorConfiguration>
    </LogicalPartition></content>
  </entry>
</feed>"""

LOGON_RESPONSE = b"""<?xml version="1.0"?>
<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">
  <X-API-Session>session-token-abc</X-API-Session>
</LogonResponse>"""


class FakeClient:
    """HMC 대신 미리 만든 XML 을 돌려준다. 호출 순서를 기록해 확인한다."""

    calls: list[str] = []

    def __init__(self, host, username, password, *, port=12443, verify_tls=False, timeout_seconds=30, **_):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.token: str | None = None
        self.fail_on: str | None = None

    def logon(self) -> str:
        FakeClient.calls.append(f"logon:{self.host}")
        if self.host == "unreachable":
            raise HMCError("연결하지 못했습니다.", "CONNECT")
        if not self.password:
            raise HMCError("인증에 실패했습니다.", "AUTH")
        self.token = "session-token-abc"
        return self.token

    def get(self, path: str) -> Any:
        FakeClient.calls.append(f"get:{path}")
        assert self.token, "로그온 없이 조회하면 안 된다"
        if "LogicalPartition" in path:
            return ElementTree.fromstring(LPAR_FEED)
        return ElementTree.fromstring(MANAGED_SYSTEM_FEED)

    def logoff(self) -> None:
        FakeClient.calls.append(f"logoff:{self.host}")
        self.token = None


@pytest.fixture(autouse=True)
def _reset_calls():
    FakeClient.calls = []
    yield


def _config(endpoints: list[dict], tmp_path: Path) -> AppConfig:
    return AppConfig(
        root_dir=tmp_path,
        sqlite_path=Path("data/hmc.db"),
        hmc={"enabled": True, "port": 12443, "timeout_seconds": 30, "endpoints": endpoints},
    )


def _endpoint(**overrides) -> dict:
    return {"id": "hmc01", "name": "본사 HMC", "host": "10.0.0.10",
            "username": "hscroot", "password": "secret", "enabled": True, **overrides}


def test_a_frame_is_read_even_though_the_xml_is_namespaced(tmp_path: Path) -> None:
    collector = HMCCollector(_config([_endpoint()], tmp_path), client_factory=FakeClient)
    result = collector.collect_one(_endpoint())

    frame = result["frames"][0]
    assert frame["system_name"] == "P9-FRAME-01"
    assert frame["machine_type"] == "9080"
    assert frame["model"] == "M9S"
    assert frame["serial_number"] == "78AB123"
    assert frame["system_uuid"] == "sys-uuid-1"
    # 숫자는 숫자로. 문자열이면 합계를 낼 수 없다.
    assert frame["installed_proc_units"] == 48
    assert frame["available_proc_units"] == 12.5
    assert frame["installed_memory_mb"] == 2097152


def test_an_lpar_carries_the_frame_it_sits_on(tmp_path: Path) -> None:
    collector = HMCCollector(_config([_endpoint()], tmp_path), client_factory=FakeClient)
    lpar = collector.collect_one(_endpoint())["partitions"][0]

    assert lpar["partition_name"] == "AIX-WAS-01"
    assert lpar["partition_id"] == 3
    assert lpar["partition_state"] == "running"
    assert lpar["os_version"].startswith("AIX 7.2")
    assert lpar["rmc_ip"] == "10.10.20.31"
    assert lpar["memory_mb"] == 65536
    assert lpar["proc_units"] == 2.5
    # 어느 프레임 위인지 없으면 자원 합계를 낼 수 없다.
    assert lpar["system_name"] == "P9-FRAME-01"
    assert lpar["system_uuid"] == "sys-uuid-1"


def test_the_session_is_always_closed(tmp_path: Path) -> None:
    """세션을 지우지 않으면 HMC 의 동시 세션 수 제한에 걸린다."""
    collector = HMCCollector(_config([_endpoint()], tmp_path), client_factory=FakeClient)
    collector.collect_one(_endpoint())

    assert FakeClient.calls[0] == "logon:10.0.0.10"
    assert FakeClient.calls[-1] == "logoff:10.0.0.10"


def test_one_broken_hmc_does_not_stop_the_others(tmp_path: Path) -> None:
    config = _config([
        _endpoint(id="hmc01"),
        _endpoint(id="hmc02", host="unreachable"),
        _endpoint(id="hmc03", host="10.0.0.12"),
    ], tmp_path)
    result = HMCCollector(config, client_factory=FakeClient).collect_all()

    assert result["status"] == "PARTIAL"
    assert result["success_scopes"] == ["hmc01", "hmc03"]
    assert "hmc02" in result["failed_scopes"]
    assert result["frame_count"] == 2


def test_a_disabled_hmc_is_skipped(tmp_path: Path) -> None:
    config = _config([_endpoint(id="hmc01"), _endpoint(id="hmc02", enabled=False)], tmp_path)
    assert [e["id"] for e in HMCCollector(config).endpoints()] == ["hmc01"]


def test_the_connection_test_says_where_it_stopped(tmp_path: Path) -> None:
    collector = HMCCollector(_config([], tmp_path), client_factory=FakeClient)

    ok = collector.test_one(_endpoint())
    assert ok["status"] == "SUCCESS"
    assert ok["frame_count"] == 1
    assert ok["frames"] == ["P9-FRAME-01"]

    unreachable = collector.test_one(_endpoint(host="unreachable"))
    assert unreachable["status"] == "FAILED"
    assert unreachable["stage"] == "CONNECT"

    no_password = collector.test_one(_endpoint(password=""))
    assert no_password["status"] == "FAILED"
    assert no_password["stage"] == "AUTH"

    empty = collector.test_one(_endpoint(host=""))
    assert empty["status"] == "FAILED"
    assert empty["stage"] == "CONFIG"


def test_the_default_port_is_the_rest_api_port_not_the_console(tmp_path: Path) -> None:
    """12443 이 REST API 포트다. 443(콘솔)·22(SSH) 와 다르다."""
    assert normalize_endpoint({"id": "a", "host": "h"})["port"] == 12443
    assert normalize_endpoint({"id": "a", "host": "h", "port": "8443"})["port"] == 8443
    assert normalize_endpoint({"id": "a", "host": "h", "port": ""})["port"] == 12443


def test_the_logon_token_is_read_from_the_response() -> None:
    from asset_sync.collectors.hmc_client import HMCClient

    assert HMCClient._read_token(LOGON_RESPONSE) == "session-token-abc"
    with pytest.raises(HMCError) as excinfo:
        HMCClient._read_token(b"<html>not xml at all")
    assert excinfo.value.stage == "LOGON"


# ── 방화벽 · 계정이 맞는데도 나던 오류들 ───────────────────────────────────
import ssl as _ssl
import urllib.request as _urlreq

from asset_sync.collectors.hmc_client import HMCClient as _RealClient


def test_the_windows_system_proxy_is_not_used_by_default(monkeypatch) -> None:
    """urllib 은 시스템(IE) 프록시를 저절로 쓴다. HMC 요청이 프록시로 가면 403 · 시간 초과가 난다."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

    def proxied(client) -> bool:
        return any(isinstance(h, _urlreq.ProxyHandler) and h.proxies for h in client._opener.handlers)

    assert not proxied(_RealClient("10.0.0.10", "u", "p"))
    assert proxied(_RealClient("10.0.0.10", "u", "p", use_proxy=True))


def test_tls_failures_are_not_reported_as_a_firewall_problem() -> None:
    client = _RealClient("10.0.0.10", "u", "p")
    message, stage = client._describe_connect_error(
        _ssl.SSLError(1, "[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled"))
    assert stage == "TLS" and "legacy_tls" in message and "방화벽" not in message
    assert client._describe_connect_error(TimeoutError("timed out"))[1] == "TIMEOUT"
    assert client._describe_connect_error(OSError("Tunnel connection failed: 403 Forbidden"))[1] == "PROXY"
    assert client._describe_connect_error(ConnectionRefusedError(111, "Connection refused"))[1] == "CONNECT"


def test_legacy_tls_is_opt_in() -> None:
    plain = _RealClient("h", "u", "p").ssl_context()
    legacy = _RealClient("h", "u", "p", legacy_tls=True).ssl_context()
    flag = getattr(_ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    assert not plain.options & flag and legacy.options & flag


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Opener:
    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies, self.requests = list(bodies), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _Response(self.bodies.pop(0))


def test_an_empty_204_reply_is_an_empty_list_and_logon_asks_for_the_response_type() -> None:
    token = (b'<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">'
             b'<X-API-Session>tok</X-API-Session></LogonResponse>')
    opener = _Opener([token, b""])
    client = _RealClient("10.0.0.10", "u", "p", opener=opener)
    feed = client.get("/rest/api/uom/ManagedSystem/x/LogicalPartition")
    assert list(feed) == []
    logon = opener.requests[0]
    assert logon.get_header("Accept").endswith("type=LogonResponse")
    assert opener.requests[1].get_header("X-api-session") == "tok"
