"""월간 보고 장표가 받은 양식대로 나오는지 확인한다.

이 파일은 보고자료에 **그대로 붙여 쓰는** 것이다. 머리글 위치나 전월·당월 배치가
어긋나면 붙여 쓸 수 없으므로, 양식의 자리를 셀 좌표로 못 박는다.

양식(올려받은 파일)의 첫 시트 구조:

    B2:C4 = '서버' | D2:H2 = 당월(’26.08) | I2:L2 = 전월(’26.07)
    D3 = CPU/MEM  E3 = CPU  F3 = MEM  G3 = 현재 대수  H3 = 디스크
    E4/F4/H4 = 사용률
    B5 부터 자료. B = 위치, C = 통합기명, D = '128C / 896G'
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.monthly_report_service import MonthlyReportService

GB = 1024
BASE_DAY = date(2026, 9, 30)
LAST_MONTH = date(2026, 8, 31)
TWO_MONTHS = date(2026, 7, 31)


# ── 자료 심기 ──────────────────────────────────────────────────────────


def asset(cm_id: str, *, physical: bool = False, place: str = "CMPLACE010",
          os_family: str = "Linux Redhat", eosl: str = "2030-12-31") -> dict:
    return {
        "cm_id": cm_id,
        "normalized_hostname": f"host-{cm_id}",
        "primary_ip": f"10.0.0.{int(cm_id[-3:]) % 250 + 1}",
        "os_family": os_family, "os_version": "8.6", "status_code": "CMSTA010",
        "server_category_code": "CMSVRCATCD010" if physical else "CMSVRCATCD020",
        "raw": {
            "CM_ID": cm_id, "CM_NAME": f"업무-{cm_id}", "CM_HOSTNAME": f"host-{cm_id}",
            "CM_IP": f"10.0.0.{int(cm_id[-3:]) % 250 + 1}", "CM_OS": "CMCIOSCD010",
            "CM_OS_VERSION": "8.6", "CM_EOL_DT": eosl, "CM_PLACE": place,
        },
    }


@pytest.fixture()
def portal(tmp_path: Path):
    config = AppConfig(
        root_dir=tmp_path, sqlite_path=Path("data/report.db"),
        rvtools={"resource_usage": {"enabled": False}, "vcenters": []},
    )
    manager = create_manager(config)
    manager.initialize()
    return config, manager


def seed_itsm(manager, day: date, records: list[dict]) -> int:
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", day.isoformat())
        repo.finish_collection_run(run, "SUCCESS", len(records), day.isoformat())
        snapshot_id = repo.create_snapshot(
            "ITSM", day.isoformat(), f"{day.isoformat()}T07:00:00", run,
            "SUCCESS", len(records), "h",
        )
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [(snapshot_id, r["cm_id"], r["normalized_hostname"], r["primary_ip"], 4, 8192,
              r["os_family"], r["os_version"], r["status_code"], r["server_category_code"],
              "CMOWNCATCD0010", r["raw"].get("CM_EOL_DT"), "h",
              json.dumps(r["raw"], ensure_ascii=False)) for r in records],
        )
        conn.commit()
    return snapshot_id


def seed_usage(manager, day: date, clusters: list[dict]) -> int:
    """통합기 몇 개와 그 위 VM, 그리고 데이터스토어를 심는다."""
    with manager.connect() as conn:
        cur = conn.execute(
            "INSERT INTO resource_usage_run(period_start, period_end, started_at, status)"
            " VALUES(?, ?, ?, 'SUCCESS')",
            (day.replace(day=1).isoformat(), day.isoformat(), day.isoformat()),
        )
        run_id = int(cur.lastrowid)
        for item in clusters:
            for host in item["hosts"]:
                conn.execute(
                    "INSERT INTO host_resource_usage_daily(run_id, stat_date, vcenter_id,"
                    " service_name, cluster_name, esxi_host, vm_count, allocated_cpu_cores,"
                    " allocated_memory_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct, mem_avg_pct,"
                    " sample_count, collection_status, raw_json, created_at)"
                    " VALUES(?,?,'VC1',?,?,?, 0, ?,?, ?,?,?,?, 12, 'SUCCESS', '{}', ?)",
                    (run_id, day.isoformat(), item.get("service") or "업무A", item["name"], host,
                     item["cores"], item["memory_gb"] * GB,
                     item["cpu"] + 10, item["cpu"], item["mem"] + 10, item["mem"], day.isoformat()),
                )
            for index in range(item["vms"]):
                name = f"{item['hosts'][0]}-vm{index:02d}"
                conn.execute(
                    "INSERT INTO vm_resource_usage_daily(run_id, stat_date, vcenter_snapshot_id,"
                    " asset_key, vcenter_id, service_name, cluster_name, esxi_host, vm_uuid,"
                    " vm_name, power_state, allocated_cpu_cores, allocated_memory_mb,"
                    " provisioned_disk_mb, used_disk_mb, cpu_max_pct, cpu_avg_pct, mem_max_pct,"
                    " mem_avg_pct, sample_count, inventory_status, collection_status, raw_json,"
                    " created_at)"
                    " VALUES(?,?,NULL,?, 'VC1',?,?,?,?,?, 'poweredOn', 4, ?, ?, ?, 40,20,60,30,"
                    " 12, 'CURRENT', 'SUCCESS', '{}', ?)",
                    (run_id, day.isoformat(), name, item.get("service") or "업무A", item["name"],
                     item["hosts"][0], name, name, 16 * GB, 100 * GB, 40 * GB, day.isoformat()),
                )
            if item.get("datastore"):
                conn.execute(
                    "INSERT INTO datastore_usage_daily(run_id, stat_date, vcenter_id, service_name,"
                    " cluster_name, datastore_name, datastore_type, accessible, capacity_mb,"
                    " free_mb, used_mb, provisioned_mb, host_count, vm_count, collection_status,"
                    " raw_json, created_at)"
                    " VALUES(?,?,'VC1','업무A',?,?, 'VMFS', 1, ?,?,?,?, ?, 0, 'SUCCESS', ?, ?)",
                    (run_id, day.isoformat(), None, item["datastore"],
                     10000 * GB, 6000 * GB, 4000 * GB, 5000 * GB, len(item["hosts"]),
                     json.dumps({"cluster_names": item["datastore_clusters"]}, ensure_ascii=False),
                     day.isoformat()),
                )
        conn.commit()
    return run_id


def fleet(*, with_new: bool = False) -> list[dict]:
    """같은 데이터스토어를 쓰는 묶음 둘. 장표의 병합 구조를 만든다."""
    rows = [
        {"name": "Windows 통합기 #1", "hosts": ["esxi-w01", "esxi-w02"], "cores": 64,
         "memory_gb": 448, "cpu": 14, "mem": 25, "vms": 12,
         "datastore": "DS_WIN", "datastore_clusters": ["Windows 통합기 #1", "Windows 통합기 #2"]},
        {"name": "Windows 통합기 #2", "hosts": ["esxi-w03"], "cores": 128,
         "memory_gb": 896, "cpu": 19, "mem": 16, "vms": 13},
        {"name": "Linux 통합기 #1", "hosts": ["esxi-l01"], "cores": 96,
         "memory_gb": 896, "cpu": 11, "mem": 33, "vms": 20,
         "datastore": "DS_LNX", "datastore_clusters": ["Linux 통합기 #1"]},
    ]
    if with_new:
        rows.append({"name": "신규 LINUX 통합 #1", "hosts": ["esxi-n01"], "cores": 64,
                     "memory_gb": 1536, "cpu": 14, "mem": 14, "vms": 13})
    return rows


def build(config, manager, base_day: date = BASE_DAY, **options):
    with manager.connect() as conn:
        return MonthlyReportService(config, AssetRepository(conn), **options).build(base_day)


def saved(config, workbook, name: str = "report.xlsx"):
    path = Path(config.root_dir) / name
    workbook.save(path)
    workbook.close()
    return load_workbook(path)


# ── 시트 구성 ──────────────────────────────────────────────────────────


def test_the_workbook_has_the_four_sheets_in_order(portal) -> None:
    """대시보드를 앞에 두되, 장표 순서는 받은 파일과 같아야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001"), asset("CM002", physical=True)])
    book = saved(config, build(config, manager))
    assert book.sheetnames == [
        "통합서버자원사용현황", "통합서버자원사용현황(상세)",
        "서버현황(전체)", "서버현황(물리)",
    ]


