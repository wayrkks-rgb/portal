"""HMC 접속 정보 저장. 비밀번호는 나가지 않고, 비워 보내도 지워지지 않는다."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from asset_sync.settings_store import LocalSettingsStore, SettingsValidationError


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalSettingsStore:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    return LocalSettingsStore(tmp_path)


def _payload(**overrides):
    endpoint = {"id": "hmc01", "name": "본사 HMC", "host": "10.0.0.10",
                "username": "hscroot", "password": "secret", "enabled": True}
    endpoint.update(overrides)
    return {"hmc": {"enabled": True, "endpoints": [endpoint]}}


def test_the_password_is_stored_but_never_returned(store: LocalSettingsStore) -> None:
    saved = store.save_hmc(_payload())

    endpoint = saved["hmc"]["endpoints"][0]
    assert endpoint["password_configured"] is True
    assert "password" not in endpoint
    # 파일에는 들어 있어야 수집이 된다. 나가지만 않으면 된다.
    local = yaml.safe_load((store.root / "config" / "app_config.local.yaml").read_text(encoding="utf-8"))
    assert local["hmc"]["endpoints"][0]["password"] == "secret"


def test_an_empty_password_keeps_the_saved_one(store: LocalSettingsStore) -> None:
    """화면은 비밀번호를 돌려받지 못한다. 주소만 고쳐 저장할 때 지워지면 안 된다."""
    store.save_hmc(_payload())
    store.save_hmc(_payload(host="10.0.0.99", password=""))

    local = yaml.safe_load((store.root / "config" / "app_config.local.yaml").read_text(encoding="utf-8"))
    assert local["hmc"]["endpoints"][0]["host"] == "10.0.0.99"
    assert local["hmc"]["endpoints"][0]["password"] == "secret"


def test_what_cannot_be_used_is_refused_with_a_reason(store: LocalSettingsStore) -> None:
    for payload, reason in (
        (_payload(id=""), "식별자"),
        (_payload(host=""), "주소"),
        (_payload(username=""), "계정"),
        (_payload(password=""), "비밀번호"),
    ):
        with pytest.raises(SettingsValidationError) as excinfo:
            store.save_hmc(payload)
        assert reason in str(excinfo.value)


def test_the_same_identifier_twice_is_refused(store: LocalSettingsStore) -> None:
    payload = _payload()
    payload["hmc"]["endpoints"].append(dict(payload["hmc"]["endpoints"][0]))
    with pytest.raises(SettingsValidationError) as excinfo:
        store.save_hmc(payload)
    assert "중복" in str(excinfo.value)


def test_saving_hmc_does_not_touch_oracle_or_vcenter(store: LocalSettingsStore) -> None:
    """섹션을 따로 저장한다. 하나를 고치다 다른 하나가 지워지면 안 된다."""
    (store.root / "config" / "app_config.local.yaml").write_text(
        yaml.safe_dump({"oracle": {"host": "db.example.invalid"},
                        "vcenter": {"vcenters": [{"id": "vc01"}]}}, allow_unicode=True),
        encoding="utf-8",
    )
    store.save_hmc(_payload())

    local = yaml.safe_load((store.root / "config" / "app_config.local.yaml").read_text(encoding="utf-8"))
    assert local["oracle"]["host"] == "db.example.invalid"
    assert local["vcenter"]["vcenters"] == [{"id": "vc01"}]
    assert local["hmc"]["endpoints"][0]["id"] == "hmc01"
