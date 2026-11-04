"""통합서버 자원사용현황의 VM 대수는 vCenter 와 ITSM 이 함께 바뀌어야 움직인다.

- 추가: vCenter 에 VM 이 있고 ITSM 이 운영 · 대기가 된 날
- 삭제: ITSM 이 미사용 · 매각/폐기가 된 날. vCenter 에서만 지운 VM 은 '삭제 대기' 로 대수에 남는다
- vMotion: 늘 반영(전체 대수 그대로, 통합기별 대수만 옮겨 감)
- 두 날 사이 추가 - 삭제 = VM 대수 변화이고, 그 VM 들을 목록 · Excel 로 뽑을 수 있다
"""

from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import unquote

import pytest

GB = 1024

# VM 이름 → (10/1 통합기, 11/1 통합기, 10/1 ITSM 상태, 11/1 ITSM 상태). None = 없음
PLAN = {
    "keep":        ("esxi-a", "esxi-a", "CMSTA010", "CMSTA010"),
    "vmo":         ("esxi-a", "esxi-b", "CMSTA010", "CMSTA010"),   # vMotion
    "new-ok":      (None,     "esxi-b", None,       "CMSTA010"),   # vCenter 생성 + ITSM 운영
    "new-wait":    (None,     "esxi-b", None,       None),         # vCenter 에만 생김
    "late-reg":    ("esxi-a", "esxi-a", None,       "CMSTA050"),   # 늦게 ITSM 대기로 등록
    "del-ok":      ("esxi-a", None,     "CMSTA010", "CMSTA020"),   # vCenter 삭제 + ITSM 미사용
    "del-wait":    ("esxi-a", None,     "CMSTA010", "CMSTA010"),   # vCenter 에서만 지움
    "itsm-retire": ("esxi-a", "esxi-a", "CMSTA010", "CMSTA060"),   # ITSM 폐기, VM 은 남음
}


def seed(conn, day: str, side: int) -> None:
    from asset_sync.repositories import AssetRepository

    repo = AssetRepository(conn)
    conn.execute(
        "INSERT INTO resource_usage_run(period_start, period_end, started_at, status) VALUES(?, ?, ?, 'SUCCESS')",
        (day, day, day))
    run = int(conn.execute("SELECT MAX(id) AS n FROM resource_usage_run").fetchone()["n"])
    for esxi in ("esxi-a", "esxi-b"):
        conn.execute(
            "INSERT INTO host_resource_usage_daily(run_id, stat_date, vcenter_id, service_name, cluster_name,"
            " esxi_host, vm_count, allocated_cpu_cores, allocated_memory_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct,"
            " mem_avg_pct, sample_count, collection_status, raw_json, created_at)"
            " VALUES(?,?,'VC1','업무A','CL1',?,0,64,?,80,40,70,35,12,'SUCCESS','{}',?)",
            (run, day, esxi, 512 * GB, day))
    statuses = []
    for name, plan in PLAN.items():
        host, status = plan[side], plan[2 + side]
        if host:
            conn.execute(
                "INSERT INTO vm_resource_usage_daily(run_id, stat_date, vcenter_snapshot_id, asset_key, vcenter_id,"
                " service_name, cluster_name, esxi_host, vm_uuid, vm_name, power_state, allocated_cpu_cores,"
                " allocated_memory_mb, sample_count, inventory_status, collection_status, raw_json, created_at)"
                " VALUES(?,?,NULL,?,'VC1','업무A','CL1',?,?,?,'poweredOn',4,?,12,'CURRENT','SUCCESS','{}',?)",
                (run, day, f"VC1|{name}", host, f"uuid-{name}", name, 16 * GB, day))
        if status:
            statuses.append((name, status))
    itsm_run = repo.start_collection_run("ITSM", f"{day}T07:00:00")
    repo.finish_collection_run(itsm_run, "SUCCESS", len(statuses), f"{day}T07:00:00", ["ALL"])
    sid = repo.create_snapshot("ITSM", day, f"{day}T07:00:00", itsm_run, "SUCCESS", len(statuses), f"h{day}")
    conn.executemany(
        "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,ip_json,cpu_cores,"
        "memory_mb,os_family,os_version,status_code,server_category_code,environment_code,eos_value,record_hash,"
        "raw_json) VALUES(?,?,?,NULL,'[]',4,16384,'Linux Redhat','8',?,'HW0101','CMOWNCATCD0010','2030','h',?)",
        [(sid, f"CM-{name}", name, status,
          json.dumps({"CM_ID": f"CM-{name}", "CM_NAME": f"업무-{name}", "CM_STA_CD": status}, ensure_ascii=False))
         for name, status in statuses])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import database_manager, reset_database_manager

    reset_database_manager()
    app = create_app()
    with database_manager().connect() as conn:
        seed(conn, "2026-10-01", 0)
        seed(conn, "2026-11-01", 1)
        conn.commit()
    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "hong", "role": "user", "name": "홍"}
    yield test_client
    reset_database_manager()