# ── 시트 1: 통합서버자원사용현황 ───────────────────────────────────────


def test_the_first_sheet_matches_the_form_layout(portal) -> None:
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, fleet())
    sheet = saved(config, build(config, manager, unit="CLUSTER"))["통합서버자원사용현황"]

    assert sheet["B2"].value == "서버"
    assert sheet["D2"].value == "’26.09", "당월 라벨이 양식 표기와 달라졌다"
    assert sheet["I2"].value == "’26.08", "전월 라벨"
    assert sheet["D3"].value == "CPU/MEM"
    assert sheet["E3"].value == "CPU" and sheet["E4"].value == "사용률"
    assert sheet["F3"].value == "MEM" and sheet["F4"].value == "사용률"
    assert sheet["G3"].value == "현재 대수"
    assert sheet["H3"].value == "디스크" and sheet["H4"].value == "사용률"
    assert sheet["K3"].value == "현재 대수"
    merged = {str(r) for r in sheet.merged_cells.ranges}
    assert "B2:C4" in merged and "D2:H2" in merged and "I2:L2" in merged


def test_the_first_sheet_puts_last_month_and_this_month_side_by_side(portal) -> None:
    """전월과 당월이 한 줄에 있어야 보고자료에 그대로 붙일 수 있다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, LAST_MONTH, fleet())
    seed_usage(manager, BASE_DAY, fleet())
    sheet = saved(config, build(config, manager, unit="CLUSTER"))["통합서버자원사용현황"]

    rows = {}
    for row in range(5, sheet.max_row + 1):
        name = sheet.cell(row, 3).value
        if name:
            rows[str(name)] = row
    line = rows["Windows 통합기 #1"]
    assert sheet.cell(line, 4).value == "128C / 896G", "실제 자원 표기"
    assert sheet.cell(line, 5).value == 14            # 당월 CPU 사용률
    assert sheet.cell(line, 6).value == 25            # 당월 MEM 사용률
    assert sheet.cell(line, 7).value == 12            # 당월 VM 대수
    assert sheet.cell(line, 9).value == 14            # 전월 CPU
    assert sheet.cell(line, 11).value == 12           # 전월 VM 대수


def test_a_new_integrator_is_marked_not_counted_as_a_drop(portal) -> None:
    """전월이 없는 통합기에 0 을 적으면 '줄었다' 로 읽힌다. 그렇게 적으면 안 된다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, LAST_MONTH, fleet())
    seed_usage(manager, BASE_DAY, fleet(with_new=True))
    sheet = saved(config, build(config, manager, unit="CLUSTER"))["통합서버자원사용현황"]

    line = next(row for row in range(5, sheet.max_row + 1)
                if sheet.cell(row, 3).value == "신규 LINUX 통합 #1")
    assert sheet.cell(line, 9).value == "신규 생성 통합기"
    assert sheet.cell(line, 11).value is None, "전월 대수를 0 으로 적으면 안 된다"
    # 당월 값은 정상으로 들어 있다.
    assert sheet.cell(line, 7).value == 13


