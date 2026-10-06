"""월간점검 통합서버 자원사용현황은 ESXi 를 한 대씩 다 늘어놓아야 한다.

클러스터 합계만 보여 주면 "어느 ESXi 가 모자란지" 를 알 수 없다. 증설은 ESXi
단위로 하므로 줄도 ESXi 단위여야 하고, 클러스터 합계는 그 위에 머리줄로 얹는다.
"""

from __future__ import annotations

import re
from pathlib import Path

from asset_sync.config import AppConfig
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.resource_usage_service import VMResourceUsageExportService

STAT_DATE = "2026-09-01"
GB = 1024
ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "templates" / "pages" / "monthly_check.html"
SCRIPT = ROOT / "templates" / "partials" / "js" / "monthly_check.html"


def _service(tmp_path: Path):
    cfg = AppConfig(
        root_dir=tmp_path,
        sqlite_path=Path("data/monthly.db"),
        rvtools={"resource_usage": {"enabled": False}, "vcenters": []},
    )
    manager = SQLiteManager(cfg.database_path)
    manager.initialize()
    return cfg, manager


def _seed(conn, hosts: list[dict], vms: list[dict]) -> None:
    conn.execute(
        "INSERT INTO resource_usage_run(period_start, period_end, started_at, status)"
        " VALUES(?, ?, ?, 'SUCCESS')",
        (STAT_DATE, STAT_DATE, STAT_DATE),
    )
    for host in hosts:
        conn.execute(
            "INSERT INTO host_resource_usage_daily(run_id, stat_date, vcenter_id, service_name,"
            " cluster_name, esxi_host, vm_count, allocated_cpu_cores, allocated_memory_mb,"
            " cpu_max_pct, cpu_avg_pct, mem_max_pct, mem_avg_pct, sample_count, collection_status,"
            " raw_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'SUCCESS', '{}', ?)",
            (1, STAT_DATE, host["vcenter_id"], host["service_name"], host["cluster_name"],
             host["esxi_host"], host["vm_count"], host["cpu"], host["memory_mb"],
             80.0, 40.0, 70.0, 35.0, 12, STAT_DATE),
        )
    for vm in vms:
        conn.execute(
            "INSERT INTO vm_resource_usage_daily(run_id, stat_date, vcenter_snapshot_id, asset_key,"
            " vcenter_id, service_name, cluster_name, esxi_host, vm_uuid, vm_name, power_state,"
            " allocated_cpu_cores, allocated_memory_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct,"
            " mem_avg_pct, sample_count, inventory_status, collection_status, raw_json, created_at)"
            " VALUES(?,?,NULL,?,?,?,?,?,?,?, 'poweredOn', ?,?, 30.0, 10.0, 60.0, 30.0, 12,"
            " 'CURRENT', 'SUCCESS', '{}', ?)",
            (1, STAT_DATE, vm["vm_name"], vm["vcenter_id"], vm["service_name"], vm["cluster_name"],
             vm["esxi_host"], vm["vm_name"], vm["vm_name"], vm["cpu"], vm["memory_mb"], STAT_DATE),
        )
    conn.commit()


def _fleet(clusters: int = 3, per_cluster: int = 4):
    hosts, vms = [], []
    for c in range(clusters):
        for h in range(per_cluster):
            esxi = f"esxi-{c}{h:02d}"
            hosts.append({
                "vcenter_id": "VC1", "service_name": "업무A", "cluster_name": f"CL{c}",
                "esxi_host": esxi, "vm_count": 2, "cpu": 64, "memory_mb": 512 * GB,
            })
            for index in range(2):
                vms.append({
                    "vcenter_id": "VC1", "service_name": "업무A", "cluster_name": f"CL{c}",
                    "esxi_host": esxi, "vm_name": f"{esxi}-vm{index}",
                    "cpu": 4, "memory_mb": 16 * GB,
                })
    return hosts, vms


def test_every_esxi_comes_back_as_its_own_row(tmp_path: Path) -> None:
    """클러스터 3 묶음 × ESXi 4 대면 호스트 12 줄, 클러스터 3 줄이다."""
    cfg, manager = _service(tmp_path)
    hosts, vms = _fleet()
    with manager.connect() as conn:
        _seed(conn, hosts, vms)
        result = VMResourceUsageExportService(cfg, AssetRepository(conn)).summary(STAT_DATE, STAT_DATE)

    assert len(result["hosts"]) == 12
    assert len(result["clusters"]) == 3
    names = {row["esxi_host"] for row in result["hosts"]}
    assert len(names) == 12, "ESXi 이름이 뭉쳐 버리면 한 대씩 볼 수 없다"
    # 줄마다 그 ESXi 몫만 들어 있어야 한다. 묶음 합계가 섞이면 안 된다.
    for row in result["hosts"]:
        assert row["vm_count"] == 2
        assert row["assigned_cpu_cores"] == 8
        assert row["assigned_memory_gb"] == 32
        assert row["cluster_name"].startswith("CL")


def test_the_cluster_total_equals_the_sum_of_its_hosts(tmp_path: Path) -> None:
    """화면은 ESXi 줄 위에 묶음 합계를 얹는다. 둘이 어긋나면 안 된다."""
    cfg, manager = _service(tmp_path)
    hosts, vms = _fleet()
    with manager.connect() as conn:
        _seed(conn, hosts, vms)
        result = VMResourceUsageExportService(cfg, AssetRepository(conn)).summary(STAT_DATE, STAT_DATE)

    for cluster in result["clusters"]:
        members = [h for h in result["hosts"] if h["cluster_name"] == cluster["cluster_name"]]
        assert cluster["vm_count"] == sum(h["vm_count"] for h in members)
        assert cluster["assigned_cpu_cores"] == sum(h["assigned_cpu_cores"] for h in members)
        assert cluster["allocated_cpu_cores"] == sum(h["allocated_cpu_cores"] for h in members)
        assert cluster["host_count"] == len(members)


def test_the_monthly_screen_draws_one_line_per_esxi() -> None:
    """화면이 클러스터 합계만 그리던 것으로 되돌아가지 않게 못을 박는다."""
    script = SCRIPT.read_text(encoding="utf-8")
    block = script.split("function renderMonthlyIntegratedAgain()", 1)[1].split("\nfunction ", 1)[0]
    assert "monthlyIntegratedGroups" in block
    assert "row-child" in block, "ESXi 낱개 줄이 없다"
    assert "row-group" in block, "클러스터 합계 머리줄이 없다"
    # 묶음은 클러스터 키로, 낱개는 호스트 키로 전월과 견준다.
    groups = script.split("function monthlyIntegratedGroups", 1)[1].split("\nfunction ", 1)[0]
    assert "MONTHLY_HOST_KEY" in groups
    assert "hostLabel" in block, "ESXi 이름을 안 쓰고 있다"


def test_the_filters_on_the_page_are_wired_to_the_renderer() -> None:
    """줄이 수백 개라 검색·접기가 필요하다. 양쪽 id 가 맞아야 작동한다."""
    page = PAGE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    for element_id in ("monthly-integrated-search", "monthly-integrated-fold",
                       "monthly-integrated-changed", "monthly-integrated-shown"):
        assert f'id="{element_id}"' in page, f"{element_id} 가 화면에 없다"
        assert f"'{element_id}'" in script, f"{element_id} 를 스크립트가 안 찾는다"
    # 화면의 onchange/oninput 이 부르는 함수가 실제로 있어야 한다.
    for name in set(re.findall(r'on(?:change|input)="(\w+)\(\)"', page)):
        assert f"function {name}(" in script or f"function {name}(" in page, f"{name} 함수가 없다"
