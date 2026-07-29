"""PowerCLI collector unit tests. """
from pathlib import Path

from asset_sync.collectors.powercli_collector import PowerCLICollector, PowerCLICollectionError
from asset_sync.config import AppConfig


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(root_dir=tmp_path, rvtools={
        "default_port":443, "default_auth_mode":"CREDENTIAL", "default_username":"user001", "default_password":"secret",
        "default_bypass_ssl_check":True, "vcenters":[],
    }, security={})


def test_common_credential_is_resolved(tmp_path: Path) -> None:
    collector = PowerCLICollector(make_config(tmp_path))
    resolved = collector._effective_entry({"id":"vc_001","server":"vc.example.invalid","auth_profile":"COMMON"})
    assert resolved["auth_mode"] == "CREDENTIAL"
    assert resolved["username"] == "user001"
    assert resolved["password"] == "secret"
    env = collector._build_environment(resolved, tmp_path / "out.json")
    assert env["VCENTER_SERVER"] == "vc.example.invalid"
    assert env["VCENTER_PASSWORD"] == "secret"


def test_pass_through_does_not_require_password(tmp_path: Path) -> None:
    collector = PowerCLICollector(make_config(tmp_path))
    resolved = collector._effective_entry({"id":"vc_001","server":"vc.example.invalid","auth_profile":"CUSTOM","auth_mode":"PASS_THROUGH"})
    env = collector._build_environment(resolved, tmp_path / "out.json")
    assert env["VCENTER_AUTH_MODE"] == "PASS_THROUGH"


def test_missing_credential_is_rejected(tmp_path: Path) -> None:
    cfg = make_config(tmp_path); cfg.rvtools["default_password"] = ""
    collector = PowerCLICollector(cfg)
    resolved = collector._effective_entry({"id":"vc_001","server":"vc.example.invalid","auth_profile":"COMMON"})
    try:
        collector._build_environment(resolved, tmp_path / "out.json")
    except PowerCLICollectionError as exc:
        assert "사용자명과 비밀번호" in str(exc)
    else:
        raise AssertionError("missing password must be rejected")