def test_the_disk_column_is_merged_per_datastore_group(portal) -> None:
    """디스크 사용률은 통합기 하나가 아니라 같은 데이터스토어를 쓰는 묶음 단위다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, fleet())
    sheet = saved(config, build(config, manager, unit="CLUSTER"))["통합서버자원사용현황"]

    merged = {str(r) for r in sheet.merged_cells.ranges}
    # Windows #1·#2 가 DS_WIN 을 함께 쓰므로 디스크 칸이 두 줄로 묶인다.
    assert any(r.startswith("H") and ":" in r and r != "H3:H4" for r in merged), merged
    windows = next(row for row in range(5, sheet.max_row + 1)
                   if sheet.cell(row, 3).value == "Windows 통합기 #1")
    # 10000GB 중 4000GB 사용 = 40%
    assert sheet.cell(windows, 8).value == 40.0


def test_the_change_block_counts_then_names_the_vms(portal) -> None:
    """위는 숫자만, 아래 세부내용에 어느 VM 인지 이름이 나와야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, fleet())
    now = f"{BASE_DAY.isoformat()}T09:00:00"
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("RVTOOLS", now)
        repo.finish_collection_run(run, "SUCCESS", 1, now, ["VC1"], [])
        snapshot = repo.create_snapshot("RVTOOLS", BASE_DAY.isoformat(), now, run, "SUCCESS", 1, "h")
        repo.insert_rv_records(snapshot, [{
            "asset_key": "Linux 통합기 #1-vm99", "vm_uuid": "u99", "smbios_uuid": "s99",
            "vm_id": "mo99", "vcenter": "VC1", "vm_name": "Linux 통합기 #1-vm99",
            "dns_name": "x", "normalized_hostname": "x", "ip_addresses": [], "primary_ip": None,
            "cpus": 2, "memory_mb": 4096, "os_family": "Linux Redhat", "os_version": "8",
            "power_state": "poweredon", "datacenter": "DC1", "cluster": "Linux 통합기 #1",
            "cluster_name": "Linux 통합기 #1", "esxi_host": "esxi-l01",
            "template_flag": False, "srm_placeholder": False, "raw": {}, "record_hash": "h",
        }])
        repo.replace_change_events(snapshot, [{
            "source": "RVTOOLS", "asset_key": "Linux 통합기 #1-vm99", "event_type": "RV_NEW",
            "field_name": None, "old_value": None, "new_value": None,
            "previous_snapshot_id": None, "detected_at": now, "metadata": {},
        }])
        conn.commit()

    sheet = saved(config, build(config, manager, unit="CLUSTER"))["통합서버자원사용현황"]
    text = "\n".join(
        " ".join(str(c) for c in row if c is not None)
        for row in sheet.iter_rows(values_only=True)
    )
    assert "통합기별 서버 증감현황" in text
    assert "총 계" in text
    assert "세부내용" in text
    assert "Linux 통합기 #1-vm99" in text, "어느 VM 이 생겼는지 이름이 없다"
    assert "생성 : 1" in text


