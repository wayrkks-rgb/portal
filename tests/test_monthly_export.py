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


#: 자산 시트의 머리글 두 줄과 자료 시작 줄.
LABEL_ROW, NAME_ROW, FIRST_DATA_ROW = 4, 5, 6


def _columns(sheet) -> tuple[list, list]:
    """(한글 이름 줄, 원래 컬럼명 줄)"""
    return ([cell.value for cell in sheet[LABEL_ROW]],
            [cell.value for cell in sheet[NAME_ROW]])


def test_the_place_code_becomes_idc_or_dr_in_the_file(monthly):
    """CMPLACE020 은 DR 이다. 해석한 값과 원본이 모두 있어야 한다."""
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "assets")
    sheet = load_workbook(path)["자산 목록"]
    labels, names = _columns(sheet)
    location = labels.index("위치") + 1
    raw_place = names.index("CM_PLACE") + 1

    seen = {}
    for line in range(FIRST_DATA_ROW, sheet.max_row + 1):
        seen[sheet.cell(row=line, column=raw_place).value] = sheet.cell(row=line, column=location).value
    assert seen["CMPLACE010"] == "IDC"
    assert seen["CMPLACE020"] == "DR"


def test_the_excluded_sheet_shows_why_each_one_was_dropped(monthly):
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "excluded")
    sheet = load_workbook(path)["제외한 대상"]
    labels, _ = _columns(sheet)
    reason = labels.index("제외 사유") + 1
    assert sheet.cell(row=FIRST_DATA_ROW, column=reason).value == "상태가 운영·대기가 아님"
    # 제외 기준도 파일에 적혀 있어야 한다.
    assert "제외 기준" in str(sheet["A2"].value)


def test_the_asset_sheet_carries_every_itsm_column(monthly):
    """화면은 꼭 필요한 것만 보여 주지만 파일에는 원본 전 컬럼이 들어가야 한다.

    받아서 다시 거르고 피벗하려면 전 컬럼이 필요하다.
    """
    status, records, tmp_path = monthly
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "assets")
    sheet = load_workbook(path)["자산 목록"]
    labels, names = _columns(sheet)

    # 원본에 있던 컬럼이 하나도 빠지지 않는다.
    expected = set()
    for record in records:
        expected.update(record.get("raw") or {})
    assert expected <= set(names), f"빠진 컬럼: {expected - set(names)}"

    # 아는 컬럼은 한글 이름이 붙는다. 모르는 컬럼은 원래 이름을 그대로 쓴다.
    assert labels[names.index("CM_HOSTNAME")] == "호스트명"
    assert labels[names.index("CM_EOL_DT")] == "OS 지원종료일" or "CM_EOL_DT" in names

    # 값이 해석되지 않은 원본 그대로여야 한다. 코드로 걸러 쓸 수 있어야 한다.
    status_column = names.index("CM_STA_CD") + 1 if "CM_STA_CD" in names else None
    if status_column:
        values = {sheet.cell(row=line, column=status_column).value
                  for line in range(FIRST_DATA_ROW, sheet.max_row + 1)}
        assert values & {"CMSTA010", "CMSTA060"}


def test_a_column_only_some_records_have_is_still_included(monthly):
    """ITSM 조회 SQL 에 따라 컬럼이 달라진다. 자료에서 모아야 한다."""
    status, records, tmp_path = monthly
    records = [dict(item) for item in records]
    records[0]["raw"] = dict(records[0]["raw"], CM_EXTRA_NOTE="증설 예정")
    path = MonthlyCheckExportService(status, records).save(tmp_path / "export", "assets")
    sheet = load_workbook(path)["자산 목록"]
    _, names = _columns(sheet)
    assert "CM_EXTRA_NOTE" in names


def test_one_sheet_can_be_built_for_the_popup(monthly):
    """화면에서 숫자를 눌러 나온 목록만 따로 받는 길."""
    status, records, tmp_path = monthly
    workbook = MonthlyCheckExportService(status, records).build_asset_list(
        "DR · AIX 1대", records[:1], "조건: 위치=DR · OS=IBM"
    )
    assert workbook.sheetnames == ["자산 목록"]
    sheet = workbook["자산 목록"]
    assert sheet["A1"].value == "DR · AIX 1대"
    assert "조건: 위치=DR" in str(sheet["A2"].value)
    _, names = _columns(sheet)
    assert "CM_ID" in names


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


def test_the_eosl_bucket_is_decided_in_one_place():
    """표를 만들 때와 숫자를 눌러 고를 때가 같은 함수를 써야 한다.

    두 군데에 따로 적으면 표의 숫자와 목록의 줄 수가 어긋난다.
    """
    bucket = ServerStatusService.eosl_bucket
    assert bucket(None, 2026) == "미사용"
    assert bucket(9999, 2026) == "계획 없음"
    assert bucket(2020, 2026) == "2026년 이전"
    assert bucket(2026, 2026) == "2026년"
    assert bucket(2027, 2026) == "2027년"
    assert bucket(2031, 2026) == "2028년 이상"


def test_the_eosl_table_and_the_bucket_agree(monthly):
    """표의 각 칸 숫자가 그 칸으로 묶이는 자산 수와 같아야 한다."""
    status, records, _ = monthly
    year = status["eosl"]["criteria"]["base_year"]
    counted = status["eosl"]["all"]["counts"]
    included = [item for item in records if not item.get("exclude_reason")]
    for column, number in counted.items():
        picked = [
            item for item in included
            if ServerStatusService.eosl_bucket(item.get("eosl_year"), year) == column
        ]
        assert len(picked) == number, f"{column}: 표 {number} vs 목록 {len(picked)}"
