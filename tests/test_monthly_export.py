"""월간 점검 장표 엑셀. 화면과 파일의 숫자가 같아야 한다.

파일이 따로 집계하면 화면과 숫자가 달라질 수 있고, 그러면 어느 쪽을 믿어야
하는지 알 수 없다. 그래서 화면에 쓴 결과를 그대로 받아 옮긴다. 그 성질을 여기서
지킨다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.monthly_export_service import (
    MONTHLY_SECTIONS,
    MonthlyCheckExportService,
)
from asset_sync.services.server_status_service import ServerStatusService


def asset(cm_id, *, place="CMPLACE010", physical=False, os_family="Linux Redhat",
          status="CMSTA010", eosl="2030-12-31"):
    return {
        "cm_id": cm_id, "normalized_hostname": f"host-{cm_id}", "primary_ip": "10.0.0.5",
        "os_family": os_family, "status_code": status,
        "server_category_code": "CMSVRCATCD010" if physical else "CMSVRCATCD020",
        "raw": {"CM_ID": cm_id, "CM_NAME": f"업무-{cm_id}", "CM_HOSTNAME": f"host-{cm_id}",
                "CM_IP": "10.0.0.5", "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6",
                "CM_EOL_DT": eosl, "CM_PLACE": place},
    }


@pytest.fixture()
def monthly(tmp_path: Path):
    """두 달치 스냅샷을 만들고 월간 점검 결과를 돌려준다."""
    config = AppConfig(root_dir=tmp_path, sqlite_path=Path("data/export.db"))
    manager = create_manager(config)
    manager.initialize()

    rows = [
        asset("CM0001"),
        asset("CM0002", place="CMPLACE020", physical=True, os_family="AIX"),
        asset("CM0003", place="CMPLACE020", os_family="WINDOWS", eosl="99991231"),
        asset("CM0004", status="CMSTA060"),          # 폐기 -> 제외
    ]
    now = datetime.now()
    with manager.connect() as conn:
        repo = AssetRepository(conn)
        run = repo.start_collection_run("ITSM", now.isoformat())
        repo.finish_collection_run(run, "SUCCESS", len(rows), now.isoformat(), ["ALL"])
        snapshot_id = repo.create_snapshot(
            "ITSM", now.date().isoformat(), now.isoformat(), run, "SUCCESS", len(rows), "h")
        conn.executemany(
            "INSERT INTO itsm_asset_snapshot(snapshot_id,cm_id,normalized_hostname,primary_ip,"
            "ip_json,cpu_cores,memory_mb,os_family,os_version,status_code,server_category_code,"
            "environment_code,eos_value,record_hash,raw_json) VALUES(?,?,?,?,'[]',?,?,?,?,?,?,?,?,?,?)",
            [(snapshot_id, r["cm_id"], r["normalized_hostname"], r["primary_ip"], 4, 8192,
              r["os_family"], "8.6", r["status_code"], r["server_category_code"],
              "CMOWNCATCD0010", r["raw"]["CM_EOL_DT"], "h",
              json.dumps(r["raw"], ensure_ascii=False)) for r in rows],
        )
        conn.commit()

    with manager.connect() as conn:
        service = ServerStatusService(config, AssetRepository(conn))
        status = service.status(snapshot_id)
        status["eosl"] = service.eosl(snapshot_id)
        status["as_of"] = now.date().isoformat()
        records = service.records(snapshot_id)
    return status, records, tmp_path


def test_every_section_can_be_taken_on_its_own(monthly):
    """월간 보고에 붙일 때 필요한 장표만 뽑는 일이 많다."""
    status, records, tmp_path = monthly
    service = MonthlyCheckExportService(status, records)
    for section, label in MONTHLY_SECTIONS.items():
        path = service.save(tmp_path / "export", section)
        assert path.exists(), section
        workbook = load_workbook(path)
        assert workbook.sheetnames == [label[:31]], section
        workbook.close()
        assert label in path.name


def test_all_puts_every_section_in_one_file(monthly):
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "all")
    workbook = load_workbook(path)
    assert workbook.sheetnames == [name[:31] for name in MONTHLY_SECTIONS.values()]
    workbook.close()


def test_an_unknown_section_is_refused_with_the_list(monthly):
    status, records, tmp_path = monthly
    with pytest.raises(ValueError) as excinfo:
        MonthlyCheckExportService(status, records).build("없는항목")
    assert "server_all" in str(excinfo.value)


def test_the_file_says_the_same_numbers_as_the_screen(monthly):
    """집계를 새로 하지 않는다. 화면 결과를 그대로 옮긴다."""
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "server_all")
    sheet = load_workbook(path)["서버현황(전체)"]

    # 머리글은 4행, 자료는 5~7행(IDC·DR·계)
    columns = [cell.value for cell in sheet[4]]
    total_column = columns.index("소계") + 1
    rows = {sheet.cell(row=line, column=1).value: line for line in (5, 6, 7)}

    table = status["all"]["table"]["rows"]
    for name in ("IDC", "DR", "계"):
        assert sheet.cell(row=rows[name], column=total_column).value == table[name]["소계"], name
    # 폐기 자산은 화면에서도 파일에서도 빠진다.
    assert sheet.cell(row=rows["계"], column=total_column).value == 3


def test_the_place_code_becomes_idc_or_dr_in_the_file(monthly):
    """CMPLACE020 은 DR 이다. 코드가 그대로 적히면 안 된다."""
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "assets")
    sheet = load_workbook(path)["자산 목록"]
    header = [cell.value for cell in sheet[3]]
    location = header.index("위치") + 1
    raw_place = header.index("위치 원본값") + 1

    seen = {}
    for line in range(4, sheet.max_row + 1):
        seen[sheet.cell(row=line, column=raw_place).value] = sheet.cell(row=line, column=location).value
    assert seen["CMPLACE010"] == "IDC"
    assert seen["CMPLACE020"] == "DR"


def test_the_excluded_sheet_shows_why_each_one_was_dropped(monthly):
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "excluded")
    sheet = load_workbook(path)["제외한 대상"]
    header = [cell.value for cell in sheet[3]]
    reason = header.index("제외 사유") + 1
    assert sheet.cell(row=4, column=reason).value == "상태가 운영·대기가 아님"


def test_the_eosl_sheet_carries_the_diagnosis(monthly):
    """표만 보면 실제 데이터인지 컬럼을 잘못 짚은 것인지 알 수 없다."""
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "eosl")
    sheet = load_workbook(path)["EOSL 현황"]
    text = "\n".join(
        str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None
    )
    assert "EOSL 값 출처" in text
    assert "CM_EOL_DT" in text