# ── 시트 2: 통합기별 VM 상세 ───────────────────────────────────────────


def test_the_detail_sheet_groups_vms_by_integrator_in_the_same_order(portal) -> None:
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, fleet())
    book = saved(config, build(config, manager, unit="CLUSTER"))
    first, detail = book["통합서버자원사용현황"], book["통합서버자원사용현황(상세)"]

    # 위치 칸은 병합돼 있어 첫 줄만 값이 있다. 통합기명 칸으로 읽고 계 줄에서 멈춘다.
    order = []
    for row in range(5, first.max_row + 1):
        if first.cell(row, 2).value == "계":
            break
        name = first.cell(row, 3).value
        if name:
            order.append(str(name))
    titles = [str(detail.cell(row, 2).value) for row in range(1, detail.max_row + 1)
              if detail.cell(row, 2).value and "가상서버 운영" in str(detail.cell(row, 2).value)]
    assert len(titles) == 3
    assert len(order) == 3
    for name, title in zip(order, titles):
        assert title.startswith(name), f"순서가 첫 시트와 다릅니다: {name} vs {title}"
    windows = next(t for t in titles if t.startswith("Windows 통합기 #1"))
    assert "128core" in windows and "896GB" in windows
    assert "12개 가상서버 운영" in windows


def test_the_detail_sheet_shows_cpu_and_memory_per_vm(portal) -> None:
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, fleet())
    sheet = saved(config, build(config, manager, unit="CLUSTER"))["통합서버자원사용현황(상세)"]

    header = next(row for row in range(1, sheet.max_row + 1)
                  if sheet.cell(row, 2).value == "구분 (VM)")
    assert sheet.cell(header, 3).value == "할당량"
    assert sheet.cell(header, 5).value == "개별서버 부하"
    labels = [sheet.cell(header + 1, column).value for column in range(3, 9)]
    assert labels == ["vCPU(개)", "메모리(GB)", "CPU\nMAX(%)", "CPU\nAVG(%)",
                      "메모리\nMAX(%)", "메모리\nAVG(%)"]
    first_vm = header + 2
    assert str(sheet.cell(first_vm, 2).value).endswith("vm00")
    assert sheet.cell(first_vm, 3).value == 4            # vCPU
    assert sheet.cell(first_vm, 4).value == 16           # 메모리 GB
    assert sheet.cell(first_vm, 5).value == 40.0         # CPU MAX
    assert sheet.cell(first_vm, 6).value == 20.0         # CPU AVG
    # 통합기별 소계가 있어야 할당 합계를 바로 읽을 수 있다.
    text = "\n".join(" ".join(str(c) for c in row if c is not None)
                     for row in sheet.iter_rows(values_only=True))
    assert "소계" in text


