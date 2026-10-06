"""월간 점검 장표를 엑셀로 낸다.

화면에 보이는 것을 그대로 옮긴다. 화면과 파일의 숫자가 다르면 어느 쪽을 믿어야
하는지 알 수 없으므로, 집계는 새로 하지 않고 ``ServerStatusService`` 가 이미
만든 결과를 받아 쓴다.

항목마다 따로 받을 수 있게 만든다. 월간 보고에 붙일 때 필요한 장표만 뽑는 일이
많고, 전부 한 파일로 주면 쓰는 사람이 시트를 지워야 한다.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

from .asset_scope import ACTION_CHOICES
from .change_presenter import FIELD_LABELS

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

    #: 사람이 채워 넣는 열. 받아서 표시하고 다시 올리는 길이다. 맨 앞에 둔다.
    _EDIT_COLUMNS: tuple[tuple[str, str], ...] = (
        ("처리", "_action"), ("제외 사유", "_reason"),
    )

    #: 해석해서 만든 값. 원본 컬럼 앞에 둔다. 코드가 아니라 사람이 읽는 값이다.
    _DERIVED_COLUMNS: tuple[tuple[str, str], ...] = (
        ("자산번호", "cm_id"), ("업무명", "service_name"), ("호스트명", "hostname"),
        ("IP", "primary_ip"), ("위치", "location"), ("물리/논리", "_kind"),
        ("OS", "os_family"), ("OS 묶음", "os_group"), ("OS버전", "os_version"),
        ("CPU Core", "cpu_cores"), ("Memory MB", "memory_mb"),
        ("EOSL 연도", "eosl_year"), ("EOSL 컬럼", "eosl_field"),
        ("자산 여부", "_included"), ("제외 사유(현재)", "exclude_label"),
        ("수동 지정", "_manual"), ("수동 사유(현재)", "manual_note"),
    )

    #: 원본 컬럼 중 앞에 놓을 것. 나머지는 이 뒤에 사전순으로 붙는다.
    _RAW_ORDER: tuple[str, ...] = (
        "CM_ID", "CM_NAME", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP",
        "CM_STA_CD", "CM_SVR_CAT_CD", "CM_CAT_CD", "CM_OWN_CAT_CD", "CM_NET_CD",
        "CM_OS", "CM_OS_VERSION", "CM_CPU_CNT", "CM_CPU_CORE_CNT", "CM_MEMORY",
        "CM_EOL_DT", "OS_EOS_DATE", "CM_PLACE", "CM_RACK_LOC",
        "CM_MAKE_NAME", "CM_MODEL_NAME", "CM_SERIAL_NO",
        "CM_OWN_EMP_ID", "CM_OWN_DPT_ID", "CM_USER_EMP_ID", "CM_USER_DPT_ID",
        "CM_WOR_MNG_EMP_ID", "CM_TAKIN_DTTM", "CM_DESCR", "CM_DESCR2",
    )

    @classmethod
    def raw_columns(cls, items: list[dict[str, Any]]) -> list[str]:
        """원본에 실제로 들어 있는 컬럼 목록.

        ITSM 조회 SQL 에 따라 컬럼이 달라지므로 코드에 못 박지 않는다. 자료에서
        모으고, 자주 보는 것을 앞에 둔 뒤 나머지는 사전순으로 붙인다.
        """
        found: set[str] = set()
        for item in items:
            found.update(str(key) for key in (item.get("raw") or {}))
        ordered = [name for name in cls._RAW_ORDER if name in found]
        return ordered + sorted(found - set(ordered))

    #: vCenter VM 목록의 집계값. ITSM 자산과 열이 다르다.
    _VM_COLUMNS: tuple[tuple[str, str], ...] = (
        ("VM 이름", "vm_name"), ("호스트명", "hostname"), ("IP", "primary_ip"),
        ("vCenter", "vcenter_id"), ("통합기(Cluster)", "cluster_name"), ("ESXi", "esxi_host"),
        ("전원", "power_state"), ("OS", "os_family"), ("OS버전", "os_version"),
        ("vCPU", "cpus"), ("Memory MB", "memory_mb"), ("VM UUID", "vm_uuid"),
        ("자산키", "asset_key"),
        ("자산 여부", "_included"), ("제외 사유(현재)", "exclude_label"),
        ("수동 지정", "_manual"), ("수동 사유(현재)", "manual_note"),
    )

    @classmethod
    def _columns_for(cls, items: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
        """ITSM 자산인지 vCenter VM 인지 보고 열을 고른다."""
        if any("vm_name" in item for item in items):
            return cls._VM_COLUMNS
        return cls._DERIVED_COLUMNS

    def _write_assets(
        self,
        sheet: Any,
        title: str,
        items: Iterable[dict[str, Any]],
        *,
        note: str = "",
    ) -> None:
        """자산 목록. 해석한 값 + **ITSM 원본 전 컬럼**.

        화면(팝업)은 업무명·호스트명·IP 처럼 꼭 필요한 것만 보여 준다. 화면에서
        스무 컬럼을 늘어놓으면 읽을 수 없기 때문이다. 하지만 엑셀로 받을 때는
        전 컬럼이 필요하다 -- 받아서 다시 거르고 피벗하기 때문이다.

        머리글은 두 줄이다. 윗줄이 한글 이름, 아랫줄이 원래 컬럼명이다. 필터는
        아랫줄(원래 컬럼명)에 걸린다 -- ITSM 에서 찾을 때 쓰는 이름이 그쪽이다.
        """
        rows = list(items)
        sheet["A1"] = title
        sheet["A1"].font = Font(bold=True, size=13)
        basis = [f"기준일 {self.status.get('as_of') or '-'}", f"{len(rows):,}건"]
        if note:
            basis.append(note)
        sheet["A2"] = " · ".join(basis)
        sheet["A2"].font = Font(size=10, color="666666")

        raw_columns = self.raw_columns(rows)
        derived = self._columns_for(rows)
        label_row, name_row = 4, 5

        # 사람이 채우는 열. 200~300 건을 화면에서 하나씩 체크할 수 없으므로,
        # 여기에 제외/포함을 적어 그대로 다시 올리면 한 번에 적용된다.
        for index, (label, _) in enumerate(self._EDIT_COLUMNS, start=1):
            cell = sheet.cell(row=label_row, column=index, value=label)
            cell.fill = PatternFill("solid", fgColor="C55A11")
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center")
            cell.border = _BORDER
            lower = sheet.cell(row=name_row, column=index, value=label)
            lower.fill = PatternFill("solid", fgColor="FBE5D6")
            lower.font = Font(size=9, bold=True, color="833C0C")
            lower.alignment = Alignment(horizontal="center")
            lower.border = _BORDER

        edits = len(self._EDIT_COLUMNS)
        for index, (label, _) in enumerate(derived, start=edits + 1):
            cell = sheet.cell(row=label_row, column=index, value=label)
            cell.fill = _HEAD_FILL
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center")
            cell.border = _BORDER
            lower = sheet.cell(row=name_row, column=index, value="(계산값·수정불가)")
            lower.fill = _SUB_FILL
            lower.font = Font(size=9, color="333333")
            lower.alignment = Alignment(horizontal="center")
            lower.border = _BORDER

        offset = edits + len(derived)
        for index, name in enumerate(raw_columns, start=offset + 1):
            cell = sheet.cell(row=label_row, column=index, value=FIELD_LABELS.get(name, name))
            cell.fill = PatternFill("solid", fgColor="2E6B3E")
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center")
            cell.border = _BORDER
            lower = sheet.cell(row=name_row, column=index, value=name)
            lower.fill = _SUB_FILL
            lower.font = Font(size=9, color="333333")
            lower.alignment = Alignment(horizontal="center")
            lower.border = _BORDER

        line = name_row
        for item in rows:
            line += 1
            filled = dict(item)
            filled["_kind"] = "물리" if item.get("physical") else "논리"
            filled["_included"] = "제외" if item.get("exclude_reason") else "자산"
            filled["_manual"] = "예" if item.get("manual") else ""
            # 처리·사유 칸은 비워 둔다. 사람이 채우는 자리다. 이미 수동으로
            # 제외해 둔 것은 그 사실을 적어 둬야 두 번 적지 않는다.
            if item.get("manual"):
                sheet.cell(row=line, column=1,
                           value="제외" if item.get("exclude_reason") else "포함")
                sheet.cell(row=line, column=2, value=item.get("manual_note") or "")
            for index, (_, key) in enumerate(derived, start=edits + 1):
                value = filled.get(key)
                sheet.cell(row=line, column=index, value="" if value is None else value)
            raw = item.get("raw") or {}
            for index, name in enumerate(raw_columns, start=offset + 1):
                value = raw.get(name)
                # dict·list 가 들어오면 엑셀이 받지 못한다. 글로 바꾼다.
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                sheet.cell(row=line, column=index, value="" if value is None else value)

        # 계산값 열은 엑셀에서 아예 못 고치게 잠근다. 고친 뒤 "왜 반영이 안 되지"
        # 를 묻는 것보다, 고치는 순간 엑셀이 막아 주는 쪽이 낫다. 고쳐야 하는 칸
        # (처리·제외 사유·원본 컬럼)만 열어 둔다.
        editable = Protection(locked=False)
        for line_number in range(name_row + 1, max(line, name_row) + 1):
            for index in range(1, edits + 1):
                sheet.cell(row=line_number, column=index).protection = editable
            for index in range(offset + 1, offset + len(raw_columns) + 1):
                sheet.cell(row=line_number, column=index).protection = editable
        sheet.protection.sheet = True
        # 잠그더라도 정렬·필터는 되어야 한다. 그걸 막으면 쓸 수가 없다.
        sheet.protection.autoFilter = False
        sheet.protection.sort = False
        sheet.protection.formatColumns = False
        sheet.protection.formatRows = False

        sheet.freeze_panes = f"C{name_row + 1}"
        last = get_column_letter(offset + len(raw_columns)) if raw_columns else get_column_letter(offset)
        if line > name_row:
            sheet.auto_filter.ref = f"A{name_row}:{last}{line}"
            # 300 줄을 손으로 쓰면 오타가 난다. 고르게 한다.
            choices = DataValidation(
                type="list", allow_blank=True,
                formula1='"' + ",".join(ACTION_CHOICES) + '"',
                showDropDown=False,
            )
            choices.error = "제외 · 포함 · 자동 중에서 고르세요."
            choices.errorTitle = "처리"
            sheet.add_data_validation(choices)
            choices.add(f"A{name_row + 1}:A{line}")
        # 쓰는 법을 파일 안에 적어 둔다. 설명을 따로 찾지 않아도 되게.
        sheet["A3"] = (
            "▶ 고칠 수 있는 칸은 두 종류입니다. ①주황 머리글 [처리]·[제외 사유]"
            " ②초록 머리글(ITSM 원본 컬럼) -- 여기에 값을 적으면 그 값으로 보정됩니다"
            " (예: CM_EOL_DT 가 비어 있으면 2031-12-31 로 채워 넣기)."
            "  파란 머리글(계산값)은 계산 결과라 잠겨 있습니다."
            "  채운 뒤 이 파일을 그대로 다시 올리면 됩니다."
        )
        sheet["A3"].font = Font(size=10, bold=True, color="C55A11")
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
                self._write_assets(
                    sheet, "자산에서 제외한 대상", excluded,
                    note=f"제외 기준: {(self.status.get('excluded') or {}).get('reason') or '-'}",
                )
            elif name == "assets":
                self._write_assets(sheet, "자산 목록(제외 대상 포함)", self.records)

        if not workbook.sheetnames:            # 방어. wanted 가 빌 일은 없다.
            workbook.create_sheet("빈 결과")
        return workbook

    def build_asset_list(
        self,
        title: str,
        items: list[dict[str, Any]],
        note: str = "",
    ) -> Workbook:
        """자산 목록 한 장만. 화면에서 숫자를 눌러 나온 그 목록을 받을 때 쓴다."""
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "자산 목록"
        self._write_assets(sheet, title, items, note=note)
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
