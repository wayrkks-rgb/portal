from pathlib import Path

from asset_sync.collectors.rvtools_command_runner import RVToolsCommandRunner
from asset_sync.config import AppConfig


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(root_dir=tmp_path, rvtools={"incoming_dir": "data/incoming/rvtools"}, security={})


def test_build_pass_through_command(tmp_path: Path) -> None:
    runner = RVToolsCommandRunner(make_config(tmp_path))
    cmd = runner._build_command(
        {"server": "vc.example.invalid", "auth_mode": "PASS_THROUGH", "bypass_ssl_check": True},
        Path("RVTools.exe"), tmp_path, tmp_path / "out.xlsx",
    )
    assert "-passthroughAuth" in cmd
    assert "vc.example.invalid" in cmd
    assert "-BypassSSLCheck" in cmd
    assert "-u" not in cmd


def test_build_encrypted_password_command(tmp_path: Path) -> None:
    runner = RVToolsCommandRunner(make_config(tmp_path))
    cmd = runner._build_command(
        {
            "server": "vc.example.invalid", "auth_mode": "ENCRYPTED_PASSWORD",
            "username": "user001", "encrypted_password": "_RVToolsV3PWD_SYNTHETIC",
        },
        Path("RVTools.exe"), tmp_path, tmp_path / "out.xlsx",
    )
    assert cmd[cmd.index("-u") + 1] == "user001"
    assert cmd[cmd.index("-p") + 1] == "_RVToolsV3PWD_SYNTHETIC"