# ── 시트 3·4: 대시보드 ─────────────────────────────────────────────────


def test_the_dashboard_has_the_four_blocks_and_charts(portal) -> None:
    config, manager = portal
    for day in (TWO_MONTHS, LAST_MONTH, BASE_DAY):
        count = {TWO_MONTHS: 3, LAST_MONTH: 5, BASE_DAY: 8}[day]
        seed_itsm(manager, day, [
            asset(f"CM{index:03d}", physical=index % 3 == 0,
                  place="CMPLACE020" if index % 4 == 0 else "CMPLACE010",
                  os_family="WINDOWS" if index % 2 else "Linux Redhat")
            for index in range(1, count + 1)
        ])
    sheet = saved(config, build(config, manager))["서버현황(전체)"]
    text = "\n".join(" ".join(str(c) for c in row if c is not None)
                     for row in sheet.iter_rows(values_only=True))
    for block in ("① 전체 현황", "② 구성 비중", "③ 최근 3개월", "④ 한 달간"):
        assert block in text, f"{block} 가 없다"
    # 원형 둘 + 추이 하나.
    assert len(sheet._charts) == 3, f"그래프가 {len(sheet._charts)}개입니다"
    assert "변동 대상 목록" in text
    assert "host-CM006" in text or "host-CM007" in text, "늘어난 서버 이름이 없다"


def test_the_dashboard_puts_the_change_inside_the_cell(portal) -> None:
    """양식 표기: 1145(+9). 셀 안에 있어야 그대로 붙여 쓸 수 있다."""
    config, manager = portal
    seed_itsm(manager, LAST_MONTH, [asset("CM001")])
    seed_itsm(manager, BASE_DAY, [asset("CM001"), asset("CM002"), asset("CM003")])
    sheet = saved(config, build(config, manager))["서버현황(전체)"]

    total = next(row for row in range(1, sheet.max_row + 1) if sheet.cell(row, 2).value == "계")
    values = [sheet.cell(total, column).value for column in range(3, 10)]
    assert any(isinstance(v, str) and "(+2)" in v for v in values), values


def test_the_physical_sheet_separates_physical_servers(portal) -> None:
    config, manager = portal
    seed_itsm(manager, LAST_MONTH, [asset("CM001", physical=True)])
    seed_itsm(manager, BASE_DAY, [
        asset("CM001", physical=True), asset("CM002", physical=True), asset("CM003"),
    ])
    book = saved(config, build(config, manager))
    physical, whole = book["서버현황(물리)"], book["서버현황(전체)"]

    def total_of(sheet):
        row = next(r for r in range(1, sheet.max_row + 1) if sheet.cell(r, 2).value == "계")
        return [sheet.cell(row, column).value for column in range(3, 10)]

    assert any(isinstance(v, str) and "2(+1)" in v for v in total_of(physical)), total_of(physical)
    assert any(isinstance(v, str) and "3(+2)" in v for v in total_of(whole)), total_of(whole)
    text = "\n".join(" ".join(str(c) for c in row if c is not None)
                     for row in physical.iter_rows(values_only=True))
    assert "물리서버" in text
    # 물리 시트의 변동 목록에는 논리서버가 들어가면 안 된다.
    assert "host-CM003" not in text, "논리서버가 물리 시트에 섞였다"


# ── 자료가 없을 때 ─────────────────────────────────────────────────────


def test_without_any_snapshot_it_still_produces_a_file(portal) -> None:
    """수집 전에 눌러도 오류 대신 안내가 적힌 파일이 나와야 한다."""
    config, manager = portal
    book = saved(config, build(config, manager))
    first = book["통합서버자원사용현황"]
    text = "\n".join(" ".join(str(c) for c in row if c is not None)
                     for row in first.iter_rows(values_only=True))
    assert "07시 자동배치" in text
    dashboard = book["서버현황(전체)"]
    assert "ITSM 스냅샷이 없습니다" in "\n".join(
        " ".join(str(c) for c in row if c is not None)
        for row in dashboard.iter_rows(values_only=True)
    )


