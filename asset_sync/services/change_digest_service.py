"""증감 현황을 **서버 한 대 = 한 줄**로 묶는다.

증감 현황에서 알아야 할 것은 세 가지다.

1. **어떤 서버가** 추가·삭제·수정됐나
2. vCenter 에서 바뀐 것이 **ITSM 에도 반영됐나**
3. 그 변경이 **자산 대수를 움직이나** -- 일간에서 본 것이 월간 대수에도 잡히나

한 서버의 변경이 출처별로 갈리면 안 된다. ITSM 쪽 변경 한 줄, vCenter 쪽 변경
한 줄로 나오면 같은 서버를 두 군데서 찾아 맞춰 봐야 한다. 그래서 여기서는 ITSM
자산번호와 vCenter VM 을 **먼저 짝지어** 같은 서버로 묶고, 그 한 줄 안에 양쪽
변경과 지금 값(호스트명·IP·CPU·메모리·통합기)을 함께 적는다. 통합기 화면에 다시
들어가지 않아도 되도록.

반영 여부는 이벤트끼리 견주지 않는다. **지금 ITSM 값**과 견준다 -- 이벤트끼리
견주면 ITSM 을 며칠 늦게 고친 경우를 미반영으로 잘못 읽는다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from . import asset_matching
from .asset_scope import REASON_LABELS, AssetScope, load_itsm_records
from .change_presenter import present
from .display_name_service import DisplayNameService

#: 한 줄의 성격. 앞의 셋은 대수가 바뀔 수 있는 것, CHANGED 는 내용만 바뀐 것.
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

#: 값 자리에 원본 전체가 들어 있는 이벤트. 항목 변경으로 적을 것이 아니지만,
#: 사라진 서버의 호스트명·IP 는 **여기에만** 남아 있으므로 반드시 읽어야 한다.
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

#: 상태코드상 자산으로 세는 것.
ACTIVE_STATUS = {"CMSTA010", "CMSTA050"}


def _is_empty(value: Any) -> bool:
    """값이 없다고 읽히는가. 숫자 칸의 0 과 빈 값은 둘 다 '모름' 이다."""
    return str(value or "").strip().lower() in EMPTY_VALUES


def _as_int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


class _Context:
    """한 번의 조회에 쓰는 밑자료. 줄마다 다시 읽지 않는다."""

    def __init__(self, service: "ChangeDigestService") -> None:
        repo = service.repo
        snapshot = repo.latest_snapshot("ITSM")
        self.itsm_snapshot = snapshot
        assets = (
            load_itsm_records(service.config, repo, int(snapshot["id"])) if snapshot else []
        )
        self.assets = {str(a.get("cm_id")): a for a in assets}
        self.index = asset_matching.build_index(
            {str(a.get("cm_id")): service._as_match_record(a) for a in assets}
        )
        rv_snapshot = repo.latest_snapshot("RVTOOLS")
        self.vms = repo.load_rv_records(int(rv_snapshot["id"])) if rv_snapshot else {}
        # identity_map 은 cm_id → vm_uuid 로 들고 있다. 여기서는 그 반대가 필요하다.
        self.by_vm_uuid = {v: k for k, v in repo.identity_maps().items()}
        self.names = DisplayNameService(repo).resolver(service.config)
        self.tolerance = int(service.config.matching.get("memory_tolerance_mb", 1))


class ChangeDigestService:
    """변경 이벤트를 서버 단위로 접고, ITSM 반영 여부와 대수 효과를 붙인다."""

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
        context = _Context(self)
        rows = self._fold(events, context, include_trivial=include_trivial)
        for row in rows:
            self._attach_identity(row, context)
            self._attach_reflection(row, context)
            self._attach_count_effect(row, context)
            row.pop("_itsm_record", None)
            row.pop("_vm_record", None)
            row.pop("_payloads", None)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["kind"]] += 1
        pending = [r for r in rows if r.get("reflection") == NOT_REFLECTED]
        effects = [int(r.get("count_effect") or 0) for r in rows]
        return {
            "period": {"start": start, "end": end},
            "rows": rows,
            "counts": {
                kind: {"count": int(counts.get(kind, 0)), "label": label}
                for kind, label in KIND_LABELS.items()
            },
            "summary": {
                "servers": len({r["server_key"] for r in rows}),
                "rows": len(rows),
                "trivial_hidden": sum(int(r.get("trivial_count") or 0) for r in rows),
                "itsm_pending": len(pending),
                # 일간에서 본 증감이 월간 대수에 얼마나 반영되는지. 세 숫자가
                # 일·주·월을 한 몸으로 읽게 해 준다.
                "count_added": sum(1 for value in effects if value > 0),
                "count_removed": sum(1 for value in effects if value < 0),
                "count_neutral": sum(1 for value in effects if value == 0),
                "count_net": sum(effects),
            },
        }

    def pending_itsm(self, start: str, end: str) -> list[dict[str, Any]]:
        """ITSM 에 아직 반영되지 않은 것만. 담당자에게 넘길 목록이다."""
        return [
            row for row in self.digest(start, end)["rows"]
            if row.get("reflection") == NOT_REFLECTED
        ]

    # ── 접기 ────────────────────────────────────────────────────────────
    def _fold(
        self, events: list[dict[str, Any]], context: _Context, *, include_trivial: bool
    ) -> list[dict[str, Any]]:
        """같은 **서버**의 같은 날 변경을 한 줄로 접는다.

        출처로 가르지 않는다. ITSM 자산번호와 vCenter VM 을 먼저 짝지어 같은
        서버로 본다 -- 한 서버가 두 줄로 갈리면 찾기 어렵다는 것이 이 화면의
        첫 번째 불편이었다.
        """
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for event in events:
            event_type = str(event.get("event_type") or "")
            if event_type in GROUP_EVENTS:
                # 묶음 이벤트는 항목별 이벤트와 같은 내용이다. 줄로 세지 않는다.
                continue
            source = str(event.get("source") or "")
            asset_key = str(event.get("asset_key") or "")
            payload = self._payload(event) if event_type in WHOLE_RECORD_EVENTS else {}
            server_key = self._server_key(source, asset_key, payload, context)
            day = str(event.get("detected_at") or "")[:10]
            key = (server_key, day)
            if key not in buckets:
                buckets[key] = {
                    "server_key": server_key, "day": day,
                    "detected_at": event.get("detected_at"),
                    "kind": CHANGED, "fields": [], "trivial": [],
                    "event_types": [], "sources": [],
                    "itsm_cm_id": None, "vm_key": None,
                    "_payloads": {},
                }
                order.append(key)
            row = buckets[key]
            row["event_types"].append(event_type)
            if source not in row["sources"]:
                row["sources"].append(source)
            if source == "ITSM":
                row["itsm_cm_id"] = row["itsm_cm_id"] or asset_key
            else:
                row["vm_key"] = row["vm_key"] or asset_key
            if payload:
                row["_payloads"][source] = payload
            # 가장 센 성격이 줄의 성격이 된다. 신규·삭제가 항목 변경보다 세다.
            kind = KIND_OF.get(event_type)
            if kind and self._rank(kind) > self._rank(row["kind"]):
                row["kind"] = kind
            if str(event.get("detected_at") or "") > str(row["detected_at"] or ""):
                row["detected_at"] = event.get("detected_at")
            if event_type in WHOLE_RECORD_EVENTS:
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
            row["itsm_fields"] = [f for f in row["fields"] if f["source"] == "ITSM"]
            row["vcenter_fields"] = [f for f in row["fields"] if f["source"] != "ITSM"]
            row["summary"] = self._summary(row)
            row.pop("trivial", None)
            rows.append(row)
        rows.sort(key=lambda r: (str(r.get("detected_at") or ""), str(r.get("server_key") or "")),
                  reverse=True)
        return rows

    @staticmethod
    def _payload(event: dict[str, Any]) -> dict[str, Any]:
        """신규·삭제 이벤트가 값 자리에 담아 둔 원본 한 벌.

        사라진 서버는 지금 스냅샷에 없다. 호스트명·IP·통합기를 여기서 꺼내지
        않으면 어느 서버였는지 알 수 없고, ITSM 자산도 찾지 못해 '반영됨' 으로
        잘못 읽힌다.
        """
        for side in ("new_value", "old_value"):
            text = event.get(side)
            if not isinstance(text, str) or not text.strip().startswith("{"):
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _server_key(
        self, source: str, asset_key: str, payload: dict[str, Any], context: _Context
    ) -> str:
        """이 이벤트가 가리키는 **서버**의 키. ITSM 자산을 찾으면 그쪽으로 모은다."""
        if source == "ITSM":
            return f"ITSM:{asset_key}"
        record = context.vms.get(asset_key) or payload or {}
        cm_id = self._find_asset(asset_key, record, context)
        return f"ITSM:{cm_id}" if cm_id else f"VM:{asset_key}"

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
            "source": str(event.get("source") or ""),
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
        parts: list[str] = []
        if row["kind"] in {CREATED, REMOVED}:
            parts.append(KIND_LABELS[row["kind"]])
        for item in row["fields"][:4]:
            parts.append(f"{item['label']} {item['old']} → {item['new']}")
        if len(row["fields"]) > 4:
            parts.append(f"외 {len(row['fields']) - 4}건")
        return " · ".join(parts) or "항목 변경 없음"

    # ── 어느 서버인지 ───────────────────────────────────────────────────
    def _attach_identity(self, row: dict[str, Any], context: _Context) -> None:
        """한 줄에 ITSM 쪽과 vCenter 쪽 지금 값을 함께 싣는다.

        여기까지 담아야 통합기 화면에 다시 들어가지 않아도 된다.
        """
        payloads = row.get("_payloads") or {}
        asset = context.assets.get(str(row.get("itsm_cm_id") or "")) or {}
        if not asset and row.get("itsm_cm_id") and "ITSM" in payloads:
            # ITSM 에서 지워진 자산은 지금 스냅샷에 없다. 이벤트의 원본을 쓴다.
            asset = {"raw": payloads["ITSM"]}
        vm = context.vms.get(str(row.get("vm_key") or "")) or payloads.get("RVTOOLS") or {}
        # vCenter 쪽 변경만 있고 ITSM 자산이 있는 서버면, 그 자산도 함께 보여준다.
        if not asset and row["server_key"].startswith("ITSM:"):
            asset = context.assets.get(row["server_key"][5:]) or {}
            row["itsm_cm_id"] = row.get("itsm_cm_id") or row["server_key"][5:]
        # vCenter VM 을 아직 못 붙였으면 자산으로 거꾸로 찾아 본다.
        if not vm and asset:
            vm = self._vm_for_asset(asset, context) or {}
            row["vm_key"] = row.get("vm_key") or (vm.get("asset_key") if vm else None)

        raw = asset.get("raw") or {}
        itsm_cpu = asset.get("cpu_cores") if asset.get("cpu_cores") is not None else _as_int(raw.get("CM_CPU_CORE_CNT"))
        itsm_memory = asset.get("memory_mb") if asset.get("memory_mb") is not None else None
        vcenter_id = vm.get("vcenter") or vm.get("vcenter_id") or ""
        esxi = vm.get("esxi_host") or ""
        cluster = vm.get("cluster_name") or vm.get("cluster") or ""

        row.update({
            # 대표 이름. 업무명 → 호스트명 → VM 이름 순이다.
            "label": (raw.get("CM_NAME") or asset.get("normalized_hostname")
                      or vm.get("vm_name") or row.get("vm_key") or row.get("itsm_cm_id")),
            "service_name": raw.get("CM_NAME"),
            "hostname": (asset.get("normalized_hostname") or raw.get("CM_HOSTNAME")
                         or vm.get("normalized_hostname") or vm.get("dns_name")),
            "primary_ip": asset.get("primary_ip") or raw.get("CM_IP") or vm.get("primary_ip"),
            "asset_key": row.get("itsm_cm_id") or row.get("vm_key"),
            "os": asset.get("os_family") or vm.get("os_family"),
            "itsm_cpu": itsm_cpu,
            "itsm_memory_mb": itsm_memory,
            "itsm_status": asset.get("status_code") or raw.get("CM_STA_CD"),
            "vm_name": vm.get("vm_name"),
            "vm_cpu": _as_int(vm.get("cpus")),
            "vm_memory_mb": _as_int(vm.get("memory_mb")),
            "power_state": vm.get("power_state"),
            "vcenter_id": vcenter_id or None,
            "vcenter_label": context.names.name("VCENTER", vcenter_id, vcenter_id) or None,
            "esxi_host": esxi or None,
            "esxi_label": context.names.name("ESXI", esxi, vcenter_id) or None,
            "cluster_name": cluster or None,
            "cluster_label": context.names.name("CLUSTER", cluster, vcenter_id) or None,
            "_itsm_record": asset,
            "_vm_record": vm,
        })

    def _vm_for_asset(self, asset: dict[str, Any], context: _Context) -> dict[str, Any] | None:
        """자산에 짝지어진 VM. 통합기 정보를 한 줄에 싣기 위해 찾는다."""
        cm_id = str(asset.get("cm_id") or "")
        for key, record in context.vms.items():
            if context.by_vm_uuid.get(str(record.get("vm_uuid") or "")) == cm_id:
                return dict(record, asset_key=key)
        hostname = str(asset.get("normalized_hostname") or "").strip().lower()
        if not hostname:
            return None
        for key, record in context.vms.items():
            if str(record.get("normalized_hostname") or "").strip().lower() == hostname:
                return dict(record, asset_key=key)
        return None

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
        vm_key: str, record: dict[str, Any], context: _Context
    ) -> str | None:
        """이 VM 에 해당하는 ITSM 자산번호. 기억해 둔 짝이 있으면 그것이 먼저다."""
        remembered = context.by_vm_uuid.get(str(record.get("vm_uuid") or ""))
        if remembered:
            return remembered
        ips = record.get("ip_addresses") or (
            [record["primary_ip"]] if record.get("primary_ip") else []
        )
        probe = {
            "normalized_hostname": record.get("normalized_hostname") or record.get("dns_name"),
            "ip_addresses": ips,
        }
        if not probe["normalized_hostname"] and not probe["ip_addresses"]:
            return None
        return asset_matching.find(vm_key, probe, context.index).key

    # ── ITSM 에 반영됐나 ────────────────────────────────────────────────
    def _attach_reflection(self, row: dict[str, Any], context: _Context) -> None:
        """vCenter 변경이 ITSM 에도 들어갔는지 본다."""
        if "RVTOOLS" not in (row.get("sources") or []):
            row["reflection"] = None
            row["reflection_label"] = ""
            row["reflection_note"] = ""
            row["itsm_action"] = ""
            return
        if context.itsm_snapshot is None:
            row["reflection"] = NOT_CHECKED
            row["reflection_label"] = REFLECTION_LABELS[NOT_CHECKED]
            row["reflection_note"] = "ITSM 스냅샷이 없어 견줄 수 없습니다."
            row["itsm_action"] = ""
            return
        asset = context.assets.get(str(row.get("itsm_cm_id") or "")) or None
        verdict, note, action = self._judge(row, row.get("_vm_record") or {}, asset, context)
        row["reflection"] = verdict
        row["reflection_label"] = REFLECTION_LABELS[verdict]
        row["reflection_note"] = note
        row["itsm_action"] = action

    def _judge(
        self,
        row: dict[str, Any],
        record: dict[str, Any],
        asset: dict[str, Any] | None,
        context: _Context,
    ) -> tuple[str, str, str]:
        """반영됐는지, 안 됐으면 무엇을 해야 하는지."""
        types = set(row.get("event_types") or [])
        status = str((asset or {}).get("status_code") or "")

        if "RV_NEW" in types:
            if asset is None:
                return (NOT_REFLECTED, "vCenter 에 새로 생긴 VM 인데 ITSM 에 자산이 없습니다.",
                        "ITSM 에 자산 등록")
            if status not in ACTIVE_STATUS:
                return (NOT_REFLECTED,
                        f"ITSM 자산은 있으나 상태가 운영·대기가 아닙니다({status}).",
                        "ITSM 상태를 운영으로")
            return (REFLECTED, "ITSM 에 운영 자산으로 있습니다.", "")

        if "RV_REMOVED" in types:
            if asset is None:
                return (REFLECTED, "ITSM 에도 없습니다.", "")
            if status in ACTIVE_STATUS:
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
                if abs(int(wanted) - int(held)) > context.tolerance:
                    gaps.append(f"{label}: vCenter {int(wanted):,} / ITSM {int(held):,}")
            elif int(wanted) != int(held):
                gaps.append(f"{label}: vCenter {int(wanted):,} / ITSM {int(held):,}")
        if gaps:
            return (NOT_REFLECTED, " · ".join(gaps), "ITSM 값을 vCenter 와 맞추기")
        return (REFLECTED, "ITSM 값이 vCenter 와 같습니다.", "")

    # ── 자산 대수를 움직이나 ────────────────────────────────────────────
    def _attach_count_effect(self, row: dict[str, Any], context: _Context) -> None:
        """이 변경이 **서버 대수**를 +1 / -1 / 그대로 중 무엇으로 만드는가.

        일간에서 '신규' 로 본 것이 월간 대수에는 안 잡히는 일이 있었다. 상태가
        운영·대기가 아니거나 제외 규칙에 걸리면 자산으로 세지 않기 때문인데,
        화면이 그 말을 안 해 주니 숫자가 어긋난 것처럼 보였다. 여기서 밝힌다.
        """
        types = set(row.get("event_types") or [])
        payloads = row.get("_payloads") or {}
        effect, note = 0, ""

        if "ITSM_ASSET_CREATED" in types or "ITSM_ASSET_REACTIVATED" in types:
            asset = context.assets.get(str(row.get("itsm_cm_id") or ""))
            if asset is None:
                note = "ITSM 자산을 지금 스냅샷에서 찾지 못했습니다."
            else:
                decision = self.scope.decide_itsm(asset)
                if decision.included and not decision.reason:
                    effect, note = 1, "ITSM 운영 자산으로 들어가 서버 대수에 더해집니다."
                else:
                    note = ("자산에서 빠지는 대상이라 서버 대수는 그대로입니다"
                            f" ({REASON_LABELS.get(decision.reason, decision.reason or '제외')}).")
        elif "ITSM_RECORD_REMOVED" in types:
            before = payloads.get("ITSM")
            counted = self._counted_before(before)
            if counted:
                effect, note = -1, "ITSM 에서 빠져 서버 대수에서 하나 줄어듭니다."
            else:
                note = "원래 자산으로 세지 않던 대상이라 서버 대수는 그대로입니다."
        elif "ITSM_STATUS_TO_UNUSED" in types or "ITSM_STATUS_TO_DISPOSED" in types:
            effect, note = -1, "상태가 운영·대기에서 빠져 서버 대수에서 하나 줄어듭니다."
        elif row.get("kind") == CREATED and "RV_NEW" in types:
            if row.get("itsm_cm_id"):
                note = "ITSM 자산이 이미 있어 서버 대수는 그대로입니다."
            else:
                note = "ITSM 에 자산이 없어 서버 대수에 들어가지 않습니다. 등록해야 잡힙니다."
        elif row.get("kind") == REMOVED and "RV_REMOVED" in types:
            note = ("vCenter 에서만 사라졌습니다. ITSM 상태를 바꿔야 서버 대수가 줄어듭니다."
                    if row.get("itsm_cm_id") else "ITSM 자산이 없어 서버 대수는 그대로입니다.")
        else:
            note = "내용만 바뀌어 서버 대수는 그대로입니다."

        row["count_effect"] = effect
        row["count_effect_label"] = {1: "자산 +1", -1: "자산 −1"}.get(effect, "대수 변화 없음")
        row["count_note"] = note

    def _counted_before(self, raw: dict[str, Any] | None) -> bool:
        """지워지기 전에 자산으로 세던 대상이었나. 이벤트에 남은 원본으로 판단한다."""
        if not raw:
            return True                      # 알 수 없으면 세던 것으로 본다
        status = str(raw.get("CM_STA_CD") or "")
        return status in ACTIVE_STATUS if status else True

    # ── 엑셀 ────────────────────────────────────────────────────────────
    EXPORT_COLUMNS = (
        ("일시", "detected_at"), ("구분", "kind_label"), ("서버", "label"),
        ("호스트명", "hostname"), ("IP", "primary_ip"),
        ("ITSM 자산번호", "itsm_cm_id"), ("ITSM 상태", "itsm_status"),
        ("ITSM CPU", "itsm_cpu"), ("ITSM 메모리(MB)", "itsm_memory_mb"),
        ("VM 이름", "vm_name"), ("VM CPU", "vm_cpu"), ("VM 메모리(MB)", "vm_memory_mb"),
        ("통합기", "vcenter_label"), ("클러스터", "cluster_label"), ("ESXi", "esxi_label"),
        ("바뀐 내용", "summary"),
        ("ITSM 반영", "reflection_label"), ("판정 근거", "reflection_note"),
        ("해야 할 일", "itsm_action"),
        ("서버 대수", "count_effect_label"), ("대수 근거", "count_note"),
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
            f" · 대수 영향 +{int(summary.get('count_added') or 0):,}"
            f" / −{int(summary.get('count_removed') or 0):,}"
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
