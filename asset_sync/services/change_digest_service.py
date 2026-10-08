"""증감 현황을 **서버 한 대 = 한 줄**로 묶고, ITSM 에 반영됐는지 함께 본다.

증감 현황에서 알아야 할 것은 두 가지뿐이다.

1. **어떤 서버가** 실제로 추가·삭제·수정됐나
2. vCenter 에서 바뀐 것이 **ITSM 에도 반영됐나**

지금까지는 그 둘을 알기 어려웠다. 한 자산의 변경이 묶음 이벤트(ITSM_ASSET_UPDATED)
와 항목별 이벤트로 흩어져 같은 서버가 네다섯 줄로 나왔고, 0 에서 빈 값으로 바뀐
것처럼 뜻 없는 변경까지 같은 무게로 끼어 있었다. 반영 여부는 아예 없었다.

여기서는 한 서버의 하루치 변경을 한 줄로 접고, 바뀐 항목을 그 줄 안에 적는다.
vCenter 변경에는 **지금 ITSM 값과 견준 결과**를 붙인다 -- 이벤트끼리 견주면
ITSM 을 며칠 늦게 고친 경우를 미반영으로 잘못 읽는다.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from . import asset_matching
from .asset_scope import AssetScope, load_itsm_records
from .change_presenter import present

#: 한 줄의 성격. 앞의 셋은 대수가 바뀌는 것, CHANGED 는 내용만 바뀐 것.
CREATED = "CREATED"
REMOVED = "REMOVED"
STATUS = "STATUS"
CHANGED = "CHANGED"

KIND_LABELS = {CREATED: "신규", REMOVED: "삭제", STATUS: "상태변경", CHANGED: "변경"}

#: 이벤트 → 줄의 성격.
KIND_OF = {
    "ITSM_ASSET_CREATED": CREATED, "ITSM_ASSET_REACTIVATED": CREATED, "RV_NEW": CREATED,
    "ITSM_RECORD_REMOVED": REMOVED, "RV_REMOVED": REMOVED,
    "ITSM_STATUS_TO_UNUSED": STATUS, "ITSM_STATUS_TO_DISPOSED": STATUS,
    "ITSM_STATUS_CHANGED": STATUS,
}

#: 묶음 이벤트. 항목별 이벤트와 같은 내용을 한 번 더 적는 것이라 줄로 세지 않는다.
GROUP_EVENTS = {"ITSM_ASSET_UPDATED"}

#: 값이 통째로 들어 있어 '항목 변경' 으로 적을 수 없는 이벤트.
WHOLE_RECORD_EVENTS = {
    "ITSM_ASSET_CREATED", "ITSM_RECORD_REMOVED", "RV_NEW", "RV_REMOVED", "COLLECTION_GAP",
}

#: 값이 없다는 뜻으로 읽히는 것. 숫자 칸에서 0 과 빈 값은 둘 다 '모름' 이다.
EMPTY_VALUES = {"", "-", "0", "0.0", "0gb", "0mb", "none", "null", "n/a"}

#: vCenter 변경이 ITSM 에 반영됐는지.
REFLECTED = "REFLECTED"
NOT_REFLECTED = "NOT_REFLECTED"
NO_ASSET = "NO_ASSET"
NOT_CHECKED = "NOT_CHECKED"

REFLECTION_LABELS = {
    REFLECTED: "반영됨",
    NOT_REFLECTED: "ITSM 미반영",
    NO_ASSET: "ITSM 자산 못 찾음",
    NOT_CHECKED: "대상 아님",
}


def _is_empty(value: Any) -> bool:
    """값이 없다고 읽히는가. 숫자 칸의 0 과 빈 값은 둘 다 '모름' 이다."""
    return str(value or "").strip().lower() in EMPTY_VALUES


class ChangeDigestService:
    """변경 이벤트를 서버 단위로 접고, ITSM 반영 여부를 붙인다."""

    def __init__(self, config: Any, repository: Any, scope: AssetScope | None = None) -> None:
        self.config = config
        self.repo = repository
        self.scope = scope or AssetScope.load(config, repository)

    # ── 바깥에서 부르는 것 ──────────────────────────────────────────────
    def digest(
        self,
        start: str,
        end: str,
        *,
        source: str | None = None,
        include_trivial: bool = False,
    ) -> dict[str, Any]:
        events = self.repo.changes(source, 100000, start, end)
        rows = self._fold(events, include_trivial=include_trivial)
        self._attach_identity(rows)
        self._attach_reflection(rows)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["kind"]] += 1
        pending = [r for r in rows if r.get("reflection") == NOT_REFLECTED]
        return {
            "period": {"start": start, "end": end},
            "rows": rows,
            "counts": {
                kind: {"count": int(counts.get(kind, 0)), "label": label}
                for kind, label in KIND_LABELS.items()
            },
            "summary": {
                "servers": len({(r["source"], r["asset_key"]) for r in rows}),
                "rows": len(rows),
                "trivial_hidden": sum(int(r.get("trivial_count") or 0) for r in rows),
                "itsm_pending": len(pending),
            },
        }

    def pending_itsm(self, start: str, end: str) -> list[dict[str, Any]]:
        """ITSM 에 아직 반영되지 않은 것만. 담당자에게 넘길 목록이다."""
        return [
            row for row in self.digest(start, end, source="RVTOOLS")["rows"]
            if row.get("reflection") == NOT_REFLECTED
        ]

    # ── 접기 ────────────────────────────────────────────────────────────
    def _fold(self, events: list[dict[str, Any]], *, include_trivial: bool) -> list[dict[str, Any]]:
        """같은 서버의 같은 날 변경을 한 줄로 접는다."""
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        for event in events:
            event_type = str(event.get("event_type") or "")
            if event_type in GROUP_EVENTS:
                # 묶음 이벤트는 항목별 이벤트와 같은 내용이다. 줄로 세지 않는다.
                continue
            day = str(event.get("detected_at") or "")[:10]
            key = (str(event.get("source") or ""), str(event.get("asset_key") or ""), day)
            if key not in buckets:
                buckets[key] = {
                    "source": key[0], "asset_key": key[1], "day": day,
                    "detected_at": event.get("detected_at"),
                    "kind": CHANGED, "fields": [], "trivial": [],
                    "event_types": [],
                }
                order.append(key)
            row = buckets[key]
            row["event_types"].append(event_type)
            # 가장 센 성격이 줄의 성격이 된다. 신규·삭제가 항목 변경보다 세다.
            kind = KIND_OF.get(event_type)
            if kind and self._rank(kind) > self._rank(row["kind"]):
                row["kind"] = kind
            if str(event.get("detected_at") or "") > str(row["detected_at"] or ""):
                row["detected_at"] = event.get("detected_at")
            if event_type in WHOLE_RECORD_EVENTS:
                # 원본 전체가 값 자리에 들어 있다. 항목 변경으로 적을 것이 아니다.
                continue
            field = self._field(event)
            if field is None:
                continue
            (row["trivial"] if field["trivial"] else row["fields"]).append(field)

        rows: list[dict[str, Any]] = []
        for key in order:
            row = buckets[key]
            if include_trivial:
                row["fields"] = row["fields"] + row["trivial"]
                row["trivial"] = []
            row["trivial_count"] = len(row["trivial"])
            # 뜻 있는 변경이 하나도 없고 성격도 '변경' 이면 올릴 것이 없다.
            if row["kind"] == CHANGED and not row["fields"]:
                if not row["trivial_count"]:
                    continue
                row["only_trivial"] = True
            row["kind_label"] = KIND_LABELS[row["kind"]]
            row["summary"] = self._summary(row)
            rows.append(row)
        rows.sort(key=lambda r: (str(r.get("detected_at") or ""), str(r.get("asset_key") or "")),
                  reverse=True)
        return rows

    @staticmethod
    def _rank(kind: str) -> int:
        return {CHANGED: 0, STATUS: 1, REMOVED: 2, CREATED: 3}.get(kind, 0)

    def _field(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """바뀐 항목 하나. 읽을 수 있는 값으로 바꾸고 뜻 없는 변경을 표시한다."""
        shown = present(dict(event))
        name = event.get("field_name")
        if not name:
            return None
        old_raw, new_raw = event.get("old_value"), event.get("new_value")
        # 0 에서 빈 값으로(또는 그 반대로) 바뀐 것은 둘 다 '모름' 이다. 같은
        # 무게로 끼면 진짜 변경이 묻힌다. 버리지는 않고 접어 둔다.
        trivial = _is_empty(old_raw) and _is_empty(new_raw)
        return {
            "name": name,
            "label": shown.get("field_label") or name,
            "old": self._readable(shown.get("old_display"), old_raw),
            "new": self._readable(shown.get("new_display"), new_raw),
            "event_type": event.get("event_type"),
            "trivial": trivial,
        }

    @staticmethod
    def _readable(display: Any, raw: Any) -> str:
        value = display if display not in (None, "") else raw
        if value in (None, ""):
            return "(값 없음)"
        text = str(value)
        return text if len(text) <= 120 else text[:117] + "…"

    @staticmethod
    def _summary(row: dict[str, Any]) -> str:
        if row["kind"] in {CREATED, REMOVED}:
            return KIND_LABELS[row["kind"]]
        parts = [f"{item['label']} {item['old']} → {item['new']}" for item in row["fields"][:4]]
        if len(row["fields"]) > 4:
            parts.append(f"외 {len(row['fields']) - 4}건")
        return " · ".join(parts) or "항목 변경 없음"

    # ── 어느 서버인지 ───────────────────────────────────────────────────
    def _attach_identity(self, rows: list[dict[str, Any]]) -> None:
        """호스트명·IP·업무명을 붙인다. 자산번호만으로는 어느 서버인지 모른다."""
        itsm = self._itsm_by_key()
        rv_snapshot = self.repo.latest_snapshot("RVTOOLS")
        vms = self.repo.load_rv_records(int(rv_snapshot["id"])) if rv_snapshot else {}
        for row in rows:
            if row["source"] == "ITSM":
                record = itsm.get(row["asset_key"]) or {}
                raw = record.get("raw") or {}
                row.update({
                    "hostname": record.get("normalized_hostname") or raw.get("CM_HOSTNAME"),
                    "primary_ip": record.get("primary_ip") or raw.get("CM_IP"),
                    "service_name": raw.get("CM_NAME"),
                    "label": raw.get("CM_NAME") or record.get("normalized_hostname") or row["asset_key"],
                })
                continue
            vm = vms.get(row["asset_key"]) or self._record_from_event(row) or {}
            row.update({
                "hostname": vm.get("normalized_hostname") or vm.get("dns_name"),
                "primary_ip": vm.get("primary_ip"),
                "service_name": vm.get("vm_name"),
                "vm_name": vm.get("vm_name"),
                "esxi_host": vm.get("esxi_host"),
                "vcenter_id": vm.get("vcenter"),
                "label": vm.get("vm_name") or row["asset_key"],
                "_record": vm,
            })

    def _itsm_by_key(self) -> dict[str, dict[str, Any]]:
        snapshot = self.repo.latest_snapshot("ITSM")
        if not snapshot:
            return {}
        return {
            str(record.get("cm_id")): record
            for record in load_itsm_records(self.config, self.repo, int(snapshot["id"]))
        }

    @staticmethod
    def _record_from_event(row: dict[str, Any]) -> dict[str, Any]:
        """지워진 VM 은 지금 스냅샷에 없다. 이벤트가 들고 있는 원본에서 찾는다."""
        import json

        for field in row.get("fields", []):
            for side in ("old", "new"):
                text = field.get(side)
                if isinstance(text, str) and text.strip().startswith("{"):
                    try:
                        parsed = json.loads(text)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(parsed, dict):
                        return parsed
        return {}

    # ── ITSM 에 반영됐나 ────────────────────────────────────────────────
    def _attach_reflection(self, rows: list[dict[str, Any]]) -> None:
        """vCenter 변경이 ITSM 에도 들어갔는지 본다.

        이벤트끼리 견주지 않는다. ITSM 을 며칠 늦게 고친 경우를 미반영으로 잘못
        읽기 때문이다. **지금 ITSM 값**과 견준다 -- 그게 '반영됐나' 의 답이다.
        """
        targets = [row for row in rows if row["source"] == "RVTOOLS"]
        if not targets:
            return
        snapshot = self.repo.latest_snapshot("ITSM")
        if not snapshot:
            for row in targets:
                row["reflection"] = NOT_CHECKED
                row["reflection_label"] = REFLECTION_LABELS[NOT_CHECKED]
                row["reflection_note"] = "ITSM 스냅샷이 없어 견줄 수 없습니다."
            return

        assets = load_itsm_records(self.config, self.repo, int(snapshot["id"]))
        by_key = {str(a.get("cm_id")): a for a in assets}
        index = asset_matching.build_index(
            {str(a.get("cm_id")): self._as_match_record(a) for a in assets}
        )
        identity = {v: k for k, v in self.repo.identity_maps().items()}
        tolerance = int(self.config.matching.get("memory_tolerance_mb", 1))

        for row in targets:
            record = row.get("_record") or {}
            cm_id = self._find_asset(row, record, index, identity)
            asset = by_key.get(cm_id or "")
            verdict, note, action = self._judge(row, record, asset, tolerance)
            row["itsm_cm_id"] = cm_id
            row["reflection"] = verdict
            row["reflection_label"] = REFLECTION_LABELS[verdict]
            row["reflection_note"] = note
            row["itsm_action"] = action
            row.pop("_record", None)
        for row in rows:
            row.pop("_record", None)

    @staticmethod
    def _as_match_record(asset: dict[str, Any]) -> dict[str, Any]:
        """짝짓기 색인은 vCenter 쪽 모양을 받는다. ITSM 자산을 그 모양으로 편다."""
        raw = asset.get("raw") or {}
        return {
            "vm_uuid": None,
            "normalized_hostname": asset.get("normalized_hostname") or raw.get("CM_HOSTNAME"),
            "ip_addresses": asset.get("ip_addresses") or (
                [asset.get("primary_ip")] if asset.get("primary_ip") else []
            ),
            "vm_name": raw.get("CM_NAME"),
        }

    @staticmethod
    def _find_asset(
        row: dict[str, Any],
        record: dict[str, Any],
        index: asset_matching.MatchIndex,
        identity: dict[str, str],
    ) -> str | None:
        """이 VM 에 해당하는 ITSM 자산번호. 기억해 둔 짝이 있으면 그것이 먼저다."""
        remembered = identity.get(str(record.get("vm_uuid") or ""))
        if remembered:
            return remembered
        probe = {
            "normalized_hostname": record.get("normalized_hostname") or row.get("hostname"),
            "ip_addresses": record.get("ip_addresses") or (
                [row["primary_ip"]] if row.get("primary_ip") else []
            ),
        }
        match = asset_matching.find(str(row.get("asset_key") or ""), probe, index)
        return match.key

    def _judge(
        self,
        row: dict[str, Any],
        record: dict[str, Any],
        asset: dict[str, Any] | None,
        tolerance: int,
    ) -> tuple[str, str, str]:
        """반영됐는지, 안 됐으면 무엇을 해야 하는지."""
        types = set(row.get("event_types") or [])
        active = {"CMSTA010", "CMSTA050"}
        status = str((asset or {}).get("status_code") or "")

        if "RV_NEW" in types:
            if asset is None:
                return (NOT_REFLECTED, "vCenter 에 새로 생긴 VM 인데 ITSM 에 자산이 없습니다.",
                        "ITSM 에 자산 등록")
            if status not in active:
                return (NOT_REFLECTED,
                        f"ITSM 자산은 있으나 상태가 운영·대기가 아닙니다({status}).",
                        "ITSM 상태를 운영으로")
            return (REFLECTED, "ITSM 에 운영 자산으로 있습니다.", "")

        if "RV_REMOVED" in types:
            if asset is None:
                return (REFLECTED, "ITSM 에도 없습니다.", "")
            if status in active:
                return (NOT_REFLECTED,
                        "vCenter 에서 사라졌는데 ITSM 은 아직 운영·대기입니다.",
                        "ITSM 상태를 미사용·폐기로")
            return (REFLECTED, f"ITSM 상태가 이미 {status} 입니다.", "")

        if asset is None:
            return (NO_ASSET, "짝이 되는 ITSM 자산을 찾지 못했습니다.",
                    "정합성 화면에서 짝을 지어 주세요")

        checks: list[tuple[str, Any, Any]] = []
        if "RV_CPU_CHANGED" in types:
            checks.append(("CPU 코어", record.get("cpus"), asset.get("cpu_cores")))
        if "RV_MEMORY_CHANGED" in types:
            checks.append(("메모리(MB)", record.get("memory_mb"), asset.get("memory_mb")))
        if not checks:
            return (NOT_CHECKED, "ITSM 에 적을 항목이 아닙니다.", "")

        gaps = []
        for label, wanted, held in checks:
            if wanted is None or held is None:
                gaps.append(f"{label}: vCenter {wanted if wanted is not None else '모름'}"
                            f" / ITSM {held if held is not None else '모름'}")
                continue
            if label.startswith("메모리"):
                if abs(int(wanted) - int(held)) > tolerance:
                    gaps.append(f"{label}: vCenter {int(wanted):,} / ITSM {int(held):,}")
            elif int(wanted) != int(held):
                gaps.append(f"{label}: vCenter {int(wanted):,} / ITSM {int(held):,}")
        if gaps:
            return (NOT_REFLECTED, " · ".join(gaps), "ITSM 값을 vCenter 와 맞추기")
        return (REFLECTED, "ITSM 값이 vCenter 와 같습니다.", "")

    # ── 엑셀 ────────────────────────────────────────────────────────────
    EXPORT_COLUMNS = (
        ("일시", "detected_at"), ("출처", "source"), ("구분", "kind_label"),
        ("서버", "label"), ("호스트명", "hostname"), ("IP", "primary_ip"),
        ("자산번호", "asset_key"), ("바뀐 내용", "summary"),
        ("ITSM 반영", "reflection_label"), ("판정 근거", "reflection_note"),
        ("해야 할 일", "itsm_action"), ("ITSM 자산번호", "itsm_cm_id"),
        ("통합기(ESXi)", "esxi_host"),
    )

    @classmethod
    def write_xlsx(cls, result: dict[str, Any], rows: list[dict[str, Any]], path: Any) -> Any:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "증감현황"
        period = result.get("period") or {}
        summary = result.get("summary") or {}
        sheet["A1"] = "증감 현황 · ITSM 반영 점검"
        sheet["A1"].font = Font(bold=True, size=13)
        sheet["A2"] = (
            f"{period.get('start', '-')} ~ {period.get('end', '-')}"
            f" · 서버 {int(summary.get('servers') or 0):,}대"
            f" · ITSM 미반영 {int(summary.get('itsm_pending') or 0):,}건"
        )
        sheet["A2"].font = Font(size=9, color="666666")
        sheet.append([])
        sheet.append([label for label, _ in cls.EXPORT_COLUMNS])
        head = sheet.max_row
        for cell in sheet[head]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="ED7D31")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row in rows:
            sheet.append([row.get(key) for _, key in cls.EXPORT_COLUMNS])
        sheet.freeze_panes = sheet.cell(head + 1, 1).coordinate
        last = sheet.cell(head, len(cls.EXPORT_COLUMNS)).column_letter
        sheet.auto_filter.ref = f"A{head}:{last}{sheet.max_row}"
        for column in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in column)
            sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 48)
        workbook.save(path)
        workbook.close()
        return path
