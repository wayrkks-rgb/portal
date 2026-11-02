"""일간 점검 [현재 VM 상태 보기]: 전원이 바뀐 VM 의 '지금' 전원을 통합기에서 다시 읽는다.

실제 vCenter 는 없으므로 PowerShell 자리에 가짜 실행 파일을, REST 자리에 가짜 세션을
둔다. 확인하는 것은 (1) 그 VM 의 통합기에만 붙는가, (2) 비밀번호가 명령줄이 아니라
환경 변수로만 가는가, (3) 단계별 시간이 결과에 실리는가, (4) REST 가 막히면 PowerCLI 로
넘어가는가 이다.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from asset_sync.services import live_power_service as live
from asset_sync.services.live_power_service import LivePowerService, normalize_power

VMS = [
    {"asset_key": "u1", "vm_id": "virtualmachine-vm-101", "vm_uuid": "uuid-1", "vm_name": "WEB01",
     "vcenter": "VC1", "power_state": "poweredoff"},
    {"asset_key": "u2", "vm_id": "virtualmachine-vm-102", "vm_uuid": "uuid-2", "vm_name": "WEB02",
     "vcenter": "VC1", "power_state": "poweredon"},
    {"asset_key": "u3", "vm_id": "virtualmachine-vm-201", "vm_uuid": "uuid-3", "vm_name": "DB01",
     "vcenter": "VC2", "power_state": "poweredoff"},
]


def fake_powershell(tmp_path: Path) -> Path:
    """PowerShell 대신 돈다. 받은 환경 변수를 남기고, VM_KEYS 에 든 VM 만 켜짐으로 답한다."""
    script = tmp_path / "fake_pwsh.py"
    script.write_text(
        "import json, os, sys\n"
        f"open({str(tmp_path / 'seen.json')!r}, 'a').write(json.dumps({{'argv': sys.argv[1:],"
        " 'server': os.environ.get('VCENTER_SERVER'), 'password': os.environ.get('VCENTER_PASSWORD'),"
        " 'keys': os.environ.get('VM_KEYS')}) + '\\n')\n"
        "keys = os.environ.get('VM_KEYS', '').split(',')\n"
        "vms = [{'moref': k, 'name': k.upper(), 'power': 'poweredOn'} for k in keys if k.startswith('vm-')]\n"
        "print('noise line')\n"
        "print('RESULT_JSON=' + json.dumps({'timings': {'module': 8.5, 'connect': 1.2, 'query': 0.4}, 'vms': vms}))\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "pwsh"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


def config_for(tmp_path: Path, method: str = "POWERCLI", **extra) -> SimpleNamespace:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "get_vm_power_state.ps1").write_text("# fake", encoding="utf-8")
    rvtools = {
        "live_power_method": method,
        "powershell_path": str(fake_powershell(tmp_path)),
        "default_auth_mode": "CREDENTIAL", "default_username": "svc", "default_password": "s3cret!",
        "vcenters": [{"id": "VC1", "name": "본사 통합기", "server": "vc1.local"},
                     {"id": "VC2", "name": "DR 통합기", "server": "vc2.local"}],
        **extra,
    }
    return SimpleNamespace(rvtools=rvtools, root_dir=tmp_path, security={},
                           resolve=lambda value: Path(value) if Path(value).is_absolute() else tmp_path / value)


def seen(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "seen.json").read_text().splitlines()]


def test_each_vcenter_is_asked_once_for_its_own_vms(tmp_path) -> None:
    result = LivePowerService(config_for(tmp_path)).lookup(VMS)
    assert result["status"] == "SUCCESS"
    calls = {call["server"]: call for call in seen(tmp_path)}
    assert set(calls) == {"vc1.local", "vc2.local"}                 # 통합기마다 한 번
    assert calls["vc1.local"]["keys"] == "uuid-1,uuid-2,vm-101,vm-102"
    assert calls["vc2.local"]["keys"] == "uuid-3,vm-201"
    # 비밀번호는 환경 변수로만 간다. 명령줄(프로세스 목록에 보임)에 있으면 안 된다.
    assert all("s3cret!" not in " ".join(call["argv"]) for call in calls.values())
    assert calls["vc1.local"]["password"] == "s3cret!"

    by_key = {row["vm_key"]: row for row in result["results"]}
    assert (by_key["u1"]["batch_power"], by_key["u1"]["now_power"], by_key["u1"]["same"]) == ("poweredoff", "poweredon", False)
    assert by_key["u2"]["same"] is True and by_key["u2"]["now_label"] == "켜짐"
    vc1 = next(v for v in result["vcenters"] if v["vcenter_id"] == "VC1")
    assert vc1["method"] == "POWERCLI" and vc1["vcenter_label"] == "본사 통합기"
    timings = vc1["attempts"][0]["timings"]
    assert timings["module"] == 8.5 and timings["connect"] == 1.2 and "process" in timings


def test_auto_falls_back_to_powercli_when_rest_fails(tmp_path, monkeypatch) -> None:
    def broken_rest(self, entry, vms):
        raise live.LivePowerError("REST 막힘(HTTP 503)")

    monkeypatch.setattr(LivePowerService, "_via_rest", broken_rest)
    result = LivePowerService(config_for(tmp_path, "AUTO")).lookup(VMS[:1])
    vc = result["vcenters"][0]
    assert [a["method"] for a in vc["attempts"]] == ["REST", "POWERCLI"]
    assert vc["attempts"][0]["status"] == "FAILED" and "503" in vc["attempts"][0]["error"]
    assert vc["method"] == "POWERCLI" and result["results"][0]["now_power"] == "poweredon"


def test_pass_through_skips_rest(tmp_path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(LivePowerService, "_via_rest", lambda self, e, v: called.append(1))
    config = config_for(tmp_path, "AUTO")
    config.rvtools["vcenters"][0].update({"auth_profile": "CUSTOM", "auth_mode": "PASS_THROUGH"})
    result = LivePowerService(config).lookup(VMS[:1])
    assert not called and result["vcenters"][0]["method"] == "POWERCLI"


class FakeResponse:
    def __init__(self, status: int, body) -> None:
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """vSphere 7.0U1 이하처럼 /api 가 없고 /rest 만 있는 통합기."""
    calls: list[tuple[str, str, object]] = []

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.verify = True

    def post(self, url, auth=None, timeout=None):
        FakeSession.calls.append(("POST", url, auth))
        if url.endswith("/api/session"):
            return FakeResponse(404, {})
        return FakeResponse(200, {"value": "tok-1"})

    def get(self, url, params=None, timeout=None):
        FakeSession.calls.append(("GET", url, dict(self.headers)))
        wanted = [value for _, value in params]
        return FakeResponse(200, {"value": [{"vm": v, "name": v.upper(), "power_state": "POWERED_ON"} for v in wanted]})

    def delete(self, url, timeout=None):
        FakeSession.calls.append(("DELETE", url, None))
        return FakeResponse(200, {})


def test_rest_uses_the_old_api_when_the_new_one_is_missing(tmp_path, monkeypatch) -> None:
    pytest.importorskip("requests")
    import requests

    FakeSession.calls = []
    monkeypatch.setattr(requests, "Session", FakeSession)
    result = LivePowerService(config_for(tmp_path, "REST")).lookup(VMS[:2])
    vc = result["vcenters"][0]
    assert vc["method"] == "REST" and set(vc["attempts"][0]["timings"]) == {"connect", "query"}
    assert [row["now_power"] for row in result["results"]] == ["poweredon", "poweredon"]
    get = next(call for call in FakeSession.calls if call[0] == "GET")
    assert get[1].endswith("/rest/vcenter/vm") and get[2]["vmware-api-session-id"] == "tok-1"
    assert FakeSession.calls[-1][0] == "DELETE"                         # 세션을 닫는다


def test_unknown_vcenter_and_missing_vm_are_reported_not_raised(tmp_path) -> None:
    stray = dict(VMS[0], asset_key="x", vcenter="NOPE")
    # 가짜 PowerShell 은 vm- 로 시작하는 키에만 답한다. UUID 로만 찾게 해 '통합기에 없음' 을 만든다.
    gone = dict(VMS[1], asset_key="g", vm_id="", vm_uuid="uuid-gone")
    result = LivePowerService(config_for(tmp_path)).lookup([stray, gone])
    by_key = {row["vm_key"]: row for row in result["results"]}
    assert by_key["x"]["found"] is False and "연계 설정에 없는" in by_key["x"]["error"]
    assert by_key["g"]["found"] is False and "찾지 못했습니다" in by_key["g"]["error"]
    assert result["status"] == "PARTIAL"


def test_power_values_are_normalized() -> None:
    assert normalize_power("POWERED_ON") == normalize_power("poweredOn") == "poweredon"
    assert live.power_label("POWERED_OFF") == "꺼짐"


def test_the_endpoint_asks_only_with_server_side_ids(tmp_path, monkeypatch) -> None:
    """화면은 VM 키만 보낸다. 통합기 주소 · ID 는 서버가 스냅샷에서 정한다."""
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(Path(__file__).parent))
    from test_late_itsm_registration import put_vcenter, vm

    captured = {}

    def fake_lookup(self, vms):
        captured["vms"] = vms
        return {"status": "SUCCESS", "results": [], "vcenters": []}

    monkeypatch.setattr(LivePowerService, "lookup", fake_lookup)
    from application import create_app
    from application.db import database_manager, reset_database_manager
    from asset_sync.config import load_config

    reset_database_manager()
    try:
        app = create_app()
        put_vcenter(load_config(), database_manager(), "2026-10-10", [vm("u1", "web01", "10.0.0.1")])
        client = app.test_client()
        assert client.post("/api/asset-sync/vm-power-now", json={"vm_keys": ["u1"]}).status_code in (302, 401)
        with client.session_transaction() as session:
            session["user"] = {"id": 1, "username": "hong", "role": "user", "name": "홍"}
        body = client.post("/api/asset-sync/vm-power-now",
                           json={"vm_keys": ["u1", "nope"], "server": "evil.example"}).get_json()
        assert body["missing"] == ["nope"]
        assert [v["asset_key"] for v in captured["vms"]] == ["u1"]
        assert captured["vms"][0]["vcenter"] == "VC1" and captured["vms"][0]["vm_id"] == "mo-u1"
        assert client.post("/api/asset-sync/vm-power-now", json={"vm_keys": []}).status_code == 400
        assert client.post("/api/asset-sync/vm-power-now", json={"vm_keys": ["zz"]}).status_code == 404
    finally:
        reset_database_manager()
