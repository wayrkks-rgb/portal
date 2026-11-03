"""오늘 vCenter 에만 생긴 서버를 며칠 뒤 ITSM 에 등록하면, 그날 운영 대수에 잡히는가.

운영 대수는 ITSM 기준이다. 그래서 VM 이 생긴 날에는 대수가 그대로이고, ITSM 에
**등록(또는 운영 전환)되는 날** +1 이 돼야 한다. 그 사이 일간 화면에는 'ITSM 미반영'
으로 보이다가, 등록한 뒤에는 '반영됨' 으로 바뀌어야 한다. 월간 대수와 대수 감사도
같은 숫자를 내야 한다 -- 일 · 주 · 월이 한 몸으로 움직이는지 실제 비교 엔진으로
날짜를 넘겨 가며 확인한다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.asset_scope import AssetScope
from asset_sync.services.change_digest_service import (
    CHANGED,
    CREATED,
    NOT_REFLECTED,
    REFLECTED,
    ChangeDigestService,
)
from asset_sync.services.count_audit_service import CountAuditService
from asset_sync.services.diff_service import DiffService
from asset_sync.services.server_status_service import ServerStatusService

PREV_END, DAY_VM, DAY_ITSM, DAY_RUN, MONTH_END = (
    "2026-09-30", "2026-10-05", "2026-10-08", "2026-10-10", "2026-10-31",
)


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/late.db"))
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def asset(cm_id: str, host: str, ip: str, status: str = "CMSTA010") -> dict:
    raw = {"CM_ID": cm_id, "CM_NAME": f"업무-{host}", "CM_HOSTNAME": host, "CM_IP": ip,
           "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6", "CM_EOL_DT": "2030-12-31",
           "CM_PLACE": "CMPLACE010", "CM_STA_CD": status, "CM_SVR_CAT_CD": "CMSVRCATCD020",
           "CM_CPU_CORE_CNT": "4", "CM_MEMORY": "8"}
    return {"cm_id": cm_id, "normalized_hostname": host, "primary_ip": ip, "ip": [ip],
            "cpu": 4, "memory": 8192, "status": status, "raw": raw}


def vm(key: str, host: str, ip: str) -> dict:
    return {
        "asset_key": key, "vm_uuid": key, "smbios_uuid": f"sm-{key}", "vm_id": f"mo-{key}",
        "vcenter": "VC1", "vm_name": host.upper(), "dns_name": host, "normalized_hostname": host,
        "ip_addresses": [ip], "primary_ip": ip, "cpus": 4, "memory_mb": 8192,
        "os_family": "Linux Redhat", "os_version": "8.6", "power_state": "poweredOn",
        "datacenter": "DC1", "cluster": "CL1", "cluster_name": "CL1", "esxi_host": "esxi-01",
        "template_flag": False, "srm_placeholder": False, "raw": {}, "record_hash": key,
    }


def put_itsm(config, manager, day: str, assets: list[dict]) -> None:
    """그날의 ITSM 스냅샷을 만들고, 직전 스냅샷과 비교해 변경 이벤트를 남긴다."""
    stamp = f"{day}T07:00:00"
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        previous = repo.latest_snapshot("ITSM")
        run = repo.start_collection_run("ITSM", stamp)
        repo.finish_collection_run(run, "SUCCESS", len(assets), stamp)
        snap = repo.create_snapshot("ITSM", day, stamp, run, "SUCCESS", len(assets), "h")
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(snap, a["cm_id"], a["normalized_hostname"], a["primary_ip"], json.dumps(a["ip"]),
              a["cpu"], a["memory"], "Linux Redhat", "8.6", a["status"], "CMSVRCATCD020",
              "CMOWNCATCD0010", "2030-12-31",
              # 실제 수집처럼 원본 전체로 해시한다. 비교 엔진은 해시가 같으면 건너뛴다.
              json.dumps(a["raw"], sort_keys=True, ensure_ascii=False),
              json.dumps(a["raw"], ensure_ascii=False)) for a in assets],
        )
        if previous:
            pair = DiffService(config, repo).compare_pair("ITSM", snap, int(previous["id"]))
            repo.replace_change_events(snap, pair["events"])


def put_vcenter(config, manager, day: str, vms: list[dict]) -> None:
    stamp = f"{day}T07:00:00"
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        previous = repo.latest_snapshot("RVTOOLS")
        run = repo.start_collection_run("RVTOOLS", stamp)
        repo.finish_collection_run(run, "SUCCESS", len(vms), stamp, ["VC1"], [])
        snap = repo.create_snapshot("RVTOOLS", day, stamp, run, "SUCCESS", len(vms), "h")
        repo.insert_rv_records(snap, vms)
        if previous:
            pair = DiffService(config, repo).compare_pair("RVTOOLS", snap, int(previous["id"]))
            repo.replace_change_events(snap, pair["events"])


def day_digest(config, manager, day: str, codes=None) -> dict:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        scope = None
        if codes:
            scope = AssetScope.load(SimpleNamespace(server_status={"active_status": codes}), repo)
        return ChangeDigestService(config, repo, scope).digest(f"{day}T00:00:00", f"{day}T23:59:59")


BASE_ASSETS = [asset(f"CM00{i}", f"srv{i}", f"10.0.0.{i}") for i in range(1, 4)]
BASE_VMS = [vm(f"u{i}", f"srv{i}", f"10.0.0.{i}") for i in range(1, 4)]
NEW_VM = vm("u-new", "newsrv", "10.0.0.99")


def setup_history(config, manager, *, registered_status: str = "CMSTA010") -> None:
    put_itsm(config, manager, PREV_END, BASE_ASSETS)
    put_vcenter(config, manager, PREV_END, BASE_VMS)
    # 10/05: vCenter 에만 VM 이 생겼다. ITSM 은 그대로.
    put_itsm(config, manager, DAY_VM, BASE_ASSETS)
    put_vcenter(config, manager, DAY_VM, BASE_VMS + [NEW_VM])
    # 10/08: 사흘 늦게 ITSM 에 등록됐다.
    put_itsm(config, manager, DAY_ITSM,
             BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99", registered_status)])
    put_vcenter(config, manager, DAY_ITSM, BASE_VMS + [NEW_VM])


def test_the_vm_day_does_not_count_and_the_registration_day_does(portal) -> None:
    config, manager = portal
    setup_history(config, manager)

    vm_day = day_digest(config, manager, DAY_VM)["rows"]
    assert len(vm_day) == 1
    assert vm_day[0]["kind"] == CREATED and vm_day[0]["count_effect"] == 0
    assert "등록" in vm_day[0]["count_note"]

    itsm_day = day_digest(config, manager, DAY_ITSM)["rows"]
    assert len(itsm_day) == 1
    assert itsm_day[0]["itsm_cm_id"] == "CM099"
    assert (itsm_day[0]["kind"], itsm_day[0]["count_effect"]) == (CREATED, 1)


def test_the_vm_day_turns_reflected_once_itsm_catches_up(portal) -> None:
    """반영 여부는 '지금 ITSM' 과 견준다. 늦게라도 등록하면 그날 줄도 반영됨이 된다."""
    config, manager = portal
    put_itsm(config, manager, PREV_END, BASE_ASSETS)
    put_vcenter(config, manager, PREV_END, BASE_VMS)
    put_itsm(config, manager, DAY_VM, BASE_ASSETS)
    put_vcenter(config, manager, DAY_VM, BASE_VMS + [NEW_VM])
    assert day_digest(config, manager, DAY_VM)["rows"][0]["reflection"] == NOT_REFLECTED

    put_itsm(config, manager, DAY_ITSM, BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99")])
    put_vcenter(config, manager, DAY_ITSM, BASE_VMS + [NEW_VM])
    assert day_digest(config, manager, DAY_VM)["rows"][0]["reflection"] == REFLECTED


def test_monthly_count_and_audit_agree_with_the_days(portal) -> None:
    """월간 서버 대수 +1, 그 달 일일 증감 합 +1, 대수 감사 OK."""
    config, manager = portal
    setup_history(config, manager)
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        current = repo.snapshot_on_or_before("ITSM", MONTH_END)
        previous = repo.snapshot_on_or_before("ITSM", PREV_END)
        status = ServerStatusService(config, repo).status(int(current["id"]), int(previous["id"]))
        assert status["all"]["delta"]["계"]["소계"] == 1

        audit = CountAuditService(config, repo).audit(date.fromisoformat(MONTH_END))
        check = next(c for c in audit["checks"] if c["name"] == "월간 대수 = 이번 달 일일 증감")
        assert check["verdict"] == "OK", check["message"]
        assert check["daily_net"] == 1


def test_registered_as_standby_counts_when_it_turns_operating(portal) -> None:
    """'운영' 기준에서 대기로 먼저 등록되면 그날은 그대로, 운영으로 바뀌는 날 +1."""
    config, manager = portal
    setup_history(config, manager, registered_status="CMSTA050")
    put_itsm(config, manager, DAY_RUN, BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99")])
    put_vcenter(config, manager, DAY_RUN, BASE_VMS + [NEW_VM])

    registered = day_digest(config, manager, DAY_ITSM)["rows"][0]
    assert (registered["kind"], registered["count_effect"]) == (CHANGED, 0)
    turned_on = day_digest(config, manager, DAY_RUN)["rows"][0]
    assert (turned_on["kind"], turned_on["count_effect"]) == (CREATED, 1)

    # '운영+대기' 기준이면 등록하는 날 바로 잡히고, 운영 전환은 그냥 변경이다.
    wide = ["CMSTA010", "CMSTA050"]
    assert day_digest(config, manager, DAY_ITSM, wide)["rows"][0]["count_effect"] == 1
    assert day_digest(config, manager, DAY_RUN, wide)["rows"][0]["kind"] == CHANGED


# ── 삭제도 같은 규칙인가 ─────────────────────────────────────────────────
# VM 이 먼저 사라지고 ITSM 은 며칠 뒤에 바뀐다. 대수는 ITSM 이 바뀐 날 -1 이어야 하고,
# 그사이 일간 화면에는 'ITSM 미반영' 으로 보여야 한다.
OLD_ASSET = asset("CM098", "oldsrv", "10.0.0.98")
OLD_VM = vm("u-old", "oldsrv", "10.0.0.98")


def setup_removal(config, manager, itsm_after: list[dict]) -> None:
    put_itsm(config, manager, PREV_END, BASE_ASSETS + [OLD_ASSET])
    put_vcenter(config, manager, PREV_END, BASE_VMS + [OLD_VM])
    # 10/05: vCenter 에서만 VM 이 사라졌다.
    put_itsm(config, manager, DAY_VM, BASE_ASSETS + [OLD_ASSET])
    put_vcenter(config, manager, DAY_VM, BASE_VMS)


@pytest.mark.parametrize("itsm_after", ["status", "deleted"], ids=["폐기로 바꿈", "레코드 삭제"])
def test_a_late_itsm_removal_counts_on_the_itsm_day(portal, itsm_after) -> None:
    config, manager = portal
    setup_removal(config, manager, [])
    before = day_digest(config, manager, DAY_VM)["rows"][0]
    assert (before["kind"], before["count_effect"], before["reflection"]) == ("REMOVED", 0, NOT_REFLECTED)
    assert before["itsm_cm_id"] == "CM098"

    later = BASE_ASSETS + ([asset("CM098", "oldsrv", "10.0.0.98", "CMSTA060")] if itsm_after == "status" else [])
    put_itsm(config, manager, DAY_ITSM, later)
    put_vcenter(config, manager, DAY_ITSM, BASE_VMS)

    itsm_day = day_digest(config, manager, DAY_ITSM)["rows"][0]
    assert (itsm_day["kind"], itsm_day["count_effect"]) == ("REMOVED", -1)
    # 사흘 전 줄을 다시 열어도 ITSM 짝을 잃지 않고, 이제는 반영됨이다.
    vm_day = day_digest(config, manager, DAY_VM)["rows"][0]
    assert vm_day["itsm_cm_id"] == "CM098" and vm_day["reflection"] == REFLECTED
    assert vm_day["count_effect"] == 0 and "이날이 아니라" in vm_day["count_note"]
    assert vm_day["hostname"] == "oldsrv"

    with manager.connect() as conn:
        repo = AssetRepository(conn)
        current = repo.snapshot_on_or_before("ITSM", MONTH_END)
        previous = repo.snapshot_on_or_before("ITSM", PREV_END)
        status = ServerStatusService(config, repo).status(int(current["id"]), int(previous["id"]))
        assert status["all"]["delta"]["계"]["소계"] == -1
        audit = CountAuditService(config, repo).audit(date.fromisoformat(MONTH_END))
        check = next(c for c in audit["checks"] if c["name"] == "월간 대수 = 이번 달 일일 증감")
        assert check["verdict"] == "OK" and check["daily_net"] == -1


# ── 주간 · 월간의 [생성] · [삭제] 목록 ─────────────────────────────────────
def test_period_movements_list_who_and_when(tmp_path, monkeypatch) -> None:
    """주간 화면 API: 생성 - 삭제 = 순증감이고, 줄마다 일간 기록의 날짜 · 사유가 붙는다."""
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import database_manager, reset_database_manager
    from asset_sync.config import load_config

    reset_database_manager()
    try:
        app = create_app()
        config, manager = load_config(), database_manager()
        put_itsm(config, manager, PREV_END, BASE_ASSETS + [OLD_ASSET])
        put_vcenter(config, manager, PREV_END, BASE_VMS + [OLD_VM])
        # 10/08: 신규 등록 1대 + 폐기 1대 + 대기로만 들어온 1대(대수 밖)
        put_itsm(config, manager, DAY_ITSM, BASE_ASSETS + [
            asset("CM099", "newsrv", "10.0.0.99"),
            asset("CM098", "oldsrv", "10.0.0.98", "CMSTA060"),
            asset("CM097", "stbsrv", "10.0.0.97", "CMSTA050"),
        ])
        client = app.test_client()
        with client.session_transaction() as session:
            session["user"] = {"id": 1, "username": "hong", "role": "user", "name": "홍"}
        body = client.get("/api/asset-sync/asset-movements?start=2026-10-05&end=2026-10-11").get_json()
        assert body["status"] == "SUCCESS", body
        moves = body["movements"]
        assert (moves["created"]["total"], moves["removed"]["total"], moves["net"]) == (1, 1, 0)
        assert moves["before_total"] == 4 and moves["after_total"] == 4
        created, removed = moves["created"]["items"][0], moves["removed"]["items"][0]
        assert (created["cm_id"], created["changed_on"], created["reason"]) == ("CM099", DAY_ITSM, "ITSM 신규 등록")
        assert created["hostname"] == "newsrv" and created["primary_ip"] == "10.0.0.99"
        assert (removed["cm_id"], removed["changed_on"]) == ("CM098", DAY_ITSM)
        assert "매각/폐기" in removed["reason"]

        # 월간 화면도 같은 목록 · 같은 사유
        monthly = client.get("/api/asset-sync/server-status?month=2026-10").get_json()["movements"]
        assert [i["cm_id"] for i in monthly["created"]["items"]] == ["CM099"]
        assert monthly["removed"]["items"][0]["reason"] == removed["reason"]
        assert client.get("/api/asset-sync/asset-movements?start=x&end=y").status_code == 400
    finally:
        reset_database_manager()


# ── 문구는 코드가 아니라 상태 이름, 대수 그대로인 삭제엔 그날 · 지금 상태 ─────────────
def test_notes_use_status_names_and_explain_unchanged_counts(portal) -> None:
    config, manager = portal
    unused = asset("CM098", "oldsrv", "10.0.0.98", "CMSTA020")       # 처음부터 미사용
    put_itsm(config, manager, PREV_END, BASE_ASSETS + [unused])
    put_vcenter(config, manager, PREV_END, BASE_VMS + [OLD_VM])
    put_itsm(config, manager, DAY_VM, BASE_ASSETS + [unused])
    put_vcenter(config, manager, DAY_VM, BASE_VMS)                  # VM 만 사라짐
    row = day_digest(config, manager, DAY_VM)["rows"][0]
    text = row["reflection_note"] + row["count_note"]
    assert "CMSTA" not in text
    assert row["reflection_note"].startswith("ITSM 상태가 이미 미사용입니다")
    assert "이날도 ITSM 상태가 미사용" in row["count_note"] and "무관" in row["count_note"]
    assert row["itsm_status_label"] == "미사용"


def test_was_operating_that_day_and_changed_later(portal) -> None:
    config, manager = portal
    setup_removal(config, manager, [])                               # 10/05 VM 사라짐, ITSM 운영
    put_itsm(config, manager, DAY_ITSM, BASE_ASSETS + [asset("CM098", "oldsrv", "10.0.0.98", "CMSTA020")])
    put_vcenter(config, manager, DAY_ITSM, BASE_VMS)
    note = day_digest(config, manager, DAY_VM)["rows"][0]["count_note"]
    assert "이날 ITSM 은 운영" in note and "지금 ITSM 상태는 미사용" in note


@pytest.mark.parametrize("codes, kind, effect", [
    (["CMSTA010"], "REMOVED", -1),                 # 운영만 셀 때: 운영 → 대기 는 삭제
    (["CMSTA010", "CMSTA050"], "CHANGED", 0),      # 운영+대기 를 셀 때: 그냥 변경
])
def test_the_chosen_basis_decides_created_and_removed(portal, codes, kind, effect) -> None:
    config, manager = portal
    put_itsm(config, manager, PREV_END, BASE_ASSETS)
    put_itsm(config, manager, DAY_ITSM, BASE_ASSETS[:2] + [asset("CM003", "srv3", "10.0.0.3", "CMSTA050")])
    row = day_digest(config, manager, DAY_ITSM, codes)["rows"][0]
    assert (row["kind"], row["count_effect"]) == (kind, effect)
    assert "대기" in row["summary"] and "CMSTA" not in row["summary"]


# ── 변동 내역은 ITSM 기준, vCenter 에만 생긴 것은 [ITSM 미반영] 으로 ─────────────
def test_pending_list_is_rejudged_each_time(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import database_manager, reset_database_manager
    from asset_sync.config import load_config

    reset_database_manager()
    try:
        app = create_app()
        config, manager = load_config(), database_manager()
        put_itsm(config, manager, PREV_END, BASE_ASSETS)
        put_vcenter(config, manager, PREV_END, BASE_VMS)
        put_itsm(config, manager, DAY_VM, BASE_ASSETS)
        put_vcenter(config, manager, DAY_VM, BASE_VMS + [NEW_VM])      # vCenter 에만 생김
        client = app.test_client()
        with client.session_transaction() as session:
            session["user"] = {"id": 1, "username": "hong", "role": "user", "name": "홍"}

        moves = client.get("/api/asset-sync/server-status?month=2026-10").get_json()["movements"]
        assert moves["created"]["total"] == 0                          # 대수(ITSM) 엔 아직 없음
        assert [(p["hostname"], p["kind"], p["itsm_action"]) for p in moves["pending"]] == [
            ("newsrv", "CREATED", "ITSM 에 자산 등록")]

        # ITSM 에 넣으면 다음 조회에서 미반영이 사라지고 [생성] 으로 옮겨 간다.
        put_itsm(config, manager, DAY_ITSM, BASE_ASSETS + [asset("CM099", "newsrv", "10.0.0.99")])
        moves = client.get("/api/asset-sync/server-status?month=2026-10").get_json()["movements"]
        assert moves["pending"] == []
        assert [i["cm_id"] for i in moves["created"]["items"]] == ["CM099"]
    finally:
        reset_database_manager()


# ── 필수 정보 누락 ──────────────────────────────────────────────────────
def test_missing_required_info_is_flagged_everywhere(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import database_manager, reset_database_manager
    from asset_sync.config import load_config

    reset_database_manager()
    try:
        app = create_app()
        config, manager = load_config(), database_manager()
        bad = asset("CM099", "newsrv", "10.0.0.99")
        bad["raw"].update({"CM_OS_VERSION": "", "CM_EOL_DT": "미정"})   # 버전 없음, EOSL 못 읽음
        no_eosl = asset("CM098", "eolsrv", "10.0.0.98")
        no_eosl["raw"]["CM_EOL_DT"] = ""
        put_itsm(config, manager, PREV_END, BASE_ASSETS)
        put_itsm(config, manager, DAY_ITSM, BASE_ASSETS + [bad, no_eosl])
        client = app.test_client()
        with client.session_transaction() as session:
            session["user"] = {"id": 1, "username": "hong", "role": "user", "name": "홍"}

        body = client.get("/api/asset-sync/server-status?month=2026-10").get_json()
        missing = body["missing"]
        assert missing["count"] == 2 and missing["checked"] == 5
        by_id = {item["cm_id"]: item["missing"] for item in missing["items"]}
        assert by_id["CM098"] == ["EOSL"]
        assert "EOSL 형식(미정)" in by_id["CM099"]
        assert missing["by_field"]["EOSL"] == 1 and missing["by_field"]["EOSL 형식"] == 1
        # 변동 내역 줄에도 같은 표시
        created = {i["cm_id"]: i for i in body["movements"]["created"]["items"]}
        assert created["CM098"]["missing"] == ["EOSL"]
        # 일간 줄에도
        digest = client.get(f"/api/asset-sync/change-digest?start={DAY_ITSM}T00:00:00&end={DAY_ITSM}T23:59:59").get_json()
        rows = {r["itsm_cm_id"]: r for r in digest["rows"]}
        assert rows["CM098"]["missing"] == ["EOSL"] and digest["summary"]["missing_required"] == 2
        # Excel 시트
        export = client.get("/api/asset-sync/server-status/export?month=2026-10&section=missing")
        assert export.status_code == 200, export.get_data(as_text=True)[:300]
    finally:
        reset_database_manager()


def test_a_deleted_record_is_not_flagged_as_missing_os(portal) -> None:
    """지워진 자산은 원본(raw)만 남는다. OS 코드를 이름으로 못 바꾸면 'OS 누락' 으로 잘못 뜬다."""
    config, manager = portal
    setup_removal(config, manager, [])
    put_itsm(config, manager, DAY_ITSM, BASE_ASSETS)          # CM098 레코드 삭제
    row = day_digest(config, manager, DAY_ITSM)["rows"][0]
    assert row["count_effect"] == -1 and row["missing"] == []
