"""월간 점검 장표를 엑셀로 낸다.

화면에 보이는 것을 그대로 옮긴다. 화면과 파일의 숫자가 다르면 어느 쪽을 믿어야
하는지 알 수 없으므로, 집계는 새로 하지 않고 ``ServerStatusService`` 가 이미
만든 결과를 받아 쓴다.

항목마다 따로 받을 수 있게 만든다. 월간 보고에 붙일 때 필요한 장표만 뽑는 일이
많고, 전부 한 파일로 주면 쓰는 사람이 시트를 지워야 한다.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

#: 뽑을 수 있는 항목. 키가 곧 요청값이다.
SECTIONS: dict[str, str] = {
    "server_all": "서버현황(전체)",
    "server_physical": "서버현황(물리)",
    "movements": "자산 변동 내역",
    "eosl": "EOSL 현황",
    "excluded": "제외한 대상",
    "assets": "자산 목록",
}
ALL = "all"

#: 바깥에서 쓰는 이름.
MONTHLY_SECTIONS = SECTIONS

_HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
_SUB_FILL = PatternFill("solid", fgColor="DCE6F1")
_THIN = Side(style="thin", color="B0B7C3")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _style_header(sheet: Any, row: int) -> None:
    for cell in sheet[row]:
        if cell.value is None:
            continue
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = _HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _BORDER


def _fit(sheet: Any, minimum: int = 10, maximum: int = 42) -> None:
    for column in sheet.columns:
        width = max((len(str(cell.value or "")) for cell in column), default=0)
        letter = get_column_letter(column[0].column)
        sheet.column_dimensions[letter].width = max(min(width + 2, maximum), minimum)


class MonthlyCheckExportService:
    """월간 점검 결과를 워크북으로 만든다."""

    def __init__(self, status: dict[str, Any], records: list[dict[str, Any]] | None = None) -> None:
        self.status = status or {}
        self.records = records or []

    # ── 장표 ────────────────────────────────────────────────────────────
    def _write_status_table(self, sheet: Any, title: str, block: dict[str, Any]) -> None:
        """양식의 두 줄 머리글 표. IDC·DR·계 행에 OS 묶음 열이다."""
        table = block.get("table") or {}
        delta = block.get("delta") or {}
        columns: list[str] = list(table.get("columns") or [])
        rows = table.get("rows") or {}

        sheet["A1"] = title
        sheet["A1"].font = Font(bold=True, size=13)
        sheet["A2"] = f"기준일 {self.status.get('as_of') or '-'}"
        sheet["A2"].font = Font(size=10, color="666666")
        if self.status.get("previous_as_of"):
            sheet["B2"] = f"전월 {self.status['previous_as_of']} 대비 증감"
            sheet["B2"].font = Font(size=10, color="666666")

        head = 4
        sheet.cell(row=head, column=1, value="구분")
        for index, column in enumerate(columns, start=2):
            sheet.cell(row=head, column=index, value=column)
        # 증감은 숫자 옆이 아니라 뒤쪽 묶음에 따로 둔다. 셀 하나에 두 값을 넣으면
        # 엑셀에서 더할 수 없다.
        offset = len(columns) + 2
        if delta:
            sheet.cell(row=head, column=offset, value="전월 대비 증감")
            for index, column in enumerate(columns, start=offset + 1):
                sheet.cell(row=head, column=index, value=column)
        _style_header(sheet, head)

        for line, name in enumerate(("IDC", "DR", "계"), start=head + 1):
            values = rows.get(name) or {}
            cell = sheet.cell(row=line, column=1, value=name)
            cell.font = Font(bold=True)
            cell.border = _BORDER
            for index, column in enumerate(columns, start=2):
                target = sheet.cell(row=line, column=index, value=int(values.get(column) or 0))
                target.border = _BORDER
                if name == "계" or column == "소계":
                    target.font = Font(bold=True)
            if delta:
                changes = delta.get(name) or {}
                for index, column in enumerate(columns, start=offset + 1):
                    number = int(changes.get(column) or 0)
                    target = sheet.cell(row=line, column=index, value=number)
                    target.border = _BORDER
                    if number:
                        target.font = Font(color="1C7A4F" if number > 0 else "B23A34")
        _fit(sheet)

    def _write_movements(self, sheet: Any) -> None:
        """양식의 '자산 현황 변동 내역'. 위치 → 물리/논리 → OS 순으로 센다."""
        sheet["A1"] = "자산 현황 변동 내역"
        sheet["A1"].font = Font(bold=True, size=13)
        movements = self.status.get("movements")
        if not movements:
            sheet["A3"] = "비교할 전월 스냅샷이 없습니다."
            _fit(sheet)
            return

        sheet.append([])
        sheet.append(["구분", "위치", "물리/논리", "OS", "대수"])
        _style_header(sheet, sheet.max_row)
        for key, label in (("created", "신규 생성"), ("removed", "삭제")):
            block = movements.get(key) or {}
            for area, values in (block.get("locations") or {}).items():
                for kind, item in (values.get("kinds") or {}).items():
                    for os_name, count in (item.get("os") or {}).items():
                        if count:
                            sheet.append([label, area, kind, os_name, int(count)])
            sheet.append([label, "합계", "", "", int(block.get("total") or 0)])
            sheet.cell(row=sheet.max_row, column=5).font = Font(bold=True)
        _fit(sheet)

    def _write_eosl(self, sheet: Any) -> None:
        eosl = self.status.get("eosl") or {}
        sheet["A1"] = "EOSL 현황"
        sheet["A1"].font = Font(bold=True, size=13)
        criteria = eosl.get("criteria") or {}
        sheet["A2"] = (
            f"{criteria.get('eosl_field') or '-'} 기준 · {criteria.get('base_year') or '-'}년 기준"
            f" · {criteria.get('no_plan_year') or 9999} = 계획 없음"
        )
        sheet["A2"].font = Font(size=10, color="666666")

        columns: list[str] = list(eosl.get("columns") or [])
        head = 4
        sheet.cell(row=head, column=1, value="구분")
        sheet.cell(row=head, column=2, value="전체 수량")
        for index, column in enumerate(columns, start=3):
            sheet.cell(row=head, column=index, value=column)
        _style_header(sheet, head)

        line = head
        for key in ("physical", "all"):
            row = eosl.get(key) or {}
            if not row:
                continue
            line += 1
            cell = sheet.cell(row=line, column=1, value=row.get("label") or key)
            cell.font = Font(bold=True)
            cell.border = _BORDER
            total = sheet.cell(row=line, column=2, value=int(row.get("total") or 0))
            total.font = Font(bold=True)
            total.border = _BORDER
            counts = row.get("counts") or {}
            for index, column in enumerate(columns, start=3):
                target = sheet.cell(row=line, column=index, value=int(counts.get(column) or 0))
                target.border = _BORDER

        # 값을 어디서 읽었는지 같이 적는다. 표만 보면 실제 데이터인지 컬럼을
        # 잘못 짚은 것인지 알 수 없다.
        diagnosis = eosl.get("diagnosis") or {}
        if diagnosis.get("by_field"):
            line += 2
            sheet.cell(row=line, column=1, value="EOSL 값 출처").font = Font(bold=True)
            for name, count in (diagnosis.get("by_field") or {}).items():
                line += 1
                sheet.cell(row=line, column=1, value=name)
                sheet.cell(row=line, column=2, value=int(count))
                samples = (diagnosis.get("samples") or {}).get(name) or []
                if samples:
                    sheet.cell(row=line, column=3, value="예: " + ", ".join(str(s) for s in samples))
        _fit(sheet)

    _ASSET_COLUMNS: tuple[tuple[str, str], ...] = (
        ("자산번호", "cm_id"), ("업무명", "service_name"), ("호스트명", "hostname"),
        ("IP", "primary_ip"), ("위치", "location"), ("위치 원본값", "place"),
        ("물리/논리", "_kind"), ("OS", "os_family"), ("OS 묶음", "os_group"),
        ("OS버전", "os_version"), ("CPU Core", "cpu_cores"), ("Memory MB", "memory_mb"),
        ("EOSL 값", "eosl_value"), ("EOSL 연도", "eosl_year"), ("EOSL 컬럼", "eosl_field"),
        ("상태코드", "status_code"), ("자산 여부", "_included"),
        ("제외 사유", "exclude_label"), ("수동 지정", "_manual"), ("수동 사유", "manual_note"),
    )

    def _write_assets(self, sheet: Any, title: str, items: Iterable[dict[str, Any]]) -> None:
        sheet["A1"] = title
        sheet["A1"].font = Font(bold=True, size=13)
        head = 3
        for index, (label, _) in enumerate(self._ASSET_COLUMNS, start=1):
            sheet.cell(row=head, column=index, value=label)
        _style_header(sheet, head)

        line = head
        for item in items:
            line += 1
            filled = dict(item)
            filled["_kind"] = "물리" if item.get("physical") else "논리"
            filled["_included"] = "제외" if item.get("exclude_reason") else "자산"
            filled["_manual"] = "예" if item.get("manual") else ""
            for index, (_, key) in enumerate(self._ASSET_COLUMNS, start=1):
                value = filled.get(key)
                sheet.cell(row=line, column=index, value="" if value is None else value)
        sheet.freeze_panes = f"A{head + 1}"
        if line > head:
            sheet.auto_filter.ref = f"A{head}:{get_column_letter(len(self._ASSET_COLUMNS))}{line}"
        _fit(sheet)

    # ── 파일 만들기 ─────────────────────────────────────────────────────
    def build(self, section: str = ALL) -> Workbook:
        section = str(section or ALL).strip().lower()
        if section != ALL and section not in SECTIONS:
            raise ValueError(
                "지원 항목은 " + ", ".join([ALL, *SECTIONS]) + f" 입니다: {section}"
            )
        wanted = list(SECTIONS) if section == ALL else [section]

        workbook = Workbook()
        workbook.remove(workbook.active)
        for name in wanted:
            sheet = workbook.create_sheet(SECTIONS[name][:31])
            if name == "server_all":
                self._write_status_table(sheet, "서버현황 (전체 서버)", self.status.get("all") or {})
            elif name == "server_physical":
                self._write_status_table(sheet, "서버현황 (물리서버)", self.status.get("physical") or {})
            elif name == "movements":
                self._write_movements(sheet)
            elif name == "eosl":
                self._write_eosl(sheet)
            elif name == "excluded":
                excluded = (self.status.get("excluded") or {}).get("items") or []
                self._write_assets(sheet, "자산에서 제외한 대상", excluded)
                sheet["A2"] = f"제외 기준: {(self.status.get('excluded') or {}).get('reason') or '-'}"
                sheet["A2"].font = Font(size=10, color="666666")
            elif name == "assets":
                self._write_assets(sheet, "자산 목록(제외 대상 포함)", self.records)

        if not workbook.sheetnames:            # 방어. wanted 가 빌 일은 없다.
            workbook.create_sheet("빈 결과")
        return workbook

    def file_name(self, section: str = ALL, base_day: date | None = None) -> str:
        label = "월간점검_전체" if section == ALL else f"월간점검_{SECTIONS[section]}"
        day = (base_day or date.today()).strftime("%Y%m")
        return f"{label}_{day}_{datetime.now().strftime('%H%M%S')}.xlsx"

    def save(self, target_dir: Path, section: str = ALL, base_day: date | None = None) -> Path:
        workbook = self.build(section)
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / self.file_name(section, base_day)
        workbook.save(path)
        workbook.close()
        return path
