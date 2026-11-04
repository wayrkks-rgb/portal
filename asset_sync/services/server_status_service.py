"""서버 현황과 EOSL 현황.

월간 점검 장표 두 개를 같은 데이터로 만든다. 첨부 양식에서 물리서버 소계(925)와
EOSL 서버 행 합계가 같고, 전체 소계(2,668)와 EOSL OS 행 합계가 같다. 즉 한 번 고른
대상을 두 장표가 함께 쓴다.

**무엇을 셀지는 여기서 정하지 않는다.** ``asset_scope`` 가 정한다. 통합 대시보드·
일간·주간 점검·보고서가 같은 모듈을 쓰므로 화면마다 대수가 달라질 수 없다.

집계 기준을 코드에 묻어두지 않는다. 대수가 ITSM 총 건수와 다를 때 어느 쪽이 틀렸는지
따질 수 있어야 하므로, 무엇을 어떤 기준으로 세고 무엇을 뺐는지 결과에 같이 담는다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from ..normalization.code_maps import ASSET_STATUS
from .asset_scope import (
    LOCATIONS,
    load_itsm_records,
    LOGICAL_CATEGORY,
    NO_PLAN_YEAR,
    OTHER_GROUP,
    PHYSICAL_CATEGORY,
    AssetScope,
    criteria_from,
    eosl_year,
    os_group,
    parse_year,
)

#: 예전 이름으로 import 하던 곳이 있어 남겨 둔다.
DEFAULT_OS_GROUPS = criteria_from(None).os_groups


class ServerStatusService:
    """ITSM 스냅샷에서 서버 현황·EOSL 현황을 만든다."""

    def __init__(self, config: Any, repository: Any, *, include_all: bool = False) -> None:
        self.config = config
        self.repo = repository
        self.scope = AssetScope.load(config, repository, include_all=include_all)
        self.criteria = self.scope.criteria
        self.include_all = include_all

    def load_records(self, snapshot_id: int) -> list[dict[str, Any]]:
        """사람이 고친 값(보정)을 반영한 레코드. 집계는 모두 이것을 쓴다."""
        return load_itsm_records(self.config, self.repo, snapshot_id)

    # ── 대상 고르기 ─────────────────────────────────────────────────────
    def select(self, snapshot_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(집계 대상, 제외 대상). 제외한 것도 화면에 보여야 하므로 함께 돌려준다."""
        return self.scope.split_itsm(self.load_records(snapshot_id))

    def records(self, snapshot_id: int) -> list[dict[str, Any]]:
        """한 건씩 펼친 전체 목록. 제외된 것도 사유를 달고 들어 있다.

        화면에서 OS·위치를 눌러 그 대수의 실물이 무엇인지 볼 때 쓴다.
        """
        return [self.scope.describe_itsm(record) for record in self.load_records(snapshot_id)]

    # ── 서버 현황 ───────────────────────────────────────────────────────
    def _table(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """양식의 한 표. 행은 IDC·DR·계, 열은 OS 묶음과 소계다."""
        columns = [name for name, _ in self.criteria.os_groups] + [OTHER_GROUP]
        counts: dict[str, Counter[str]] = {location: Counter() for location in LOCATIONS}
        for item in items:
            counts[item["location"]][item["os_group"]] += 1
        rows: dict[str, dict[str, int]] = {}
        for location in LOCATIONS:
            row = {column: int(counts[location][column]) for column in columns}
            row["소계"] = sum(row.values())
            rows[location] = row
        total = {column: sum(rows[location][column] for location in LOCATIONS) for column in columns}
        total["소계"] = sum(total.values())
        rows["계"] = total
        return {"columns": columns + ["소계"], "rows": rows}

    def _delta(self, current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        """전월 대비 증감. 양식의 (+8) · (-2) 에 해당한다."""
        if previous is None:
            return {}
        deltas: dict[str, dict[str, int]] = {}
        for row, values in current["rows"].items():
            before = previous["rows"].get(row, {})
            deltas[row] = {
                column: int(values[column]) - int(before.get(column, 0))
                for column in values
            }
        return deltas

    def status(self, snapshot_id: int, previous_snapshot_id: int | None = None) -> dict[str, Any]:
        included, excluded = self.select(snapshot_id)
        physical = [item for item in included if item["physical"]]

        previous_all = previous_physical = None
        if previous_snapshot_id:
            before_included, _ = self.select(previous_snapshot_id)
            previous_all = self._table(before_included)
            previous_physical = self._table([item for item in before_included if item["physical"]])

        all_table = self._table(included)
        physical_table = self._table(physical)
        records = self.load_records(snapshot_id)
        return {
            "criteria": self.describe_criteria(),
            "counts": {"selected": len(included), "excluded": len(excluded), "physical": len(physical)},
            "scope": self.scope.summary(records),
            "all": {"table": all_table, "delta": self._delta(all_table, previous_all)},
            "physical": {"table": physical_table, "delta": self._delta(physical_table, previous_physical)},
            # 제외한 대상은 목록으로 보여야 한다. 왜 빠졌는지 따지려면 판단에 쓴
            # 값(CM_OS·CM_OS_VERSION·CM_EOL_DT)이 비어 있는 것까지 보여야 한다.
            "excluded": {
                "count": len(excluded),
                "reason": self.describe_exclusion(),
                "items": excluded,
            },
        }

    def describe_exclusion(self) -> str:
        fields = ", ".join(self.criteria.exclude_when_all_empty)
        return (
            f"상태가 집계 기준({self.scope.basis_label} · {', '.join(self.criteria.active_status)})이 아니거나 "
            f"{fields} 가 모두 비어 있거나, 수동으로 제외한 자산"
        )

    def describe_criteria(self) -> dict[str, Any]:
        """무엇을 어떤 기준으로 셌는지. 화면에 그대로 보여준다."""
        criteria = self.criteria.public()
        # 예전 화면이 eosl_field(단수) 를 읽는다. 실제로 쓰는 첫 후보를 준다.
        criteria["eosl_field"] = self.criteria.eosl_fields[0] if self.criteria.eosl_fields else ""
        criteria["include_all"] = self.include_all
        # 화면 머리말에 '기준: 운영' 처럼 쓴다. 관리 → 연계 설정에서 고른 값이다.
        criteria["basis"] = self.scope.basis()
        return criteria

    # ── 신규·삭제 상세 ──────────────────────────────────────────────────
    #: 변동 내역 한 줄에 적을 칸. 이것만 보고 '빼야 할지 둬야 할지' 를 판단한다.
    MOVEMENT_FIELDS = (
        "cm_id", "hostname", "primary_ip", "service_name", "location",
        "os_group", "os_family", "os_version", "eosl_year", "eosl_value",
        "status_code", "physical", "missing",
    )

    def movements(self, snapshot_id: int, previous_snapshot_id: int) -> dict[str, Any]:
        """양식의 '자산 현황 변동 내역'.

        위치 → 물리/논리 → OS 묶음 순서로 센다. 양식이 그 순서로 읽히기 때문이다.

        대수만 주면 "8대 늘었다" 는 알아도 **어느 서버인지** 모른다. 그러면 그걸
        자산에서 빼야 하는지 둬야 하는지 판단할 수 없다. 그래서 실물 목록도 같이
        돌려준다.
        """
        current, current_out = self.select(snapshot_id)
        before, before_out = self.select(previous_snapshot_id)
        current_by_id = {item["cm_id"]: item for item in current}
        before_by_id = {item["cm_id"]: item for item in before}
        # 집계 밖 자산까지 포함한 전후 모습. '대기 → 운영' 처럼 무엇에서 무엇으로 바뀌었는지 적는다.
        current_all = {**{i["cm_id"]: i for i in current_out}, **current_by_id}
        before_all = {**{i["cm_id"]: i for i in before_out}, **before_by_id}

        created = [current_by_id[key] for key in current_by_id.keys() - before_by_id.keys()]
        removed = [before_by_id[key] for key in before_by_id.keys() - current_by_id.keys()]
        created_items = self._movement_items(created)
        removed_items = self._movement_items(removed)
        for row in created_items + removed_items:
            key = row.get("cm_id")
            old, new = before_all.get(key), current_all.get(key)
            row["status_before_label"] = self._state_label(old, "ITSM 에 없음")
            row["status_after_label"] = self._state_label(new, "ITSM 에서 삭제")
            row["transition"] = f"{row['status_before_label']} → {row['status_after_label']}"
        return {
            "created": {**self._breakdown(created), "items": created_items},
            "removed": {**self._breakdown(removed), "items": removed_items},
            # 순증감 = 생성 - 삭제. 화면이 "몇 대 늘고 몇 대 빠져서 이 숫자" 로 읽게 한다.
            "before_total": len(before_by_id),
            "after_total": len(current_by_id),
            "net": len(created) - len(removed),
        }

    @staticmethod
    def _state_label(item: dict[str, Any] | None, absent: str) -> str:
        """상태 이름. 상태가 아닌 이유(수동 제외 · 값 비어 있음 등)로 빠졌으면 그 이유도 붙인다."""
        if item is None:
            return absent
        name = ASSET_STATUS.get(str(item.get("status_code") or ""), str(item.get("status_code") or "상태 없음"))
        reason = item.get("exclude_reason")
        if item.get("manual"):
            return f"{name} · 수동 {'포함' if item.get('included') else '제외'}"
        if reason and reason != "STATUS":
            return f"{name} · {item.get('exclude_label') or reason}"
        return name

    def missing_report(self, snapshot_id: int) -> dict[str, Any]:
        """집계 대상 중 EOSL 현황 조사에 필요한 정보가 빈 자산.

        EOSL · OS 가 비면 EOSL 표에서 '미사용' · '기타' 로 조용히 빠진다. 표만 보면 그게
        실제 값인지 누락인지 알 수 없으므로 따로 모아 보여준다.
        """
        included, _ = self.select(snapshot_id)
        by_field: Counter[str] = Counter()
        items: list[dict[str, Any]] = []
        for item in included:
            missing = item.get("missing") or []
            if not missing:
                continue
            for name in missing:
                by_field["EOSL 형식" if name.startswith("EOSL 형식") else name] += 1
            row = {key: item.get(key) for key in self.MOVEMENT_FIELDS}
            row["kind"] = "물리서버" if item.get("physical") else "논리서버"
            row["status_label"] = ASSET_STATUS.get(str(item.get("status_code") or ""), "")
            items.append(row)
        items.sort(key=lambda r: (-len(r["missing"]), str(r.get("hostname") or ""), str(r.get("cm_id") or "")))
        return {
            "checked": len(included),
            "count": len(items),
            "by_field": dict(by_field.most_common()),
            "required": dict(self.criteria.public()["required_fields"]),
            "items": items,
        }

    @staticmethod
    def annotate_movements(movements: dict[str, Any] | None, digest_rows: list[dict[str, Any]]) -> None:
        """변동 대상마다 **언제 · 왜** 를 일간 증감에서 찾아 붙인다.

        목록은 기간 첫날과 끝날의 ITSM 을 견준 것이고, 날짜와 사유는 일간 보고의 같은
        서버 줄에서 온다. 둘이 같은 판정(집계 기준)을 쓰므로 서로 어긋나지 않는다.
        """
        if not movements:
            return
        found: dict[tuple[str, int], dict[str, Any]] = {}
        for row in digest_rows or []:
            effect = int(row.get("count_effect") or 0)
            cm_id = str(row.get("itsm_cm_id") or "")
            if not effect or not cm_id:
                continue
            key = (cm_id, 1 if effect > 0 else -1)
            held = found.get(key)
            if held is None or str(row.get("day") or "") > str(held.get("day") or ""):
                found[key] = row
        for side, sign in (("created", 1), ("removed", -1)):
            for item in (movements.get(side) or {}).get("items") or []:
                row = found.get((str(item.get("cm_id") or ""), sign))
                if row is None:
                    item["changed_on"] = None
                    item["reason"] = ("일간 기록 없음 — 배치가 빠진 날 바뀌었거나 수동 포함 · 제외로 바뀐 것입니다")
                    continue
                item["changed_on"] = row.get("day")
                item["reason"] = ServerStatusService._movement_reason(row)

    @staticmethod
    def _period_path(rows: list[dict[str, Any]]) -> str:
        # 줄의 요약은 이미 '삭제 · 전원 켜짐 → 꺼짐' 처럼 성격으로 시작한다. 그대로 잇는다.
        return " → ".join(f"{str(r.get('day') or '')[5:]} {r.get('summary') or r.get('kind_label')}" for r in rows)

    @classmethod
    def merge_vcenter_movements(cls, movements: dict[str, Any] | None, digest_rows: list[dict[str, Any]]) -> None:
        """기간 동안 **서버 1대 = 한 곳**(생성 · 삭제 · 변경)으로 목록을 만든다.

        - 대수(순증감)는 ITSM 기준이다. ITSM 스냅샷 비교로 늘거나 빠진 서버가 [생성] · [삭제] 의
          '대수 반영' 줄이다(지금과 같다).
        - 대수는 그대로인데 vCenter 쪽(생성 · 삭제 · 전원)이 기간 처음보다 끝이 높거나 낮으면 [생성] ·
          [삭제] 에 '대수 그대로' 로 넣고, ITSM 반영 칸에 지금 ITSM 과 견준 결과를 적는다.
        - 처음과 끝이 같으면(꺼졌다 다시 켜짐, 미사용 갔다 운영 복귀) 서버 상태는 그대로이므로
          [변경] 에만 경로를 남긴다. 같은 서버가 [생성] 과 [삭제] 에 동시에 나오지 않는다.
        """
        from .change_digest_service import VM_RANK_NAMES, vm_step

        if not movements:
            return
        listed: dict[str, str] = {}
        for side, sign in (("created", 1), ("removed", -1)):
            for item in (movements.get(side) or {}).get("items") or []:
                item.setdefault("source", "ITSM")
                item["count_effect"] = sign
                item["itsm_reflection"] = "ITSM 반영됨"
                item["itsm_action"] = ""
                listed[str(item.get("cm_id") or "")] = side
        changed = movements.setdefault("changed", {"items": []})
        changed_items = changed.setdefault("items", [])

        per_server: dict[str, list[dict[str, Any]]] = {}
        for row in sorted(digest_rows or [], key=lambda r: str(r.get("day") or "")):
            if vm_step(row) or int(row.get("count_effect") or 0):
                per_server.setdefault(str(row.get("server_key")), []).append(row)

        for key, rows in per_server.items():
            last = rows[-1]
            cm_id = str(next((r.get("itsm_cm_id") for r in reversed(rows) if r.get("itsm_cm_id")), "") or "")
            steps = [step for step in (vm_step(r) for r in rows) if step]
            vm_net = (steps[-1][1] - steps[0][0]) if steps else 0
            effect = sum(int(r.get("count_effect") or 0) for r in rows)
            vm_text = ""
            if steps:
                start_rank, end_rank = steps[0][0], steps[-1][1]
                if start_rank == 0 and end_rank > 0:
                    vm_text = "vCenter VM 생성" + (" (전원 꺼짐)" if end_rank == 1 else "")
                elif end_rank == 0 and start_rank > 0:
                    vm_text = "vCenter VM 삭제"
                else:
                    vm_text = f"vCenter {VM_RANK_NAMES[start_rank]} → {VM_RANK_NAMES[end_rank]}"
                if len(steps) > 1:
                    vm_text += f" · 기간 중 {len(steps)}번 바뀜"
            vm_day = next((r.get("day") for r in reversed(rows) if vm_step(r)), last.get("day"))
            # 화면 필터용: 전원(켜짐 ↔ 꺼짐) 인가, VM 자체가 생기거나 없어졌나.
            vcenter_kind = ""
            if steps and steps[0][0] != steps[-1][1]:
                vcenter_kind = "vm" if 0 in (steps[0][0], steps[-1][1]) else "power"
            if cm_id and cm_id in listed:
                # ITSM 비교로 이미 [생성] · [삭제] 에 있다. 한 곳에만 두고 vCenter 사실만 덧붙인다.
                twin = next(i for i in movements[listed[cm_id]]["items"] if str(i.get("cm_id") or "") == cm_id)
                if vm_text:
                    twin["vcenter_note"] = f"{vm_day} {vm_text}"
                continue
            reflected = last.get("reflection") == "REFLECTED"
            base = {
                "source": "VCENTER" if steps and not effect else "ITSM",
                "vcenter_kind": vcenter_kind,
                "changed_on": last.get("day"),
                "cm_id": cm_id or None,
                "hostname": last.get("hostname") or last.get("vm_name"),
                "vm_name": last.get("vm_name"),
                "primary_ip": last.get("primary_ip"),
                "service_name": last.get("service_name"),
                "location": None, "kind": "VM" if steps else None,
                "os_group": last.get("os"), "os_family": None, "os_version": None, "eosl_year": None,
                "status_label": last.get("itsm_status_label") or ("ITSM 자산 없음" if not cm_id else ""),
                "vcenter_label": last.get("vcenter_label"),
                "missing": last.get("missing") or [],
                "count_effect": 0,
                "itsm_reflection": last.get("reflection_label") or ("반영됨" if reflected else "ITSM 미반영"),
                "itsm_reflection_ok": reflected,
                "itsm_action": last.get("itsm_action") or "",
                "reflection_note": last.get("reflection_note") or "",
            }
            itsm_now = last.get("itsm_status_label") or "ITSM 자산 없음"
            base["status_before_label"] = base["status_after_label"] = itsm_now
            base["transition"] = (f"ITSM {itsm_now} 그대로 (vCenter 만 바뀜)" if cm_id
                                  else "ITSM 자산 없음 (vCenter 만)")
            if effect == 0 and vm_net == 0:
                changed_items.append({**base, "reason": "처음과 끝이 같음 — " + cls._period_path(rows),
                                      "itsm_reflection": "-", "itsm_action": "", "itsm_reflection_ok": True})
                continue
            if effect != 0:
                # ITSM 비교(대수)와 일간 합이 어긋난 경우. 수동 포함 · 제외를 나중에 바꿨을 때 생긴다.
                changed_items.append({**base, "reason": "일간 증감과 월간 비교가 다름 — " + cls._period_path(rows)})
                continue
            side = "created" if vm_net > 0 else "removed"
            block = movements.setdefault(side, {"total": 0, "items": []})
            block.setdefault("items", []).append({**base, "reason": vm_text})

        for side in ("created", "removed"):
            block = movements.get(side) or {}
            items = block.get("items") or []
            block["counted"] = sum(1 for i in items if i.get("count_effect"))
            block["vcenter_only"] = sum(1 for i in items if not i.get("count_effect"))
            # 대수 그대로인 것 중 전원(켜짐 ↔ 꺼짐) 과 VM 생성 · 삭제를 나눠 센다.
            block["vcenter_power"] = sum(
                1 for i in items if not i.get("count_effect") and i.get("vcenter_kind") == "power")
            block["vcenter_vm"] = sum(
                1 for i in items if not i.get("count_effect") and i.get("vcenter_kind") == "vm")
            block["itsm_pending"] = sum(
                1 for i in items if not i.get("count_effect") and not i.get("itsm_reflection_ok"))
            block["listed"] = len(items)
            items.sort(key=lambda i: (0 if i.get("count_effect") else 1, str(i.get("changed_on") or ""),
                                      str(i.get("hostname") or "")))
        changed["listed"] = len(changed_items)
        movements["vcenter_toggled"] = len(changed_items)

    @staticmethod
    def attach_excluded(movements: dict[str, Any] | None, excluded_rows: list[dict[str, Any]]) -> None:
        """자산에서 뺀 VM(vCLS · 템플릿 · 수동 제외)의 vCenter 변화. 서버 1대 = 1줄.

        이 VM 들은 ITSM 에 등록할 대상이 아니므로 [생성] · [삭제] 와 'ITSM 반영 필요' 에 넣지 않는다.
        그래도 무엇이 빠졌는지는 보여야 하므로 따로 모은다.
        """
        from .change_digest_service import VM_RANK_NAMES, vm_step

        if movements is None:
            return
        per_server: dict[str, list[dict[str, Any]]] = {}
        for row in sorted(excluded_rows or [], key=lambda r: str(r.get("day") or "")):
            per_server.setdefault(str(row.get("server_key")), []).append(row)
        items = []
        for rows in per_server.values():
            last = rows[-1]
            steps = [step for step in (vm_step(r) for r in rows) if step]
            change = last.get("summary") or last.get("kind_label") or ""
            if steps:
                first, end = steps[0][0], steps[-1][1]
                change = ("vCenter VM 생성" if first == 0 and end > 0 else
                          "vCenter VM 삭제" if end == 0 and first > 0 else
                          f"vCenter {VM_RANK_NAMES[first]} → {VM_RANK_NAMES[end]}")
            items.append({
                "changed_on": last.get("day"),
                "vm_name": last.get("vm_name"),
                "hostname": last.get("hostname") or last.get("vm_name"),
                "primary_ip": last.get("primary_ip"),
                "vcenter_label": last.get("vcenter_label") or last.get("vcenter_id"),
                "cluster_label": last.get("cluster_label") or last.get("cluster_name"),
                "esxi_label": last.get("esxi_label") or last.get("esxi_host"),
                "change": change,
                "exclude_reason": last.get("exclude_reason"),
                "exclude_label": last.get("exclude_label"),
                "vm_key": last.get("vm_key"),
            })
        items.sort(key=lambda i: (str(i.get("exclude_reason") or ""), str(i.get("hostname") or "")))
        by_reason: dict[str, int] = {}
        for item in items:
            by_reason[str(item.get("exclude_reason") or "")] = by_reason.get(str(item.get("exclude_reason") or ""), 0) + 1
        movements["excluded"] = {"items": items, "total": len(items), "by_reason": by_reason}

    @staticmethod
    def _movement_reason(row: dict[str, Any]) -> str:
        types = set(row.get("event_types") or [])
        if "ITSM_ASSET_CREATED" in types:
            return "ITSM 신규 등록"
        if "ITSM_RECORD_REMOVED" in types:
            return "ITSM 레코드 삭제"
        summary = str(row.get("summary") or "")
        for head in ("생성 · ", "삭제 · ", "생성", "삭제"):
            if summary.startswith(head):
                summary = summary[len(head):]
                break
        return summary.strip() or str(row.get("count_note") or "")

    def _movement_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """변동 내역 한 줄씩. 읽기 쉬운 순서(위치 → 구분 → 호스트명)로 세운다."""
        rows = [
            {key: item.get(key) for key in self.MOVEMENT_FIELDS}
            for item in items
        ]
        for row in rows:
            row["kind"] = "물리서버" if row.get("physical") else "논리서버"
            row["status_label"] = ASSET_STATUS.get(str(row.get("status_code") or ""), "")
        rows.sort(key=lambda r: (
            str(r.get("location") or ""), str(r.get("kind") or ""),
            str(r.get("hostname") or ""), str(r.get("cm_id") or ""),
        ))
        return rows

    def _breakdown(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        tree: dict[str, dict[str, Counter[str]]] = {
            location: {"논리서버": Counter(), "물리서버": Counter()} for location in LOCATIONS
        }
        for item in items:
            kind = "물리서버" if item["physical"] else "논리서버"
            tree[item["location"]][kind][item["os_group"]] += 1
        locations: dict[str, Any] = {}
        for location in LOCATIONS:
            kinds = {
                kind: {"total": sum(counter.values()), "os": dict(counter)}
                for kind, counter in tree[location].items()
            }
            locations[location] = {"total": sum(k["total"] for k in kinds.values()), "kinds": kinds}
        return {"total": len(items), "locations": locations}

    # ── EOSL 현황 ───────────────────────────────────────────────────────
    def eosl(
        self,
        snapshot_id: int,
        today: date | None = None,
        previous_snapshot_id: int | None = None,
    ) -> dict[str, Any]:
        """양식의 EOSL 표. 대상은 서버 현황과 같아야 한다.

        연도만 본다. 9999 는 계획 없음이고, 연도를 못 읽으면 미사용으로 센다.

        전월 스냅샷을 주면 칸마다 증감을 같이 낸다. 서버 현황만 증감이 보이고
        EOSL 은 안 보이면, 늘어난 서버가 어느 연도에 걸렸는지 알 수 없다.
        """
        year = (today or date.today()).year
        included, _ = self.select(snapshot_id)
        physical = [item for item in included if item["physical"]]
        current = {
            "physical": {"label": "서버", **self._eosl_row(physical, year)},
            "all": {"label": "OS", **self._eosl_row(included, year)},
        }
        delta: dict[str, dict[str, int]] = {}
        if previous_snapshot_id:
            before, _ = self.select(previous_snapshot_id)
            before_rows = {
                "physical": self._eosl_row([i for i in before if i["physical"]], year),
                "all": self._eosl_row(before, year),
            }
            for key, row in before_rows.items():
                delta[key] = {
                    column: int(current[key]["counts"].get(column, 0)) - int(row["counts"].get(column, 0))
                    for column in current[key]["counts"]
                }
                delta[key]["전체"] = int(current[key]["total"]) - int(row["total"])
        return {
            "criteria": {
                "eosl_field": self.criteria.eosl_fields[0] if self.criteria.eosl_fields else "",
                "eosl_fields": list(self.criteria.eosl_fields),
                "base_year": year, "no_plan_year": NO_PLAN_YEAR,
            },
            "columns": self.eosl_columns(year),
            # 양식에서 '서버' 행은 물리서버, 'OS' 행은 전체서버다.
            "physical": current["physical"],
            "all": current["all"],
            "delta": delta,
            "diagnosis": self.eosl_diagnosis(included),
        }

    def eosl_diagnosis(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """EOSL 값이 어디서 왔고 어떻게 읽혔는지.

        표가 온통 '계획 없음' 이거나 '미사용' 이면, 그게 실제 데이터인지 컬럼을
        잘못 짚은 것인지 표만 봐서는 알 수 없다. 어느 컬럼에서 몇 건을 읽었고
        원래 값이 어떻게 생겼는지 같이 내려보내 화면에서 바로 확인하게 한다.
        """
        fields: Counter[str] = Counter()
        samples: dict[str, list[str]] = {}
        unreadable: list[str] = []
        for item in items:
            name = item.get("eosl_field") or "(값 없음)"
            fields[name] += 1
            if item.get("eosl_field"):
                bucket = samples.setdefault(name, [])
                text = str(item.get("eosl_value"))
                if len(bucket) < 5 and text not in bucket:
                    bucket.append(text)
                if item.get("eosl_year") is None and len(unreadable) < 10:
                    unreadable.append(text)
        return {
            "by_field": dict(fields),
            "samples": samples,
            # 값은 있는데 연도를 못 읽은 것. 형식이 예상과 다르다는 뜻이다.
            "unreadable_samples": unreadable,
            "checked_fields": list(self.criteria.eosl_fields),
        }

    @staticmethod
    def eosl_columns(year: int) -> list[str]:
        return [f"{year}년 이전", f"{year}년", f"{year + 1}년", f"{year + 2}년 이상", "계획 없음", "미사용"]

    @staticmethod
    def eosl_bucket(eosl_year_value: Any, year: int) -> str:
        """EOSL 연도를 장표의 열 하나로 묶는다.

        표의 숫자를 눌러 그 자산을 볼 때도 같은 함수를 써야 한다. 두 군데에
        따로 적으면 표의 숫자와 목록의 줄 수가 어긋난다.
        """
        if eosl_year_value is None:
            return "미사용"
        value = int(eosl_year_value)
        if value >= NO_PLAN_YEAR:
            return "계획 없음"
        if value < year:
            return f"{year}년 이전"
        if value == year:
            return f"{year}년"
        if value == year + 1:
            return f"{year + 1}년"
        return f"{year + 2}년 이상"

    def _eosl_row(self, items: list[dict[str, Any]], year: int) -> dict[str, Any]:
        buckets: Counter[str] = Counter()
        columns = self.eosl_columns(year)
        for item in items:
            buckets[self.eosl_bucket(item["eosl_year"], year)] += 1
        counts = {column: int(buckets[column]) for column in columns}
        return {"total": len(items), "counts": counts}