def hosts(body):
    return {h["esxi_host"]: h for h in body["hosts"]}


def test_vm_count_needs_itsm_too(client) -> None:
    oct1 = client.get("/api/asset-sync/resource-usage?as_of=2026-10-01").get_json()
    nov1 = client.get("/api/asset-sync/resource-usage?as_of=2026-11-01").get_json()
    assert oct1["asset_basis"]["applied"] is True
    # 10/1: vCenter 6대 중 late-reg(ITSM 미등록) 를 뺀 5대
    assert (oct1["summary"]["vm_count"], oct1["summary"]["vcenter_vm_count"]) == (5, 6)
    # 11/1: keep · vmo · new-ok · late-reg + 삭제 대기 del-wait = 5대. new-wait · itsm-retire 는 빠짐
    assert (nov1["summary"]["vm_count"], nov1["summary"]["vcenter_vm_count"]) == (5, 6)
    assert nov1["summary"]["vm_not_counted"] == 2          # new-wait(미등록) · itsm-retire(폐기인데 VM 남음)
    assert nov1["summary"]["vm_pending_remove"] == 1       # del-wait
    a, b = hosts(nov1)["esxi-a"], hosts(nov1)["esxi-b"]
    assert (a["vm_count"], b["vm_count"]) == (3, 2)         # vMotion 한 vmo 는 b 로 옮겨 감
    assert (hosts(oct1)["esxi-a"]["vm_count"], hosts(oct1)["esxi-b"]["vm_count"]) == (5, 0)


def test_moves_explain_the_difference(client) -> None:
    body = client.get("/api/asset-sync/resource-usage/moves?from=2026-10-01&to=2026-11-01").get_json()
    by = {}
    for row in body["rows"]:
        by.setdefault(row["change"], set()).add(row["vm_name"])
    assert by["ADDED"] == {"new-ok", "late-reg"}
    assert by["REMOVED"] == {"del-ok", "itsm-retire"}
    assert by["MOVED"] == {"vmo"}
    assert by["PENDING_ADD"] == {"new-wait"}
    assert by["PENDING_REMOVE"] == {"del-wait"}
    assert body["net"] == 0
    # 통합기별 순증감 = 그 통합기 VM 대수 변화
    net = {h["esxi_host"]: h["net"] for h in body["by_host"]}
    assert net == {"esxi-a": -2, "esxi-b": 2}
    late = next(r for r in body["rows"] if r["vm_name"] == "late-reg")
    assert "ITSM" in late["reason"] and late["itsm_after"] == "대기"
    assert next(r for r in body["rows"] if r["vm_name"] == "new-ok")["service_name"] == "업무-new-ok"


def test_moves_excel(client) -> None:
    response = client.get("/api/asset-sync/resource-usage/moves/export?from=2026-10-01&to=2026-11-01")
    assert response.status_code == 200
    assert "VM추가삭제" in unquote(response.headers["Content-Disposition"])
    from openpyxl import load_workbook

    sheet = load_workbook(BytesIO(response.data))["VM추가삭제"]
    rows = [r for r in sheet.iter_rows(min_row=2, values_only=True) if any(r)]
    assert len(rows) == 7


