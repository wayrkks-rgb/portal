"""통합서버 자원사용현황 '기준일 비교': 그날 하루의 값을 보고, 두 날짜 사이 변경량을 본다.

- ``?as_of=2026-11-01`` 은 11월 1일 하루의 값이다(월 1일~오늘 집계가 아니다).
- 그날 수집이 없으면 그 전 마지막 수집값을 쓰고, 어느 날 값인지 알려 준다.
- 그 전 수집도 없으면 404 로 알려 준다.
- Excel 도 같은 날 기준으로, 파일 이름에 기준일을 적는다.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

GB = 1024


def seed_day(conn, day: str, hosts: int, vms_per_host: int) -> None:
    conn.execute(
        "INSERT INTO resource_usage_run(period_start, period_end, started_at, status) VALUES(?, ?, ?, 'SUCCESS')",
        (day, day, day),
    )
    run = int(conn.execute("SELECT MAX(id) AS n FROM resource_usage_run").fetchone()["n"])
    for h in range(hosts):
        esxi = f"esxi-{h:02d}"
        conn.execute(
            "INSERT INTO host_resource_usage_daily(run_id, stat_date, vcenter_id, service_name, cluster_name,"
            " esxi_host, vm_count, allocated_cpu_cores, allocated_memory_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct,"
            " mem_avg_pct, sample_count, collection_status, raw_json, created_at)"
            " VALUES(?,?,'VC1','업무A','CL1',?,?,64,?,80,40,70,35,12,'SUCCESS','{}',?)",
            (run, day, esxi, vms_per_host, 512 * GB, day),
        )
        for v in range(vms_per_host):
            name = f"{esxi}-vm{v}"
            conn.execute(
                "INSERT INTO vm_resource_usage_daily(run_id, stat_date, vcenter_snapshot_id, asset_key, vcenter_id,"
                " service_name, cluster_name, esxi_host, vm_uuid, vm_name, power_state, allocated_cpu_cores,"
                " allocated_memory_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct, mem_avg_pct, sample_count,"
                " inventory_status, collection_status, raw_json, created_at)"
                " VALUES(?,?,NULL,?,'VC1','업무A','CL1',?,?,?,'poweredOn',4,?,30,10,60,30,12,'CURRENT','SUCCESS','{}',?)",
                (run, day, name, esxi, name, name, 16 * GB, day),
            )


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
        seed_day(conn, "2026-10-01", hosts=4, vms_per_host=2)    # 10월 1일: ESXi 4 · VM 8
        seed_day(conn, "2026-10-31", hosts=5, vms_per_host=3)    # 10월 말: ESXi 5 · VM 15 (11/1 은 수집 없음)
    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "hong", "role": "user", "name": "홍"}
    yield test_client
    reset_database_manager()


def test_one_day_is_that_day_only(client) -> None:
    body = client.get("/api/asset-sync/resource-usage?as_of=2026-10-01").get_json()
    assert body["as_of"] == {"requested": "2026-10-01", "actual": "2026-10-01"}
    assert body["period"] == {"start": "2026-10-01", "end": "2026-10-01"}
    assert body["summary"]["host_count"] == 4 and body["summary"]["vm_count"] == 8


def test_a_day_without_collection_uses_the_last_one_before(client) -> None:
    body = client.get("/api/asset-sync/resource-usage?as_of=2026-11-01").get_json()
    assert body["as_of"] == {"requested": "2026-11-01", "actual": "2026-10-31"}
    assert body["summary"]["host_count"] == 5 and body["summary"]["vm_count"] == 15
    # 11/1 ↔ 10/1 변경량: ESXi +1, VM +7
    before = client.get("/api/asset-sync/resource-usage?as_of=2026-10-01").get_json()["summary"]
    assert (body["summary"]["host_count"] - before["host_count"], body["summary"]["vm_count"] - before["vm_count"]) == (1, 7)


def test_no_data_before_the_day_is_reported(client) -> None:
    response = client.get("/api/asset-sync/resource-usage?as_of=2026-09-01")
    assert response.status_code == 404 and response.get_json()["status"] == "NO_DATA"
    assert client.get("/api/asset-sync/resource-usage?as_of=2026-13-01").status_code == 400


def test_the_excel_is_for_that_day(client) -> None:
    response = client.get("/api/asset-sync/resource-usage/export?as_of=2026-11-01")
    assert response.status_code == 200
    name = unquote(response.headers["Content-Disposition"])
    assert "2026-11-01기준" in name and "수집 2026-10-31" in name
    from io import BytesIO

    from openpyxl import load_workbook

    sheet = load_workbook(BytesIO(response.data)).active
    hosts = [row for row in sheet.iter_rows(min_row=2, values_only=True) if any(row)]
    assert len(hosts) == 5