def test_unchanged_cells_stay_numbers_so_excel_can_sum_them(portal) -> None:
    """바뀐 칸만 글자가 된다. 전부 글자로 적으면 합계를 다시 낼 수 없다."""
    config, manager = portal
    seed_itsm(manager, LAST_MONTH, [asset("CM001"), asset("CM002", os_family="WINDOWS")])
    seed_itsm(manager, BASE_DAY, [
        asset("CM001"), asset("CM002", os_family="WINDOWS"), asset("CM003"),
    ])
    sheet = saved(config, build(config, manager))["서버현황(전체)"]
    total = next(row for row in range(1, sheet.max_row + 1) if sheet.cell(row, 2).value == "계")
    values = [sheet.cell(total, column).value for column in range(3, 10)]
    assert any(isinstance(v, int) for v in values), f"안 바뀐 칸이 글자입니다: {values}"
    assert any(isinstance(v, str) and "(+1)" in v for v in values), values


def test_the_trend_columns_follow_the_table_order(portal) -> None:
    """달마다 열이 뒤바뀌면 추이 그래프를 읽을 수 없다."""
    config, manager = portal
    for day, count in ((TWO_MONTHS, 4), (LAST_MONTH, 6), (BASE_DAY, 9)):
        seed_itsm(manager, day, [
            asset(f"CM{index:03d}",
                  os_family=["Linux Redhat", "WINDOWS", "AIX", "HP-UX"][index % 4])
            for index in range(1, count + 1)
        ])
    sheet = saved(config, build(config, manager))["서버현황(전체)"]
    head = next(row for row in range(1, sheet.max_row + 1) if sheet.cell(row, 2).value == "기준월")
    columns = []
    for column in range(3, sheet.max_column + 1):
        value = sheet.cell(head, column).value
        if value in (None, "합계"):
            break
        columns.append(str(value))
    order = ["HP", "IBM", "Linux", "Windows", "기타"]
    assert columns == [name for name in order if name in columns], columns
    # 오래된 달이 왼쪽이어야 '증가추이' 로 읽힌다.
    labels = [sheet.cell(head + offset, 2).value for offset in (1, 2, 3)]
    assert labels == ["’26.07", "’26.08", "’26.09"], labels


# ── 화면에서 받을 수 있어야 한다 ───────────────────────────────────────


@pytest.fixture()
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\n", encoding="utf-8"
    )
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager

    reset_database_manager()
    yield create_app()
    reset_database_manager()


def test_the_screen_can_download_the_report(web) -> None:
    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager

    config = load_config()
    manager = create_manager(config)
    seed_itsm(manager, BASE_DAY, [asset("CM001"), asset("CM002", physical=True)])

    client = web.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    response = client.get(f"/api/asset-sync/monthly-report?month={BASE_DAY.strftime('%Y-%m')}")
    assert response.status_code == 200, response.get_data()[:400]
    assert response.data[:2] == b"PK", "엑셀 파일이 아닙니다"
    assert "202609" in response.headers.get("Content-Disposition", "")


def test_the_button_is_wired_on_the_screen() -> None:
    page = (Path(__file__).resolve().parents[1] / "templates" / "pages" / "monthly_check.html")
    script = (Path(__file__).resolve().parents[1] / "templates" / "partials" / "js"
              / "monthly_check.html")
    assert "exportMonthlyReport()" in page.read_text(encoding="utf-8")
    body = script.read_text(encoding="utf-8")
    assert "function exportMonthlyReport(" in body
    assert "/api/asset-sync/monthly-report" in body


# ── 줄 단위와 정렬 ─────────────────────────────────────────────────────


def sortable() -> list[dict]:
    """자연 정렬을 시험할 묶음. #2 가 #10 보다 앞에 와야 한다."""
    rows = []
    for index in (1, 2, 10, 11):
        rows.append({
            "name": "Linux 클러스터", "hosts": [f"esxi-l{index:02d}"], "cores": 48,
            "memory_gb": 448, "cpu": 10 + index, "mem": 30, "vms": index,
            "service": "업무A",
        })
    for index in (1, 2, 10):
        rows.append({
            "name": "Windows 클러스터", "hosts": [f"esxi-w{index:02d}"], "cores": 64,
            "memory_gb": 448, "cpu": 20 + index, "mem": 20, "vms": index,
            "service": "업무B",
        })
    return rows