def test_without_itsm_it_counts_vcenter_as_before(client) -> None:
    from application.db import database_manager

    with database_manager().connect() as conn:
        conn.execute("DELETE FROM itsm_asset_snapshot")
        conn.execute("DELETE FROM snapshot WHERE source='ITSM'")
        conn.commit()
    body = client.get("/api/asset-sync/resource-usage?as_of=2026-11-01").get_json()
    assert body["asset_basis"]["applied"] is False
    assert body["summary"]["vm_count"] == 6
    assert client.get("/api/asset-sync/resource-usage/moves?from=2026-10-01&to=2026-11-01").get_json()["applied"] is False


def test_the_monthly_report_change_block_uses_the_same_moves(client) -> None:
    """월간 보고서의 '통합기별 서버 증감' 도 같은 판정: 추가 2 · 삭제 2 · vMotion 은 이동으로."""
    from datetime import date

    from application.db import database_manager
    from asset_sync.config import load_config
    from asset_sync.repositories import AssetRepository
    from asset_sync.services.monthly_report_service import MonthlyReportService

    with database_manager().connect() as conn:
        data = MonthlyReportService(load_config(), AssetRepository(conn), unit="ESXI")._vm_changes(date(2026, 11, 1))
    created = sorted(n for b in data.values() for n in b["created"])
    removed = sorted(n for b in data.values() for n in b["removed"])
    assert created == ["late-reg", "new-ok"] and removed == ["del-ok", "itsm-retire"]
    assert data["esxi-b"]["moved_in"] == ["vmo"] and data["esxi-a"]["moved_out"] == ["vmo"]


def add_vm(conn, day: str, name: str, host: str = "esxi-b") -> None:
    run = int(conn.execute("SELECT MAX(id) AS n FROM resource_usage_run WHERE period_start=?", (day,)).fetchone()["n"])
    conn.execute(
        "INSERT INTO vm_resource_usage_daily(run_id, stat_date, vcenter_snapshot_id, asset_key, vcenter_id,"
        " service_name, cluster_name, esxi_host, vm_uuid, vm_name, power_state, allocated_cpu_cores,"
        " allocated_memory_mb, sample_count, inventory_status, collection_status, raw_json, created_at)"
        " VALUES(?,?,NULL,?,'VC1','업무A','CL1',?,?,?,'poweredOn',1,1024,12,'CURRENT','SUCCESS','{}',?)",
        (run, day, f"VC1|{name}", host, f"uuid-{name}", name, day))


def test_vcls_and_manual_exclusions_go_to_their_own_column(client) -> None:
    """vCLS · 수동 제외 VM 은 ITSM 에 없어도 '추가 대기' 가 아니라 [제외] 다. 대수에도 대기에도 안 든다."""
    from application.db import database_manager

    with database_manager().connect() as conn:
        add_vm(conn, "2026-11-01", "vCLS-1a2b3c")
        add_vm(conn, "2026-11-01", "test-tmp")
        conn.execute("INSERT INTO asset_exclusion(source, asset_key, mode, reason, updated_at)"
                     " VALUES('RVTOOLS','VC1|test-tmp','EXCLUDE','시험용','2026-11-01')")
        conn.commit()
    body = client.get("/api/asset-sync/resource-usage?as_of=2026-11-01").get_json()
    assert body["summary"]["vm_count"] == 5 and body["summary"]["vm_not_counted"] == 2   # 그대로
    assert body["summary"]["vm_excluded"] == 2 and hosts(body)["esxi-b"]["excluded_count"] == 2
    moves = client.get("/api/asset-sync/resource-usage/moves?from=2026-10-01&to=2026-11-01").get_json()
    pending = {r["vm_name"] for r in moves["rows"] if r["change"] == "PENDING_ADD"}
    excluded = {r["vm_name"]: r["exclude_label"] for r in moves["rows"] if r["change"] == "EXCLUDED"}
    assert pending == {"new-wait"}
    assert set(excluded) == {"vCLS-1a2b3c", "test-tmp"} and "시험용" in excluded["test-tmp"]
    assert moves["excluded_total"] == 2
