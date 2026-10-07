"""월간 보고 장표 한 벌. 받은 양식을 그대로 낸다.

보고자료에 **붙여 쓸 수 있어야** 한다. 그래서 원본 자료를 늘어놓는 기존 추출
(``monthly_export_service``, ``resource_usage_service.export_xlsx``)과 따로 둔다.
그쪽은 받아서 다시 거르고 피벗하는 용도이고, 이쪽은 장표 모양 그대로다.

시트 네 장이다.

1. **통합서버자원사용현황** — 전월·당월을 한 줄에 놓는다. 실제 자원은
   "128C / 896G" 처럼 한 칸에 적고, 디스크 사용률은 통합기 하나가 아니라 **같은
   데이터스토어를 쓰는 묶음** 단위로 적는다(장표가 그렇게 병합돼 있다).
   아래에 통합기별 증감을 숫자로만 적고, 그 아래 세부내용에서 어느 VM 이
   생기고 없어졌는지 이름으로 적는다.
2. **통합서버자원사용현황(상세)** — 통합기별 VM 과 그 자원. 첫 시트와 같은 순서다.
3. **서버현황 대시보드** — 전체 현황, 원형 그래프, 3개월 OS 추이, 위치별 변동내역.
4. **물리서버** — 물리서버만 떼어 같은 것을 본다.

대시보드를 앞에 두는 이유: 숫자를 눈으로 먼저 잡고, 그 다음 증감의 실물을 본다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .asset_scope import LOCATIONS
from .resource_usage_service import VMResourceUsageExportService
from .server_status_service import ServerStatusService

#: 장표 색. 받은 양식의 회색 머리글과 연회색 묶음 칸을 그대로 쓴다.
HEAD_FILL = PatternFill("solid", fgColor="D9D9D9")
GROUP_FILL = PatternFill("solid", fgColor="F2F2F2")
NEW_FILL = PatternFill("solid", fgColor="FFF2CC")
SUB_FILL = PatternFill("solid", fgColor="32946A")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")

#: 표 머리글은 두 줄이다. 한 줄로 줄이면 '당월/전월' 과 항목이 섞인다.
HEAD_TOP = 2
HEAD_MID = 3
HEAD_BOTTOM = 4
FIRST_DATA_ROW = 5


def _month_label(day: date) -> str:
    """’26.08 처럼. 장표가 쓰는 표기를 그대로 맞춘다."""
    return f"’{day.strftime('%y')}.{day.strftime('%m')}"


def _previous_month_end(day: date) -> date:
    return day.replace(day=1) - timedelta(days=1)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _fit(sheet: Any, minimum: int = 8, maximum: int = 34) -> None:
    widths: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            length = max(len(line) for line in str(cell.value).split("\n"))
            # 한글은 영문보다 넓게 보인다. 좁게 잡으면 글자가 잘린다.
            wide = sum(1 for ch in str(cell.value) if ord(ch) > 0x2000)
            widths[cell.column] = max(widths.get(cell.column, 0), length + wide // 2)
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = min(max(width + 2, minimum), maximum)


def _head(cell: Any, value: Any = None) -> Any:
    if value is not None:
        cell.value = value
    cell.font = Font(bold=True, size=10)
    cell.fill = HEAD_FILL
    cell.alignment = CENTER
    cell.border = BOX
    return cell


def _body(cell: Any, value: Any = None, *, bold: bool = False, fill: Any = None) -> Any:
    if value is not None:
        cell.value = value
    cell.font = Font(bold=bold, size=10)
    cell.alignment = CENTER
    cell.border = BOX
    if fill is not None:
        cell.fill = fill
    return cell


def _delta_text(current: Any, previous: Any) -> Any:
    """양식 표기: 1145(+9). 변화가 없으면 숫자만 넣는다.

    바뀐 칸만 글자가 되고 나머지는 숫자로 남는다. 받은 양식도 그렇게 적혀 있고,
    그래야 엑셀에서 합계를 다시 낼 수 있다.
    """
    now = int(current or 0)
    if previous is None:
        return now
    change = now - int(previous or 0)
    return now if not change else f"{now:,}({change:+,})"


class MonthlyReportService:
    """월간 보고 장표 한 벌을 만든다."""

    def __init__(self, config: Any, repository: Any, *, include_all: bool = False) -> None:
        self.config = config
        self.repo = repository
        self.include_all = include_all
        self.status_service = ServerStatusService(config, repository, include_all=include_all)
        self.usage_service = VMResourceUsageExportService(config, repository)

    # ── 자료 모으기 ─────────────────────────────────────────────────────
    def collect(self, base_day: date) -> dict[str, Any]:
        """장표에 필요한 자료를 한 번에 모은다.

        화면과 **같은 서비스**를 쓴다. 따로 계산하면 장표와 화면의 숫자가 달라지고,
        그러면 어느 쪽을 믿어야 하는지 알 수 없다.
        """
        previous_day = _previous_month_end(base_day)
        itsm = self.repo.snapshot_on_or_before("ITSM", base_day.isoformat())
        previous_itsm = self.repo.snapshot_on_or_before("ITSM", previous_day.isoformat())

        data: dict[str, Any] = {
            "base_day": base_day,
            "previous_day": previous_day,
            "month_label": _month_label(base_day),
            "previous_label": _month_label(previous_day),
            "itsm": None,
            "movements": None,
            "trend": self._trend(base_day),
            "usage": self._usage(base_day),
            "previous_usage": self._usage(previous_day),
            "vm_changes": self._vm_changes(base_day),
        }
        if itsm:
            data["as_of"] = itsm["snapshot_date"]
            data["itsm"] = self.status_service.status(
                int(itsm["id"]), int(previous_itsm["id"]) if previous_itsm else None
            )
            data["eosl"] = self.status_service.eosl(
                int(itsm["id"]), today=base_day,
                previous_snapshot_id=int(previous_itsm["id"]) if previous_itsm else None,
            )
            if previous_itsm:
                data["previous_as_of"] = previous_itsm["snapshot_date"]
                data["movements"] = self.status_service.movements(
                    int(itsm["id"]), int(previous_itsm["id"])
                )
        return data

    def _usage(self, day: date) -> dict[str, Any] | None:
        """그 달의 자원사용현황. 달 안의 마지막 값이 그 달의 모습이다."""
        try:
            return self.usage_service.summary(_month_start(day).isoformat(), day.isoformat())
        except ValueError:
            return None

    def _vm_changes(self, base_day: date) -> dict[str, dict[str, list[str]]]:
        """통합기(클러스터)별로 어느 VM 이 생기고 없어졌는지.

        대수만 적으면 "3대 늘었다" 는 알아도 무엇이 늘었는지 모른다. 장표 아래
        세부내용에 이름을 적기 위한 자료다.
        """
        usage = self._usage(base_day) or {}
        by_host = {
            f"{row.get('vcenter_id') or ''}|{row.get('esxi_host') or ''}":
                row.get("cluster_display_name") or row.get("cluster_name") or row.get("esxi_host")
            for row in (usage.get("hosts") or [])
        }
        result: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"created": [], "removed": []})
        for change in usage.get("changes") or []:
            kind = {"RV_NEW": "created", "RV_REMOVED": "removed"}.get(str(change.get("event_type")))
            if not kind:
                continue
            key = f"{change.get('vcenter_id') or ''}|{change.get('esxi_host') or ''}"
            name = by_host.get(key) or change.get("esxi_host") or "(통합기 미상)"
            result[str(name)][kind].append(
                str(change.get("vm_name") or change.get("asset_key") or "")
            )
        for block in result.values():
            block["created"].sort()
            block["removed"].sort()
        return dict(result)

    def _trend(self, base_day: date, months: int = 3) -> list[dict[str, Any]]:
        """최근 몇 달의 OS별·구분별 대수. 추이 그래프의 자료다."""
        points: list[dict[str, Any]] = []
        day = base_day
        for _ in range(months):
            snapshot = self.repo.snapshot_on_or_before("ITSM", day.isoformat())
            if snapshot:
                included, _excluded = self.status_service.select(int(snapshot["id"]))
                physical = [item for item in included if item["physical"]]
                points.append({
                    "label": _month_label(day),
                    "total": len(included),
                    "physical": len(physical),
                    "logical": len(included) - len(physical),
                    "os": Counter(item["os_group"] for item in included),
                    "physical_os": Counter(item["os_group"] for item in physical),
                    "location": Counter(item["location"] for item in included),
                })
            day = _previous_month_end(day)
        points.reverse()                               # 오래된 달이 왼쪽
        return points

    # ── 통합기 묶음 ─────────────────────────────────────────────────────
    def _disk_groups(self, usage: dict[str, Any] | None) -> dict[str, str]:
        """클러스터 → 디스크 묶음 이름.

        장표의 디스크 사용률은 통합기 하나가 아니라 **같은 데이터스토어를 쓰는
        묶음** 단위로 적혀 있다(H5:H26 처럼 병합). 어느 클러스터들이 같은
        데이터스토어를 쓰는지는 수집기가 담아 둔 목록으로 알 수 있다.
        """
        if not usage:
            return {}
        by_cluster: dict[str, set[str]] = defaultdict(set)
        for datastore in usage.get("datastores") or []:
            names = datastore.get("cluster_names") or (
                [datastore["cluster_name"]] if datastore.get("cluster_name") else []
            )
            for cluster in names:
                by_cluster[str(cluster)].add(str(datastore.get("datastore_name") or ""))
        return {
            cluster: "|".join(sorted(names)) if names else ""
            for cluster, names in by_cluster.items()
        }

    def _disk_usage(self, usage: dict[str, Any] | None, signature: str) -> float | None:
        """그 묶음의 디스크 사용률. 묶음에 속한 데이터스토어를 합쳐 센다."""
        if not usage or not signature:
            return None
        wanted = set(signature.split("|"))
        capacity = used = 0
        for datastore in usage.get("datastores") or []:
            if str(datastore.get("datastore_name") or "") not in wanted:
                continue
            if not datastore.get("accessible"):
                continue
            capacity += int(datastore.get("capacity_mb") or 0)
            used += int(datastore.get("used_mb") or 0)
        if capacity <= 0:
            return None
        return round(used / capacity * 100, 1)

    def _rows(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """첫 시트의 줄. 위치 → 디스크 묶음 → 통합기 순서다.

        이 순서를 둘째 시트도 그대로 쓴다. 두 시트의 순서가 다르면 장표를 나란히
        놓고 읽을 수 없다.
        """
        usage = data.get("usage")
        previous = data.get("previous_usage")
        if not usage:
            return []
        groups = self._disk_groups(usage)
        before = {
            f"{row.get('vcenter_id') or ''}|{row.get('cluster_name') or ''}": row
            for row in ((previous or {}).get("clusters") or [])
        }
        rows: list[dict[str, Any]] = []
        for cluster in usage.get("clusters") or []:
            key = f"{cluster.get('vcenter_id') or ''}|{cluster.get('cluster_name') or ''}"
            name = str(cluster.get("cluster_name") or "")
            signature = groups.get(name, "")
            old = before.get(key)
            rows.append({
                "location": self._location_of(cluster, usage),
                "name": cluster.get("cluster_display_name") or name or "(통합기 미상)",
                "cluster_name": name,
                "vcenter_id": cluster.get("vcenter_id"),
                "resource": self._resource_text(cluster),
                "cpu_pct": cluster.get("cpu_avg_pct"),
                "mem_pct": cluster.get("mem_avg_pct"),
                "vm_count": int(cluster.get("vm_count") or 0),
                "disk_signature": signature,
                "disk_pct": self._disk_usage(usage, signature),
                # 전월. 없으면 이번에 새로 붙인 통합기다.
                "is_new": old is None,
                "old_cpu_pct": (old or {}).get("cpu_avg_pct"),
                "old_mem_pct": (old or {}).get("mem_avg_pct"),
                "old_vm_count": None if old is None else int(old.get("vm_count") or 0),
                "old_disk_pct": self._disk_usage(previous, signature),
                "cluster": cluster,
            })
        # 위치 → (기존/신규) → 디스크 묶음 → 이름. 장표의 병합 순서가 이것이다.
        # 새로 붙인 통합기는 묶음 맨 뒤로 보낸다. 양식도 그렇게 적혀 있고, 전월이
        # 없는 줄이 중간에 끼면 표가 읽히지 않는다.
        rows.sort(key=lambda r: (
            0 if r["location"] == "IDC" else 1,
            1 if r["is_new"] else 0,
            r["disk_signature"],
            str(r["name"]),
        ))
        return rows

    @staticmethod
    def _location_of(cluster: dict[str, Any], usage: dict[str, Any]) -> str:
        """통합기의 위치. ESXi 이름이나 업무명에 DR 이 들어 있으면 DR 로 본다.

        vCenter 는 IDC·DR 을 따로 알려주지 않는다. ITSM 의 설치위치 코드와 달리
        여기서는 이름으로 가늠할 수밖에 없다.
        """
        text = " ".join(str(value or "") for value in (
            cluster.get("cluster_name"), cluster.get("cluster_display_name"),
            cluster.get("service_name"), cluster.get("vcenter_id"),
        )).upper()
        for host in usage.get("hosts") or []:
            if str(host.get("cluster_name") or "") == str(cluster.get("cluster_name") or ""):
                text += " " + str(host.get("esxi_host") or "").upper()
        return "DR" if "DR" in text else "IDC"

    @staticmethod
    def _resource_text(cluster: dict[str, Any]) -> str:
        """"128C / 896G". 장표가 쓰는 표기 그대로."""
        cores = cluster.get("allocated_cpu_cores")
        memory = cluster.get("allocated_memory_gb")
        if cores in (None, 0) and memory in (None, 0):
            return "-"
        # 천 단위 쉼표는 넣지 않는다. 양식이 '56C / 1500G' 처럼 쓴다.
        core_text = "-" if cores in (None, 0) else f"{int(cores)}C"
        memory_text = "-" if memory in (None, 0) else f"{int(round(float(memory)))}G"
        return f"{core_text} / {memory_text}"

    # ── 시트 1: 통합서버자원사용현황 ────────────────────────────────────
    def _write_usage_sheet(self, sheet: Any, data: dict[str, Any]) -> None:
        rows = self._rows(data)
        sheet.sheet_view.showGridLines = False
        sheet["B1"] = f"통합서버 자원사용현황 ({data['month_label']})"
        sheet["B1"].font = Font(bold=True, size=14)

        # 머리글 두 줄. B2:C4 = '서버', D2:H2 = 당월, I2:L2 = 전월.
        sheet.merge_cells(start_row=HEAD_TOP, start_column=2, end_row=HEAD_BOTTOM, end_column=3)
        _head(sheet.cell(HEAD_TOP, 2), "서버")
        for column in (2, 3):
            for row in range(HEAD_TOP, HEAD_BOTTOM + 1):
                _head(sheet.cell(row, column))
        sheet.merge_cells(start_row=HEAD_TOP, start_column=4, end_row=HEAD_TOP, end_column=8)
        _head(sheet.cell(HEAD_TOP, 4), data["month_label"])
        sheet.merge_cells(start_row=HEAD_TOP, start_column=9, end_row=HEAD_TOP, end_column=12)
        _head(sheet.cell(HEAD_TOP, 9), data["previous_label"])
        for column in range(4, 13):
            _head(sheet.cell(HEAD_TOP, column))

        # 당월: CPU/MEM(실제) · CPU 사용률 · MEM 사용률 · 현재 대수 · 디스크 사용률
        plan = [
            (4, "CPU/MEM", None, True), (5, "CPU", "사용률", False), (6, "MEM", "사용률", False),
            (7, "현재 대수", None, True), (8, "디스크", "사용률", False),
            (9, "CPU", "사용률", False), (10, "MEM", "사용률", False),
            (11, "현재 대수", None, True), (12, "디스크", "사용률", False),
        ]
        for column, top, bottom, merge in plan:
            if merge:
                sheet.merge_cells(start_row=HEAD_MID, start_column=column,
                                  end_row=HEAD_BOTTOM, end_column=column)
            _head(sheet.cell(HEAD_MID, column), top)
            _head(sheet.cell(HEAD_BOTTOM, column), bottom if bottom else None)

        if not rows:
            sheet.cell(FIRST_DATA_ROW, 2, "수집된 통합기 자원사용현황이 없습니다."
                                         " 07시 자동배치가 한 번은 돌아야 합니다.")
            _fit(sheet)
            return

        line = FIRST_DATA_ROW
        for row in rows:
            _body(sheet.cell(line, 2), row["location"], bold=True, fill=GROUP_FILL)
            _body(sheet.cell(line, 3), row["name"], fill=GROUP_FILL).alignment = LEFT
            _body(sheet.cell(line, 4), row["resource"])
            _body(sheet.cell(line, 5), row["cpu_pct"])
            _body(sheet.cell(line, 6), row["mem_pct"])
            _body(sheet.cell(line, 7), row["vm_count"], bold=True)
            _body(sheet.cell(line, 8), row["disk_pct"])
            if row["is_new"]:
                # 전월이 없는 통합기. 0 으로 적으면 '줄었다' 로 읽힌다.
                for column in range(9, 13):
                    _body(sheet.cell(line, column), fill=NEW_FILL)
                sheet.cell(line, 9).value = "신규 생성 통합기"
            else:
                _body(sheet.cell(line, 9), row["old_cpu_pct"])
                _body(sheet.cell(line, 10), row["old_mem_pct"])
                _body(sheet.cell(line, 11), row["old_vm_count"])
                _body(sheet.cell(line, 12), row["old_disk_pct"])
            line += 1

        self._merge_blocks(sheet, rows, column=2, key=lambda r: r["location"])
        # 디스크는 묶음 단위로 한 번만 적는다. 같은 데이터스토어를 쓰는 통합기끼리.
        for column in (8, 12):
            self._merge_blocks(
                sheet, rows, column=column,
                key=lambda r: f"{r['location']}|{r['disk_signature']}",
                skip=lambda r: column == 12 and r["is_new"],
            )
        # 새로 붙인 통합기의 전월 칸은 한 덩어리로 묶어 '신규' 라고 적는다.
        self._merge_new_block(sheet, rows)

        total = line
        _body(sheet.cell(total, 2), "계", bold=True, fill=HEAD_FILL)
        _body(sheet.cell(total, 3), f"통합기 {len(rows):,}개", bold=True, fill=HEAD_FILL)
        _body(sheet.cell(total, 4), None, fill=HEAD_FILL)
        _body(sheet.cell(total, 5), None, fill=HEAD_FILL)
        _body(sheet.cell(total, 6), None, fill=HEAD_FILL)
        _body(sheet.cell(total, 7), sum(r["vm_count"] for r in rows), bold=True, fill=HEAD_FILL)
        _body(sheet.cell(total, 8), None, fill=HEAD_FILL)
        old_total = sum(r["old_vm_count"] or 0 for r in rows if r["old_vm_count"] is not None)
        for column in (9, 10, 12):
            _body(sheet.cell(total, column), None, fill=HEAD_FILL)
        _body(sheet.cell(total, 11), old_total, bold=True, fill=HEAD_FILL)

        self._write_cluster_changes(sheet, data, rows, start=total + 3)
        _fit(sheet)
        sheet.freeze_panes = sheet.cell(FIRST_DATA_ROW, 4).coordinate

    @staticmethod
    def _merge_blocks(sheet: Any, rows: list[dict[str, Any]], *, column: int,
                      key: Any, skip: Any = None) -> None:
        """같은 값이 이어지는 줄을 한 칸으로 묶는다. 장표가 그렇게 생겼다."""
        start = None
        previous_key = object()
        for index, row in enumerate(rows):
            current = None if (skip and skip(row)) else key(row)
            if current != previous_key:
                if start is not None and index - start > 1 and previous_key is not None:
                    sheet.merge_cells(start_row=FIRST_DATA_ROW + start, start_column=column,
                                      end_row=FIRST_DATA_ROW + index - 1, end_column=column)
                start, previous_key = index, current
        if start is not None and len(rows) - start > 1 and previous_key is not None:
            sheet.merge_cells(start_row=FIRST_DATA_ROW + start, start_column=column,
                              end_row=FIRST_DATA_ROW + len(rows) - 1, end_column=column)

    @staticmethod
    def _merge_new_block(sheet: Any, rows: list[dict[str, Any]]) -> None:
        indexes = [index for index, row in enumerate(rows) if row["is_new"]]
        if len(indexes) < 2:
            return
        # 이어져 있을 때만 묶는다. 떨어져 있으면 줄마다 적는다.
        start = indexes[0]
        for previous, current in zip(indexes, indexes[1:] + [None]):
            if current is not None and current == previous + 1:
                continue
            if previous > start:
                sheet.merge_cells(start_row=FIRST_DATA_ROW + start, start_column=9,
                                  end_row=FIRST_DATA_ROW + previous, end_column=12)
            if current is not None:
                start = current

    def _write_cluster_changes(self, sheet: Any, data: dict[str, Any],
                               rows: list[dict[str, Any]], start: int) -> None:
        """통합기별 서버 증감. 위는 숫자만, 아래 세부내용에 이름을 적는다."""
        changes = data.get("vm_changes") or {}
        sheet.cell(start, 2, "통합기별 서버 증감현황").font = Font(bold=True, size=12)
        head = start + 1
        for offset, label in enumerate(("통합기명", "생성 대수", "삭제 대수", "순증감", "비고")):
            _head(sheet.cell(head, 3 + offset), label)

        # 첫 표와 같은 순서로. 증감이 있는 통합기만 적는다.
        order = [row["name"] for row in rows]
        extra = sorted(name for name in changes if name not in order)
        line = head + 1
        created_total = removed_total = 0
        for name in order + extra:
            block = changes.get(name)
            if not block or not (block["created"] or block["removed"]):
                continue
            created, removed = len(block["created"]), len(block["removed"])
            created_total += created
            removed_total += removed
            _body(sheet.cell(line, 3), name).alignment = LEFT
            _body(sheet.cell(line, 4), created)
            _body(sheet.cell(line, 5), removed)
            _body(sheet.cell(line, 6), created - removed, bold=True)
            note = " , ".join(
                part for part in (
                    f"생성 : {created}" if created else "",
                    f"삭제 : {removed}" if removed else "",
                ) if part
            )
            _body(sheet.cell(line, 7), note).alignment = LEFT
            line += 1
        if line == head + 1:
            _body(sheet.cell(line, 3), "이 달에 생성·삭제된 VM 이 없습니다.").alignment = LEFT
            line += 1
        else:
            _body(sheet.cell(line, 3), "총 계", bold=True, fill=HEAD_FILL)
            _body(sheet.cell(line, 4), created_total, bold=True, fill=HEAD_FILL)
            _body(sheet.cell(line, 5), removed_total, bold=True, fill=HEAD_FILL)
            _body(sheet.cell(line, 6), created_total - removed_total, bold=True, fill=HEAD_FILL)
            note = " , ".join(
                part for part in (
                    f"생성 : {created_total}" if created_total else "",
                    f"삭제 : {removed_total}" if removed_total else "",
                ) if part
            )
            _body(sheet.cell(line, 7), note, bold=True, fill=HEAD_FILL).alignment = LEFT
            line += 1

        # 세부내용: 어느 VM 이 생기고 없어졌는지. 숫자만으로는 확인이 안 된다.
        detail = line + 2
        sheet.cell(detail, 2, "세부내용 (통합기별 생성 · 삭제 VM)").font = Font(bold=True, size=12)
        head = detail + 1
        for offset, label in enumerate(("통합기명", "구분", "VM 명")):
            _head(sheet.cell(head, 3 + offset), label)
        line = head + 1
        wrote = False
        for name in order + extra:
            block = changes.get(name)
            if not block:
                continue
            for kind, label in (("created", "생성"), ("removed", "삭제")):
                for vm_name in block[kind]:
                    _body(sheet.cell(line, 3), name).alignment = LEFT
                    _body(sheet.cell(line, 4), label)
                    _body(sheet.cell(line, 5), vm_name).alignment = LEFT
                    line += 1
                    wrote = True
        if not wrote:
            _body(sheet.cell(line, 3), "이 달에 생성·삭제된 VM 이 없습니다.").alignment = LEFT

    # ── 시트 2: 통합기별 VM 상세 ────────────────────────────────────────
    def _write_detail_sheet(self, sheet: Any, data: dict[str, Any]) -> None:
        rows = self._rows(data)
        usage = data.get("usage") or {}
        sheet.sheet_view.showGridLines = False
        sheet.merge_cells(start_row=2, start_column=2, end_row=2, end_column=8)
        title = sheet.cell(2, 2, f"[첨부] 통합서버 상세사항 ({data['base_day'].month}월)")
        title.font = Font(bold=True, size=16)
        title.alignment = CENTER

        by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in usage.get("vms") or []:
            key = f"{row.get('vcenter_id') or ''}|{row.get('cluster_name') or ''}"
            by_cluster[key].append(row)

        line = 4
        for row in rows:
            key = f"{row['vcenter_id'] or ''}|{row['cluster_name'] or ''}"
            members = sorted(by_cluster.get(key, []), key=lambda r: str(r.get("vm_name") or ""))
            sheet.merge_cells(start_row=line, start_column=2, end_row=line, end_column=8)
            header = sheet.cell(line, 2, self._detail_title(row, len(members)))
            header.font = Font(bold=True, size=11)
            header.fill = GROUP_FILL
            header.alignment = LEFT
            header.border = BOX
            line += 1

            top, bottom = line, line + 1
            sheet.merge_cells(start_row=top, start_column=2, end_row=bottom, end_column=2)
            _head(sheet.cell(top, 2), "구분 (VM)")
            sheet.merge_cells(start_row=top, start_column=3, end_row=top, end_column=4)
            _head(sheet.cell(top, 3), "할당량")
            _head(sheet.cell(top, 4))
            sheet.merge_cells(start_row=top, start_column=5, end_row=top, end_column=8)
            _head(sheet.cell(top, 5), "개별서버 부하")
            for column in range(6, 9):
                _head(sheet.cell(top, column))
            for offset, label in enumerate((
                "vCPU(개)", "메모리(GB)", "CPU\nMAX(%)", "CPU\nAVG(%)",
                "메모리\nMAX(%)", "메모리\nAVG(%)",
            )):
                _head(sheet.cell(bottom, 3 + offset), label)
            line = bottom + 1

            if not members:
                sheet.merge_cells(start_row=line, start_column=2, end_row=line, end_column=8)
                _body(sheet.cell(line, 2), "이 통합기에 세는 VM 이 없습니다.").alignment = LEFT
                line += 2
                continue
            for member in members:
                _body(sheet.cell(line, 2), member.get("vm_name")).alignment = LEFT
                _body(sheet.cell(line, 3), member.get("allocated_cpu_cores"))
                _body(sheet.cell(line, 4), member.get("allocated_memory_gb"))
                for offset, field in enumerate(
                    ("cpu_max_pct", "cpu_avg_pct", "mem_max_pct", "mem_avg_pct")
                ):
                    value = member.get(field)
                    _body(sheet.cell(line, 5 + offset),
                          None if value is None else round(float(value), 1))
                line += 1
            # 통합기 소계. 할당 합계를 바로 읽을 수 있어야 한다.
            _body(sheet.cell(line, 2), "소계", bold=True, fill=HEAD_FILL)
            _body(sheet.cell(line, 3),
                  sum(int(m.get("allocated_cpu_cores") or 0) for m in members),
                  bold=True, fill=HEAD_FILL)
            _body(sheet.cell(line, 4),
                  round(sum(float(m.get("allocated_memory_gb") or 0) for m in members), 1),
                  bold=True, fill=HEAD_FILL)
            for column in range(5, 9):
                _body(sheet.cell(line, column), None, fill=HEAD_FILL)
            line += 3

        if not rows:
            sheet.cell(4, 2, "수집된 통합기가 없습니다. 07시 자동배치가 한 번은 돌아야 합니다.")
        _fit(sheet, minimum=10)

    @staticmethod
    def _detail_title(row: dict[str, Any], vm_count: int) -> str:
        cluster = row["cluster"]
        cores = cluster.get("allocated_cpu_cores")
        memory = cluster.get("allocated_memory_gb")
        spec = []
        if cores:
            spec.append(f"{int(cores)}core")
        if memory:
            spec.append(f"{int(round(float(memory)))}GB")
        suffix = f" ({', '.join(spec)})" if spec else ""
        return f"{row['name']}{suffix} : {vm_count:,}개 가상서버 운영"

    # ── 시트 3·4: 서버 현황 대시보드 ────────────────────────────────────
    def _write_dashboard(self, sheet: Any, data: dict[str, Any], *, physical: bool) -> None:
        kind = "물리서버" if physical else "전체 서버"
        sheet.sheet_view.showGridLines = False
        sheet["B1"] = f"서버 현황 대시보드 · {kind} ({data['month_label']})"
        sheet["B1"].font = Font(bold=True, size=14)
        status = data.get("itsm")
        if not status:
            sheet["B3"] = "해당 기간까지의 ITSM 스냅샷이 없습니다. 수집을 먼저 실행하세요."
            _fit(sheet)
            return

        block = status["physical"] if physical else status["all"]
        table, delta = block["table"], block.get("delta") or {}
        sheet["B2"] = (
            f"{data.get('as_of', '-')} 기준"
            + (f" · 전월 {data['previous_as_of']} 대비 증감" if data.get("previous_as_of")
               else " · 비교할 전월 없음")
        )
        sheet["B2"].font = Font(size=9, color="666666")

        # ① 전체 현황. 양식 그대로 셀 안에 증감을 붙인다.
        line = 4
        sheet.cell(line, 2, "① 전체 현황").font = Font(bold=True, size=12)
        line += 1
        columns = list(table["columns"])
        groups = [("UNIX", ["HP", "IBM"]), ("X86", ["Linux", "Windows"])]
        flat: list[tuple[str, str]] = []
        for label, members in groups:
            flat.extend((label, member) for member in members if member in columns)
        flat.extend(("", column) for column in columns if column not in [m for _, m in flat])

        top, bottom = line, line + 1
        sheet.merge_cells(start_row=top, start_column=2, end_row=bottom, end_column=2)
        _head(sheet.cell(top, 2), "구분")
        _head(sheet.cell(bottom, 2))
        column_index = 3
        spans: dict[str, list[int]] = defaultdict(list)
        for label, _member in flat:
            if label:
                spans[label].append(column_index)
            column_index += 1
        column_index = 3
        for label, member in flat:
            if label:
                _head(sheet.cell(top, column_index), label if spans[label][0] == column_index else None)
                _head(sheet.cell(bottom, column_index), member)
            else:
                sheet.merge_cells(start_row=top, start_column=column_index,
                                  end_row=bottom, end_column=column_index)
                _head(sheet.cell(top, column_index),
                      f"{kind} 소계" if member == "소계" else member)
                _head(sheet.cell(bottom, column_index))
            column_index += 1
        for label, indexes in spans.items():
            if len(indexes) > 1:
                sheet.merge_cells(start_row=top, start_column=indexes[0],
                                  end_row=top, end_column=indexes[-1])

        line = bottom + 1
        for name in (*LOCATIONS, "계"):
            values = table["rows"].get(name, {})
            before = delta.get(name, {})
            _body(sheet.cell(line, 2), name, bold=True, fill=GROUP_FILL)
            for offset, (_label, member) in enumerate(flat):
                count = int(values.get(member) or 0)
                change = before.get(member)
                previous = None if change is None else count - int(change)
                _body(sheet.cell(line, 3 + offset), _delta_text(count, previous),
                      bold=name == "계" or member == "소계")
            line += 1

        # ② 구성 비중 (원형). 숫자만 보면 비중이 안 읽힌다.
        line += 1
        sheet.cell(line, 2, "② 구성 비중").font = Font(bold=True, size=12)
        chart_row = line + 1
        os_counts = {
            member: int(table["rows"]["계"].get(member) or 0)
            for _label, member in flat if member != "소계"
        }
        location_counts = {
            name: int(table["rows"].get(name, {}).get("소계") or 0) for name in LOCATIONS
        }
        # 자료 표는 위아래로 쌓고, 그래프는 옆으로 나란히 둔다. 같은 열에 두면
        # 그래프가 서로 겹친다.
        after_os = self._write_pie(sheet, chart_row, os_counts,
                                   title=f"{kind} OS 구성", anchor_column="H")
        after_location = self._write_pie(sheet, after_os, location_counts,
                                         title=f"{kind} 위치 구성", anchor_column="P")
        # 그래프가 차지하는 높이(약 15줄)만큼은 비워 둬야 아래 표를 덮지 않는다.
        line = max(after_location, chart_row + 16)

        # ③ 3개월 추이
        line = self._write_trend(sheet, data, line + 1, physical=physical)

        # ④ 변동 내역. 위치별 생성·삭제를 한 표로.
        self._write_movement_block(sheet, data, line + 1, physical=physical)
        _fit(sheet)

    def _write_pie(self, sheet: Any, row: int, counts: dict[str, int],
                   *, title: str, anchor_column: str) -> int:
        """원형 그래프 하나와 그 자료 표. 자료 없이는 그래프가 안 그려진다."""
        items = [(name, value) for name, value in counts.items() if value]
        if not items:
            sheet.cell(row, 2, f"{title}: 자료가 없습니다.")
            return row + 2
        _head(sheet.cell(row, 2), "항목")
        _head(sheet.cell(row, 3), "대수")
        for offset, (name, value) in enumerate(items, start=1):
            _body(sheet.cell(row + offset, 2), name).alignment = LEFT
            _body(sheet.cell(row + offset, 3), value)
        chart = PieChart()
        chart.title = title
        chart.height, chart.width = 7.5, 11
        chart.add_data(Reference(sheet, min_col=3, min_row=row, max_row=row + len(items)),
                       titles_from_data=True)
        chart.set_categories(Reference(sheet, min_col=2, min_row=row + 1, max_row=row + len(items)))
        chart.dataLabels = DataLabelList()
        chart.dataLabels.showPercent = True
        sheet.add_chart(chart, f"{anchor_column}{row}")
        return row + len(items) + 2

    def _write_trend(self, sheet: Any, data: dict[str, Any], row: int, *, physical: bool) -> int:
        points = data.get("trend") or []
        label = "물리서버" if physical else "전체 서버"
        sheet.cell(row, 2, f"③ 최근 {len(points)}개월 {label} 증가추이").font = Font(bold=True, size=12)
        row += 1
        if len(points) < 2:
            sheet.cell(row, 2, "비교할 과거 스냅샷이 모자랍니다. 달이 두 번은 지나야 추이가 나옵니다.")
            return row + 2

        key = "physical_os" if physical else "os"
        # 열 순서는 전체 현황 표와 같아야 한다. Counter 의 순서를 그대로 쓰면
        # 달마다 열이 뒤바뀌어 그래프를 읽을 수 없다.
        groups = [name for name, _ in self.status_service.criteria.os_groups]
        for point in points:
            for name in point[key]:
                if name not in groups:
                    groups.append(name)
        groups = [name for name in groups
                  if any(point[key].get(name) for point in points)]
        _head(sheet.cell(row, 2), "기준월")
        for offset, name in enumerate(groups):
            _head(sheet.cell(row, 3 + offset), name)
        _head(sheet.cell(row, 3 + len(groups)), "합계")
        for index, point in enumerate(points, start=1):
            _body(sheet.cell(row + index, 2), point["label"])
            for offset, name in enumerate(groups):
                _body(sheet.cell(row + index, 3 + offset), int(point[key].get(name, 0)))
            _body(sheet.cell(row + index, 3 + len(groups)),
                  int(point["physical"] if physical else point["total"]), bold=True)

        chart = LineChart()
        chart.title = f"{label} OS별 증가추이"
        chart.height, chart.width = 8, 18
        chart.y_axis.title = "대수"
        chart.add_data(
            Reference(sheet, min_col=3, max_col=3 + len(groups),
                      min_row=row, max_row=row + len(points)),
            titles_from_data=True,
        )
        chart.set_categories(Reference(sheet, min_col=2, min_row=row + 1, max_row=row + len(points)))
        sheet.add_chart(chart, f"H{row}")
        # 추이 그래프도 높이가 있다. 아래 표와 겹치지 않게 비워 둔다.
        return max(row + len(points) + 2, row + 17)

    def _write_movement_block(self, sheet: Any, data: dict[str, Any], row: int,
                              *, physical: bool) -> int:
        label = "물리서버" if physical else "전체 서버"
        sheet.cell(row, 2, f"④ 한 달간 {label} 변동내역").font = Font(bold=True, size=12)
        row += 1
        movements = data.get("movements")
        if not movements:
            sheet.cell(row, 2, "비교할 전월 스냅샷이 없습니다.")
            return row + 2

        def pick(key: str) -> list[dict[str, Any]]:
            items = (movements.get(key) or {}).get("items") or []
            return [item for item in items
                    if bool(item.get("physical")) == physical] if physical else items

        created, removed = pick("created"), pick("removed")
        _head(sheet.cell(row, 2), "구분")
        for offset, name in enumerate((*LOCATIONS, "계")):
            _head(sheet.cell(row, 3 + offset), name)
        for index, (key, items, name) in enumerate(
            (("created", created, "생성"), ("removed", removed, "삭제")), start=1
        ):
            counts = Counter(str(item.get("location") or "IDC") for item in items)
            _body(sheet.cell(row + index, 2), name, bold=True, fill=GROUP_FILL)
            for offset, location in enumerate(LOCATIONS):
                _body(sheet.cell(row + index, 3 + offset), int(counts.get(location, 0)))
            _body(sheet.cell(row + index, 3 + len(LOCATIONS)), len(items), bold=True)
        net_row = row + 3
        _body(sheet.cell(net_row, 2), "순증감", bold=True, fill=HEAD_FILL)
        for offset, location in enumerate(LOCATIONS):
            net = (sum(1 for i in created if str(i.get("location") or "IDC") == location)
                   - sum(1 for i in removed if str(i.get("location") or "IDC") == location))
            _body(sheet.cell(net_row, 3 + offset), net, bold=True, fill=HEAD_FILL)
        _body(sheet.cell(net_row, 3 + len(LOCATIONS)), len(created) - len(removed),
              bold=True, fill=HEAD_FILL)

        # 실물 목록. 숫자만 보고는 빼야 할지 둬야 할지 판단할 수 없다.
        row = net_row + 2
        sheet.cell(row, 2, "변동 대상 목록").font = Font(bold=True, size=11)
        row += 1
        headers = ("구분", "자산번호", "호스트명", "IP", "업무명", "위치",
                   "물리/논리", "OS", "EOSL", "상태")
        for offset, name in enumerate(headers):
            _head(sheet.cell(row, 2 + offset), name)
        fields = ("cm_id", "hostname", "primary_ip", "service_name", "location",
                  "kind", "os_group", "eosl_year", "status_label")
        line = row + 1
        for name, items in (("생성", created), ("삭제", removed)):
            for item in items:
                _body(sheet.cell(line, 2), name)
                for offset, field in enumerate(fields):
                    cell = _body(sheet.cell(line, 3 + offset), item.get(field))
                    if field in {"hostname", "service_name"}:
                        cell.alignment = LEFT
                line += 1
        if not created and not removed:
            _body(sheet.cell(line, 2), "이 달에 늘거나 빠진 서버가 없습니다.").alignment = LEFT
            line += 1
        return line

    # ── 조립 ────────────────────────────────────────────────────────────
    def build(self, base_day: date) -> Workbook:
        data = self.collect(base_day)
        workbook = Workbook()
        usage = workbook.active
        usage.title = "통합서버자원사용현황"
        self._write_usage_sheet(usage, data)
        self._write_detail_sheet(workbook.create_sheet("통합서버자원사용현황(상세)"), data)
        self._write_dashboard(workbook.create_sheet("서버현황(전체)"), data, physical=False)
        self._write_dashboard(workbook.create_sheet("서버현황(물리)"), data, physical=True)
        return workbook

    @staticmethod
    def file_name(base_day: date) -> str:
        return f"월간보고_{base_day.strftime('%Y%m')}.xlsx"