def first_rows(sheet, *, columns: int = 4) -> list[tuple]:
    """자료 줄만. 계 줄에서 멈춘다."""
    out = []
    for row in range(5, sheet.max_row + 1):
        if sheet.cell(row, 2).value == "계":
            break
        if any(sheet.cell(row, column).value is not None for column in range(2, columns + 2)):
            out.append(tuple(sheet.cell(row, column).value for column in range(2, columns + 2)))
    return out


def test_the_default_unit_is_one_row_per_integrator(portal) -> None:
    """통합기(ESXi) 한 대가 한 줄이고, 클러스터는 묶음 칸이 된다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, sortable())
    sheet = saved(config, build(config, manager))["통합서버자원사용현황"]

    # B=위치, C=클러스터, D=통합기, E=실제자원
    assert sheet["B2"].value == "서버"
    assert sheet["E3"].value == "CPU/MEM", "클러스터 칸이 하나 늘어난다"
    names = [row[2] for row in first_rows(sheet)]
    assert names == ["esxi-l01", "esxi-l02", "esxi-l10", "esxi-l11",
                     "esxi-w01", "esxi-w02", "esxi-w10"], names
    clusters = [row[1] for row in first_rows(sheet)]
    assert clusters[0] == "Linux 클러스터"
    # ESXi 7 대 전부가 줄이 된다. 클러스터는 둘뿐이다.
    assert len(names) == 7


def test_names_sort_naturally_not_as_text(portal) -> None:
    """'#2' 가 '#10' 보다 앞에 와야 한다. 글자로 견주면 거꾸로 된다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, [
        {"name": "Linux 클러스터", "hosts": [f"통합기 #{index}"], "cores": 48,
         "memory_gb": 448, "cpu": 10, "mem": 30, "vms": 1}
        for index in (1, 2, 3, 10, 11, 20)
    ])
    sheet = saved(config, build(config, manager))["통합서버자원사용현황"]
    names = [row[2] for row in first_rows(sheet)]
    assert names == ["통합기 #1", "통합기 #2", "통합기 #3",
                     "통합기 #10", "통합기 #11", "통합기 #20"], names


def test_hosts_named_by_ip_sort_numerically(portal) -> None:
    """IP 로 이름을 붙인 곳도 10.0.0.9 < 10.0.0.10 이 되어야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, [
        {"name": "DMZ 클러스터", "hosts": [f"10.0.0.{index}"], "cores": 32,
         "memory_gb": 256, "cpu": 10, "mem": 30, "vms": 1}
        for index in (2, 9, 10, 100)
    ])
    sheet = saved(config, build(config, manager))["통합서버자원사용현황"]
    assert [row[2] for row in first_rows(sheet)] == ["10.0.0.2", "10.0.0.9",
                                                      "10.0.0.10", "10.0.0.100"]


def test_the_sort_basis_can_be_chosen(portal) -> None:
    """현장마다 보는 순서가 다르다. 기준을 고를 수 있어야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, sortable())

    # VM 대수 많은 순.
    sheet = saved(config, build(config, manager, sort="-vm_count"), "by_vm.xlsx")["통합서버자원사용현황"]
    # B=위치 C=클러스터 D=통합기 E=실제자원 F=CPU G=MEM H=현재 대수
    counts = [row[6] for row in first_rows(sheet, columns=7)]
    assert counts == sorted(counts, reverse=True), counts

    # 업무명 → 호스트명.
    sheet = saved(config, build(config, manager, sort=["service", "host"]),
                  "by_service.xlsx")["통합서버자원사용현황"]
    names = [row[2] for row in first_rows(sheet)]
    assert names[:4] == ["esxi-l01", "esxi-l02", "esxi-l10", "esxi-l11"], names
    assert names[4:] == ["esxi-w01", "esxi-w02", "esxi-w10"], names


def test_an_unknown_sort_basis_is_reported_not_ignored(portal) -> None:
    """조용히 버리면 '왜 정렬이 안 되나' 를 사람이 한참 뒤진다."""
    config, manager = portal
    with manager.connect() as conn:
        with pytest.raises(ValueError, match="정렬 기준"):
            MonthlyReportService(config, AssetRepository(conn), sort="호스트명")
        with pytest.raises(ValueError, match="줄 단위"):
            MonthlyReportService(config, AssetRepository(conn), unit="VM")


