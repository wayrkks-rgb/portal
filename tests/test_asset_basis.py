"""자산으로 셀 ITSM 상태를 설정으로 고른다.

예전에는 운영·대기를 함께 셌다. 이제 기본은 **운영만**이고, 보고 목적에 따라
운영+대기, 미사용만 등으로 바꿔 볼 수 있다. 바꾸면 대시보드·일·주·월·EOSL 대수가
함께 바뀌어야 하고, **vCenter VM 대수는 바뀌면 안 된다** -- VM 은 전원 상태와
수동 제외·포함 정책으로 센다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from asset_sync.services.asset_scope import (
    ACTIVE_STATUS,
    BASIS_PRESETS,
    AssetScope,
    basis_label,
    basis_preset,
    criteria_from,
    normalize_basis,
)


def _cfg(codes=None):
    section = {} if codes is None else {"active_status": codes}
    return SimpleNamespace(server_status=section)


def _asset(status: str) -> dict:
    return {"cm_id": f"CM-{status}", "status_code": status,
            "raw": {"CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8", "CM_EOL_DT": "2030-12-31"}}


# ── 기준 값 ────────────────────────────────────────────────────────────


def test_the_default_counts_operating_only() -> None:
    assert ACTIVE_STATUS == ("CMSTA010",)
    assert criteria_from(_cfg()).active_status == ("CMSTA010",)


def test_presets_read_as_plain_words() -> None:
    assert basis_label(BASIS_PRESETS["OPERATING"]) == "운영"
    assert basis_label(BASIS_PRESETS["OPERATING_STANDBY"]) == "운영·대기"
    assert basis_label(BASIS_PRESETS["UNUSED"]) == "미사용"
    assert basis_preset(["CMSTA050", "CMSTA010"]) == "OPERATING_STANDBY"
    assert basis_preset(["CMSTA010", "CMSTA060"]) == "CUSTOM"


def test_codes_are_cleaned_and_ordered() -> None:
    assert normalize_basis(["cmsta050", " CMSTA010 "]) == ("CMSTA010", "CMSTA050")
    assert normalize_basis("CMSTA020,CMSTA010") == ("CMSTA010", "CMSTA020")
    with pytest.raises(ValueError):
        normalize_basis([])
    with pytest.raises(ValueError):
        normalize_basis(["CMSTA999"])


def test_a_broken_setting_falls_back_instead_of_crashing() -> None:
    """설정 파일을 손으로 잘못 고쳐도 화면이 죽지 않아야 한다."""
    assert criteria_from(_cfg(["NOPE"])).active_status == ACTIVE_STATUS


# ── 기준이 판정을 실제로 바꾸나 ─────────────────────────────────────────


@pytest.mark.parametrize("codes, counted", [
    (["CMSTA010"], {"CMSTA010"}),
    (["CMSTA010", "CMSTA050"], {"CMSTA010", "CMSTA050"}),
    (["CMSTA020"], {"CMSTA020"}),
])
def test_the_basis_decides_which_assets_count(codes, counted) -> None:
    scope = AssetScope(criteria_from(_cfg(codes)), {})
    decided = {status for status in ("CMSTA010", "CMSTA020", "CMSTA050", "CMSTA060")
               if scope.decide_itsm(_asset(status)).included}
    assert decided == counted


def test_the_exclusion_reason_names_the_current_basis() -> None:
    scope = AssetScope(criteria_from(_cfg(["CMSTA010", "CMSTA050"])), {})
    assert scope.reason_label("STATUS") == "상태가 집계 기준(운영·대기)이 아님"
    assert scope.reason_label("MANUAL") == "수동 제외"


def test_sql_counts_follow_the_basis() -> None:
    scope = AssetScope(criteria_from(_cfg(["CMSTA010", "CMSTA050"])), {})
    assert scope.status_sql() == ("status_code IN (?,?)", ["CMSTA010", "CMSTA050"])


def test_vm_counting_ignores_the_itsm_basis() -> None:
    """VM 은 전원·수동 제외/포함 정책으로 센다. ITSM 기준을 바꿔도 같아야 한다."""
    vms = [
        {"asset_key": "u1", "vm_name": "APP-01", "power_state": "poweredOn"},
        {"asset_key": "u2", "vm_name": "vCLS-1234", "power_state": "poweredOn"},
        {"asset_key": "u3", "vm_name": "TPL", "template_flag": True},
    ]
    results = [
        AssetScope(criteria_from(_cfg(codes)), {}).vcenter_summary(vms)
        for codes in (["CMSTA010"], ["CMSTA010", "CMSTA050"], ["CMSTA020"])
    ]
    assert len({(r["selected"], r["excluded"]) for r in results}) == 1
    assert results[0]["selected"] == 1


# ── 저장과 화면 ────────────────────────────────────────────────────────


def test_saving_writes_the_local_settings(tmp_path: Path) -> None:
    from asset_sync.settings_store import LocalSettingsStore, SettingsValidationError

    (tmp_path / "config").mkdir()
    store = LocalSettingsStore(tmp_path)
    saved = store.save_asset_basis({"preset": "OPERATING_STANDBY"})
    assert saved["codes"] == ["CMSTA010", "CMSTA050"]
    written = yaml.safe_load((tmp_path / "config" / "app_config.local.yaml").read_text(encoding="utf-8"))
    assert written["server_status"]["active_status"] == ["CMSTA010", "CMSTA050"]

    saved = store.save_asset_basis({"preset": "CUSTOM", "codes": ["CMSTA060", "CMSTA020"]})
    assert saved["codes"] == ["CMSTA020", "CMSTA060"] and saved["preset"] == "CUSTOM"
    with pytest.raises(SettingsValidationError):
        store.save_asset_basis({"preset": "CUSTOM", "codes": []})


@pytest.fixture()
def admin_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\nvcenter:\n  collection_mode: DEMO\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    client = create_app().test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    yield client
    reset_database_manager()


def _seed_demo(client) -> None:
    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager
    from asset_sync.services.collection_service import CollectionService

    CollectionService(load_config(), create_manager(load_config())).run_daily(demo=True)


def test_saving_on_screen_changes_the_counts_without_a_restart(admin_client) -> None:
    """저장한 뒤 다음 조회부터 바로 바뀌어야 한다. 재기동을 기다리면 안 된다."""
    _seed_demo(admin_client)
    before = admin_client.get("/api/asset-sync/dashboard").get_json()
    assert before["asset_status"]["total"] == 1                      # 데모: 운영 1 · 대기 1

    response = admin_client.put("/api/asset-sync/admin/settings/asset-basis",
                                json={"preset": "OPERATING_STANDBY"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["asset_basis"]["label"] == "운영·대기"

    after = admin_client.get("/api/asset-sync/dashboard").get_json()
    assert after["asset_status"]["total"] == 2
    assert after["asset_status"]["counting_basis"]["basis"]["label"] == "운영·대기"


def test_a_bad_basis_is_refused(admin_client) -> None:
    response = admin_client.put("/api/asset-sync/admin/settings/asset-basis",
                                json={"preset": "CUSTOM", "codes": []})
    assert response.status_code == 400
