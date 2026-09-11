"""서버 현황·EOSL 현황이 첨부 양식대로 나오는지 확인한다.

양식("서버현황 양식.xlsx", "EOSL 현황 양식.xlsx")의 구조:

    구분 | UNIX(HP, IBM) | X86(Linux, Windows) | 기타 | 소계
    IDC  | ...
    DR   | ...
    계   | ...

물리서버 표의 소계와 EOSL '서버' 행 합계가 같고, 전체 표의 소계와 EOSL 'OS' 행
합계가 같다. 즉 한 번 고른 대상을 두 장표가 함께 쓴다. 그 성질을 테스트로 지킨다.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.server_status_service import (
    OTHER_GROUP,
    ServerStatusService,
    eosl_year,
    os_group,
)

PHYSICAL = "CMSVRCATCD010"
LOGICAL = "CMSVRCATCD020"


def asset(cm_id, *, os_family="Linux Redhat", place="IDC-1F", physical=False,
          eosl="2030-12-31", os_code="CMCIOSCD010", os_version="8.6", name=None):
    """ITSM 한 줄. raw 에는 실제 컬럼 이름을 쓴다."""
    return {
        "cm_id": cm_id,
        "normalized_hostname": f"host-{cm_id}",
        "primary_ip": f"10.0.0.{int(cm_id[-3:]) % 250 + 1}",
        "os_family": os_family,
        "server_category_code": PHYSICAL if physical else LOGICAL,
        "status_code": "CMSTA010",
        "raw": {
            "CM_ID": cm_id, "CM_NAME": name or f"업무-{cm_id}",
            "CM_HOSTNAME": f"host-{cm_id}", "CM_IP": f"10.0.0.{int(cm_id[-3:]) % 250 + 1}",
            "CM_OS": os_code, "CM_OS_VERSION": os_version, "CM_EOL_DT": eosl,
            "CM_PLACE": place,
        },
    }


class FakeRepo:
    """스냅샷 id → 레코드 묶음."""

    def __init__(self, snapshots: dict[int, list[dict]]) -> None:
        self._snapshots = snapshots

    def load_itsm_records(self, snapshot_id: int, with_raw: bool = True):
        return {item["cm_id"]: item for item in self._snapshots[snapshot_id]}


def service(snapshots: dict[int, list[dict]], **server_status) -> ServerStatusService:
    config = AppConfig(root_dir=Path("/tmp"), server_status=server_status)
    return ServerStatusService(config, FakeRepo(snapshots))


# ── OS 묶음 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("os_family,expected", [
    ("HP-UX", "HP"),
    ("AIX", "IBM"),
    ("Linux Redhat", "Linux"),
    ("CentOS", "Linux"),
    ("Ubuntu", "Linux"),
    ("Rocky", "Linux"),
    ("WINDOWS", "Windows"),
    ("NUTANIX", OTHER_GROUP),
    ("ESXi", OTHER_GROUP),
    ("XenOS", OTHER_GROUP),
    ("Appliance", OTHER_GROUP),
    ("기타", OTHER_GROUP),
    (None, OTHER_GROUP),
])
def test_every_os_lands_in_the_right_column(os_family, expected):
    """기타는 HP-UX · AIX · Linux 계열 · Windows 를 뺀 나머지다."""
    from asset_sync.services.server_status_service import DEFAULT_OS_GROUPS
    assert os_group(os_family, DEFAULT_OS_GROUPS) == expected


# ── 서버 현황 표 ───────────────────────────────────────────────────────────

def sample() -> list[dict]:
    """IDC 논리 Linux 2 · IDC 물리 Windows 1 · DR 논리 AIX 1 · DR 물리 기타 1"""
    return [
        asset("CM001", os_family="Linux Redhat", place="IDC-1F"),
        asset("CM002", os_family="CentOS", place="IDC-2F"),
        asset("CM003", os_family="WINDOWS", place="IDC-1F", physical=True),
        asset("CM004", os_family="AIX", place="DR센터"),
        asset("CM005", os_family="NUTANIX", place="DR센터", physical=True),
    ]


def test_the_table_has_the_rows_and_columns_of_the_form():
    result = service({1: sample()}).status(1)
    table = result["all"]["table"]
    assert table["columns"] == ["HP", "IBM", "Linux", "Windows", "기타", "소계"]
    assert list(table["rows"]) == ["IDC", "DR", "계"]


def test_the_counts_add_up_by_row_and_column():
    """양식의 합계가 맞아야 한다. IDC + DR = 계, OS 합 = 소계."""
    table = service({1: sample()}).status(1)["all"]["table"]
    idc, dr, total = table["rows"]["IDC"], table["rows"]["DR"], table["rows"]["계"]

    assert idc == {"HP": 0, "IBM": 0, "Linux": 2, "Windows": 1, "기타": 0, "소계": 3}
    assert dr == {"HP": 0, "IBM": 1, "Linux": 0, "Windows": 0, "기타": 1, "소계": 2}
    for column in table["columns"]:
        assert total[column] == idc[column] + dr[column], column
    assert total["소계"] == 5


def test_the_physical_table_counts_only_physical_servers():
    result = service({1: sample()}).status(1)
    assert result["physical"]["table"]["rows"]["계"]["소계"] == 2
    assert result["physical"]["table"]["rows"]["IDC"]["Windows"] == 1
    assert result["physical"]["table"]["rows"]["DR"]["기타"] == 1


def test_dr_is_decided_by_the_configured_keywords():
    records = [asset("CM001", place="분당 재해복구센터"), asset("CM002", place="본사 IDC")]
    table = service({1: records}).status(1)["all"]["table"]
    assert table["rows"]["DR"]["소계"] == 1
    assert table["rows"]["IDC"]["소계"] == 1


def test_the_dr_keywords_can_be_changed_in_settings():
    records = [asset("CM001", place="분당센터")]
    table = service({1: records}, dr_keywords=["분당"]).status(1)["all"]["table"]
    assert table["rows"]["DR"]["소계"] == 1


# ── 제외 목록 ──────────────────────────────────────────────────────────────

def test_a_row_with_no_os_and_no_eosl_is_excluded_and_listed():
    """ITSM 에서 서버를 일괄로 가져오되, 서버가 아닌 것은 목록으로 보여야 한다."""
    records = sample() + [
        asset("CM900", os_code="", os_version="", eosl="", name="네트워크 스위치"),
    ]
    result = service({1: records}).status(1)
    assert result["all"]["table"]["rows"]["계"]["소계"] == 5, "제외 대상이 집계에 섞였다"
    assert result["excluded"]["count"] == 1
    item = result["excluded"]["items"][0]
    assert item["cm_id"] == "CM900"
    # 어떤 장비가 빠졌는지 알아볼 수 있어야 한다.
    assert item["hostname"] and item["primary_ip"] and item["service_name"] == "네트워크 스위치"
    assert "CM_OS" in result["excluded"]["reason"]


def test_a_row_with_only_one_of_the_three_filled_is_kept():
    """세 값이 **모두** 비어 있을 때만 제외한다."""
    records = [asset("CM001", os_code="", os_version="", eosl="2027-01-01")]
    assert service({1: records}).status(1)["excluded"]["count"] == 0


def test_the_exclusion_columns_can_be_changed_in_settings():
    records = [asset("CM001", os_code="", os_version="", eosl="2027-01-01")]
    result = service({1: records}, exclude_when_all_empty=["CM_OS", "CM_OS_VERSION"]).status(1)
    assert result["excluded"]["count"] == 1


# ── 전월 대비 증감 ─────────────────────────────────────────────────────────

def test_the_delta_against_last_month_is_reported():
    """양식의 (+8) · (-2) 에 해당한다."""
    previous = [asset("CM001"), asset("CM002"), asset("CM003", os_family="WINDOWS", physical=True)]
    current = [asset("CM001"), asset("CM004"), asset("CM005")]   # Linux 1 늘고 Windows 1 줄었다
    result = service({1: current, 2: previous}).status(1, previous_snapshot_id=2)
    delta = result["all"]["delta"]
    assert delta["IDC"]["Linux"] == 1
    assert delta["IDC"]["Windows"] == -1
    assert delta["계"]["소계"] == 0


def test_there_is_no_delta_without_a_previous_snapshot():
    assert service({1: sample()}).status(1)["all"]["delta"] == {}


# ── 신규·삭제 상세 ─────────────────────────────────────────────────────────

def test_movements_break_down_by_location_kind_and_os():
    """양식: IDC : 논리서버 12대 (Linux 7대 ...) , 물리서버 16대 (...)"""
    previous = [asset("CM001")]
    current = [
        asset("CM001"),
        asset("CM010", os_family="Linux Redhat", place="IDC-1F"),
        asset("CM011", os_family="WINDOWS", place="IDC-1F", physical=True),
        asset("CM012", os_family="AIX", place="DR센터"),
    ]
    moves = service({1: current, 2: previous}).movements(1, 2)

    created = moves["created"]
    assert created["total"] == 3
    assert created["locations"]["IDC"]["total"] == 2
    assert created["locations"]["IDC"]["kinds"]["논리서버"]["os"] == {"Linux": 1}
    assert created["locations"]["IDC"]["kinds"]["물리서버"]["os"] == {"Windows": 1}
    assert created["locations"]["DR"]["kinds"]["논리서버"]["os"] == {"IBM": 1}
    assert moves["removed"]["total"] == 0


def test_removed_assets_are_counted_from_the_previous_snapshot():
    previous = [asset("CM001"), asset("CM002", os_family="HP-UX", physical=True)]
    current = [asset("CM001")]
    moves = service({1: current, 2: previous}).movements(1, 2)
    assert moves["removed"]["total"] == 1
    assert moves["removed"]["locations"]["IDC"]["kinds"]["물리서버"]["os"] == {"HP": 1}


# ── EOSL ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("2026-12-31", 2026), ("20261231", 2026), ("2026", 2026),
    ("9999-12-31", 9999), ("", None), (None, None), ("-", None), ("미정", None),
])
def test_only_the_year_matters(value, expected):
    assert eosl_year(value) == expected


def test_eosl_buckets_follow_the_form():
    records = [
        asset("CM001", eosl="2020-01-01"),   # 이전
        asset("CM002", eosl="2026-06-30"),   # 당년
        asset("CM003", eosl="2027-03-01"),   # +1
        asset("CM004", eosl="2031-01-01"),   # +2 이상
        asset("CM005", eosl="9999-12-31"),   # 계획 없음
        # EOSL 만 비어 있으면 서버로는 남고 '미사용' 으로 센다.
        asset("CM006", eosl=""),
    ]
    result = service({1: records}).eosl(1, today=date(2026, 9, 11))
    assert result["columns"] == ["2026년 이전", "2026년", "2027년", "2028년 이상", "계획 없음", "미사용"]
    counts = result["all"]["counts"]
    assert counts == {"2026년 이전": 1, "2026년": 1, "2027년": 1,
                      "2028년 이상": 1, "계획 없음": 1, "미사용": 1}
    assert result["all"]["total"] == 6


def test_9999_is_counted_as_no_plan():
    records = [asset("CM001", eosl="9999-12-31")]
    result = service({1: records}).eosl(1, today=date(2026, 1, 1))
    assert result["all"]["counts"]["계획 없음"] == 1


def test_the_eosl_row_total_matches_the_server_status_total():
    """양식에서 물리 소계(925)=EOSL 서버 행, 전체 소계(2,668)=EOSL OS 행이다."""
    records = sample() + [asset("CM900", os_code="", os_version="", eosl="")]
    svc = service({1: records})
    status = svc.status(1)
    eosl = svc.eosl(1, today=date(2026, 9, 11))

    assert eosl["all"]["total"] == status["all"]["table"]["rows"]["계"]["소계"]
    assert eosl["physical"]["total"] == status["physical"]["table"]["rows"]["계"]["소계"]
    assert sum(eosl["all"]["counts"].values()) == eosl["all"]["total"]
    assert sum(eosl["physical"]["counts"].values()) == eosl["physical"]["total"]


def test_the_criteria_are_reported_so_a_mismatch_can_be_argued():
    """대수가 ITSM 과 다를 때 기준이 안 보이면 어느 쪽이 틀렸는지 따질 수 없다."""
    criteria = service({1: sample()}).status(1)["criteria"]
    assert criteria["eosl_field"] == "CM_EOL_DT"
    assert criteria["location_field"] == "CM_PLACE"
    assert criteria["os_groups"]["IBM"] == ["AIX"]
    assert criteria["physical_code"] == PHYSICAL


# ── 실제 DB 로 한 번 ───────────────────────────────────────────────────────

def test_it_works_against_a_real_snapshot(tmp_path: Path):
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/s.db"))
    manager = create_manager(config)
    manager.initialize()
    now = datetime.now()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", now.isoformat())
        repo.finish_collection_run(run, "SUCCESS", 2, now.isoformat(), ["ALL"])
        snapshot_id = repo.create_snapshot("ITSM", now.date().isoformat(), now.isoformat(),
                                          run, "SUCCESS", 2, "hash")
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [
                (snapshot_id, "CM001", "h1", "10.0.0.1", 4, 8192, "Linux Redhat", "8.6",
                 "CMSTA010", LOGICAL, "CMOWNCATCD0010", "2030-01-01", "h",
                 json.dumps({"CM_ID": "CM001", "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6",
                             "CM_EOL_DT": "2030-01-01", "CM_PLACE": "IDC"}, ensure_ascii=False)),
                (snapshot_id, "CM002", "h2", "10.0.0.2", 8, 16384, "AIX", "7.2",
                 "CMSTA010", PHYSICAL, "CMOWNCATCD0010", "2027-01-01", "h",
                 json.dumps({"CM_ID": "CM002", "CM_OS": "CMCIOSCD070", "CM_OS_VERSION": "7.2",
                             "CM_EOL_DT": "2027-01-01", "CM_PLACE": "DR센터"}, ensure_ascii=False)),
            ],
        )
        conn.commit()

    with manager.connect() as conn:
        svc = ServerStatusService(config, AssetRepository(conn))
        status = svc.status(snapshot_id)
        eosl = svc.eosl(snapshot_id, today=date(2026, 9, 11))

    assert status["all"]["table"]["rows"]["IDC"]["Linux"] == 1
    assert status["all"]["table"]["rows"]["DR"]["IBM"] == 1
    assert status["physical"]["table"]["rows"]["계"]["소계"] == 1
    assert eosl["all"]["counts"]["2027년"] == 1


# ── 화면이 부르는 API ──────────────────────────────────────────────────────

@pytest.fixture()
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import database_manager, reset_database_manager

    reset_database_manager()
    app = create_app()
    yield app, database_manager()
    reset_database_manager()


def seed_snapshot(manager, day: datetime, rows: list[dict]) -> int:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", day.isoformat())
        repo.finish_collection_run(run, "SUCCESS", len(rows), day.isoformat(), ["ALL"])
        snapshot_id = repo.create_snapshot("ITSM", day.date().isoformat(), day.isoformat(),
                                          run, "SUCCESS", len(rows), "hash")
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [(snapshot_id, row["cm_id"], row["normalized_hostname"], row["primary_ip"], 4, 8192,
              row["os_family"], "8", "CMSTA010", row["server_category_code"], "CMOWNCATCD0010",
              row["raw"]["CM_EOL_DT"], "h", json.dumps(row["raw"], ensure_ascii=False))
             for row in rows],
        )
        conn.commit()
    return snapshot_id


def admin(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    return client


def test_the_check_screens_open(portal):
    app, _ = portal
    client = admin(app)
    for url, screen in (("/daily-check", "page-daily_check"),
                        ("/weekly-check", "page-weekly_check"),
                        ("/monthly-check", "page-monthly_check")):
        html = client.get(url, follow_redirects=True).get_data(as_text=True)
        assert f'id="{screen}"' in html
        active = re.findall(r'<div class="page\s+active"\s+id="(page-[a-z_]+)"', html)
        assert active == [screen], f"{url} 에서 활성 화면이 {active} 입니다"
        assert "시스템 파트" in html, "사이드바에 시스템 파트가 없습니다"


def test_the_api_says_so_when_there_is_no_snapshot(portal):
    app, _ = portal
    body = admin(app).get("/api/asset-sync/server-status").get_json()
    assert body["status"] == "NO_SNAPSHOT"
    assert "스냅샷이 없습니다" in body["message"]


def test_the_api_compares_against_last_month(portal):
    app, manager = portal
    seed_snapshot(manager, datetime.now() - timedelta(days=40), [
        asset("CM001"), asset("CM002", os_family="WINDOWS", physical=True),
    ])
    seed_snapshot(manager, datetime.now(), [
        asset("CM001"), asset("CM003", os_family="CentOS"),
    ])
    body = admin(app).get("/api/asset-sync/server-status").get_json()

    assert body["status"] == "SUCCESS"
    assert body["previous_as_of"], "전월 스냅샷을 찾지 못했습니다"
    assert body["all"]["table"]["rows"]["계"]["소계"] == 2
    assert body["all"]["delta"]["계"]["Linux"] == 1
    assert body["all"]["delta"]["계"]["Windows"] == -1
    assert body["movements"]["created"]["total"] == 1
    assert body["movements"]["removed"]["total"] == 1
    # 두 장표가 같은 대상을 쓴다.
    assert body["eosl"]["all"]["total"] == body["all"]["table"]["rows"]["계"]["소계"]


def test_a_bad_month_is_refused(portal):
    app, _ = portal
    response = admin(app).get("/api/asset-sync/server-status?month=2026-13-99")
    assert response.status_code == 400
    assert "YYYY-MM" in response.get_json()["error"]