def test_the_config_sets_the_default(portal, tmp_path: Path) -> None:
    """파일로도 정할 수 있어야 한다. 매번 화면에서 고르게 하면 안 된다."""
    config, manager = portal
    config.report = {"unit": "CLUSTER", "sort": ["-vm_count"]}
    with manager.connect() as conn:
        service = MonthlyReportService(config, AssetRepository(conn))
    assert service.unit == "CLUSTER"
    assert service.sort == ("-vm_count",)
    assert "클러스터 단위" in service.describe()["unit_label"]
    assert "내림차순" in service.describe()["sort_label"]


def test_the_detail_sheet_follows_the_unit(portal) -> None:
    """첫 시트가 통합기 단위면 VM 도 통합기별로 묶여야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, sortable())
    book = saved(config, build(config, manager))
    first, detail = book["통합서버자원사용현황"], book["통합서버자원사용현황(상세)"]

    order = [row[2] for row in first_rows(first)]
    titles = [str(detail.cell(row, 2).value) for row in range(1, detail.max_row + 1)
              if detail.cell(row, 2).value and "가상서버 운영" in str(detail.cell(row, 2).value)]
    assert len(titles) == len(order)
    for name, title in zip(order, titles):
        assert title.startswith(name), f"{name} vs {title}"


def test_the_sheet_says_how_it_was_split_and_sorted(portal) -> None:
    """나중에 "왜 이 순서지?" 를 파일만 보고 알 수 있어야 한다."""
    config, manager = portal
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, sortable())
    sheet = saved(config, build(config, manager, sort=["cluster", "host"]))["통합서버자원사용현황"]
    note = " ".join(str(sheet.cell(1, column).value or "") for column in range(2, 8))
    assert "통합기(ESXi) 단위" in note
    assert "클러스터 이름" in note and "ESXi 호스트명" in note


def test_the_screen_can_choose_the_unit_and_sort(web) -> None:
    """현장에서 설정 파일을 못 고칠 때도 화면에서 고를 수 있어야 한다."""
    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager

    config = load_config()
    manager = create_manager(config)
    seed_itsm(manager, BASE_DAY, [asset("CM001")])
    seed_usage(manager, BASE_DAY, sortable())

    client = web.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": "admin", "role": "admin", "name": "admin"}
    month = BASE_DAY.strftime("%Y-%m")

    options = client.get("/api/asset-sync/monthly-report/options").get_json()
    assert [item["id"] for item in options["units"]] == ["ESXI", "CLUSTER"]
    assert any(item["id"] == "host" for item in options["sort_fields"])
    assert options["current"]["unit"] == "ESXI"

    for unit, sort in (("ESXI", "cluster,host"), ("CLUSTER", "-vm_count")):
        response = client.get(
            f"/api/asset-sync/monthly-report?month={month}&unit={unit}&sort={sort}"
        )
        assert response.status_code == 200, response.get_data()[:300]
        assert response.data[:2] == b"PK"

    # 모르는 기준은 400 으로 알려준다. 조용히 기본값으로 떨어지면 안 된다.
    bad = client.get(f"/api/asset-sync/monthly-report?month={month}&sort=호스트명")
    assert bad.status_code == 400
    assert "정렬 기준" in bad.get_json()["error"]
    bad = client.get(f"/api/asset-sync/monthly-report?month={month}&unit=VM")
    assert bad.status_code == 400


def test_the_screen_shows_what_the_default_is() -> None:
    page = (Path(__file__).resolve().parents[1] / "templates" / "pages" / "monthly_check.html")
    script = (Path(__file__).resolve().parents[1] / "templates" / "partials" / "js"
              / "monthly_check.html")
    body = page.read_text(encoding="utf-8")
    code = script.read_text(encoding="utf-8")
    for element_id in ("monthly-report-unit", "monthly-report-sort"):
        assert f'id="{element_id}"' in body, f"{element_id} 가 화면에 없다"
        assert f"'{element_id}'" in code, f"{element_id} 를 스크립트가 안 찾는다"
    assert "/api/asset-sync/monthly-report/options" in code
    assert "loadMonthlyReportOptions" in code
