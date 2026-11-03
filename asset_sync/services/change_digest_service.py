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

**생성 · 변경 · 삭제는 자산 대수 기준으로 가른다.** ITSM 상태가 미사용 → 운영처럼
집계 기준 안으로 들어오면 '변경' 이 아니라 **생성**, 운영 → 미사용 · 폐기처럼 밖으로
나가면 **삭제** 다. 이벤트 종류로 정하지 않고, 그 변경의 **전 · 후 값으로 자산
판정을 각각 돌려 보고** 정한다. 그래서 관리 화면에서 집계 기준(운영 / 운영+대기 …)을
바꾸면 지난 기록도 같은 기준으로 다시 읽힌다 -- 일 · 주 · 월이 늘 한 기준으로 움직인다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from ..normalization.code_maps import status_name
from . import asset_matching
from .asset_scope import AssetScope, load_itsm_records
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

#: vCenter 쪽 상태의 높낮이. 없음 < 꺼짐 < 켜짐. 기간의 처음보다 끝이 높으면 생성, 낮으면 삭제,
#: 같으면(꺼졌다 다시 켜짐 등) 서버 상태는 그대로이므로 변경이다.
VM_RANK = {"absent": 0, "off": 1, "on": 2}
VM_RANK_NAMES = {0: "없음", 1: "전원 꺼짐", 2: "전원 켜짐"}


def vm_step(row: dict[str, Any]) -> tuple[int, int] | None:
    """일간 한 줄의 vCenter 쪽 (전, 후). vCenter 생성 · 삭제 · 전원 변화가 없으면 None."""
    types = set(row.get("event_types") or [])
    if "RV_NEW" in types:
        now_off = str(row.get("power_state") or "").lower() == "poweredoff"
        return VM_RANK["absent"], VM_RANK["off"] if now_off else VM_RANK["on"]
    if "RV_REMOVED" in types:
        return VM_RANK["on"], VM_RANK["absent"]
    if "RV_POWER_ON" in types:
        return VM_RANK["off"], VM_RANK["on"]
    if "RV_POWER_OFF" in types:
        return VM_RANK["on"], VM_RANK["off"]
    return None


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

    def __init__(self, service: "ChangeDigestService", start: str = "") -> None:
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
        # 기간 첫날의 ITSM 에는 있었는데 지금은 지워진 자산. 지나간 날의 'VM 삭제' 줄이
        # ITSM 짝을 잃지 않게 한다. 지금 ITSM 만 보면, 레코드를 지운 뒤에 그날을 다시
        # 열었을 때 'ITSM 자산이 없어 대수 그대로' 라고 잘못 나온다.
        self.past_assets: dict[str, dict[str, Any]] = {}
        past = repo.snapshot_on_or_before("ITSM", str(start)[:10]) if start else None
        if past and snapshot and int(past["id"]) != int(snapshot["id"]):
            self.past_assets = {
                str(a.get("cm_id")): a
                for a in load_itsm_records(service.config, repo, int(past["id"]))
                if str(a.get("cm_id")) not in self.assets
            }
        self.past_index = asset_matching.build_index(
            {cm_id: service._as_match_record(a) for cm_id, a in self.past_assets.items()}
        ) if self.past_assets else None
        rv_snapshot = repo.latest_snapshot("RVTOOLS")
        self.vms = repo.load_rv_records(int(rv_snapshot["id"])) if rv_snapshot else {}
        # identity_map 은 cm_id → vm_uuid 로 들고 있다. 여기서는 그 반대가 필요하다.
        self.by_vm_uuid = {v: k for k, v in repo.identity_maps().items()}
        self.names = DisplayNameService(repo).resolver(service.config)
        #: 날짜 → {자산번호: 그날 ITSM 상태}. VM 이 사라진 날 ITSM 이 어땠는지 볼 때 쓴다.
        self.day_status: dict[str, dict[str, str]] = {}
        self.repo = repo
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
        fold: str = "day",
    ) -> dict[str, Any]:
        events = self.repo.changes(source, 100000, start, end)
        context = _Context(self, start)
        rows = self._fold(events, context, include_trivial=include_trivial)
        for row in rows:
            self._attach_identity(row, context)
            self._attach_reflection(row, context)
            self._attach_count_effect(row, context)
            for internal in ("_itsm_record", "_vm_record", "_payloads", "_itsm_changes", "_seen"):
                row.pop(internal, None)
        if fold == "period":
            # 주간처럼 여러 날을 볼 때는 서버 1대 = 1줄. 같은 서버가 [삭제](월) 와 [생성](화) 에
            # 둘 다 나오면 안 된다 -- 기간의 처음과 끝을 견주어 한 곳에만 둔다.
            rows = self.fold_period(rows)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["kind"]] += 1
        pending = [r for r in rows if r.get("reflection") == NOT_REFLECTED]
        effects = [int(r.get("count_effect") or 0) for r in rows]
        return {
            "period": {"start": start, "end": end},
            # 무엇을 자산으로 셌는지. 생성·삭제 판정이 이 기준을 따른다.
            "basis": self.scope.basis(),
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
                "missing_required": sum(1 for r in rows if r.get("missing")),
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
                    # ITSM 항목별 (변경 전 원값, 변경 후 원값). 자산 판정을 전·후로
                    # 다시 돌릴 때 쓴다. 화면용 표시값이 아니라 원래 값이어야 한다.
                    "_itsm_changes": {},
                    "_seen": set(),
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
            if source == "ITSM" and event.get("field_name"):
                self._remember_change(row["_itsm_changes"], event)
            field = self._field(event)
            if field is None:
                continue
            # 상태 변경은 상태 이벤트(ITSM_STATUS_TO_UNUSED 등)와 항목 이벤트
            # (CM_STA_CD) 로 두 번 쌓인다. 같은 항목 · 같은 값이면 한 번만 적는다.
            seen = (source, field["name"], str(event.get("old_value")), str(event.get("new_value")))
            if seen in row["_seen"]:
                continue
            row["_seen"].add(seen)
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
    def _remember_change(changes: dict[str, list[Any]], event: dict[str, Any]) -> None:
        """항목 하나의 그날 첫 '전' 값과 마지막 '후' 값. 이벤트는 최신순으로 온다."""
        name = str(event.get("field_name"))
        stamp = str(event.get("detected_at") or "")
        held = changes.get(name)
        if held is None:
            changes[name] = [event.get("old_value"), event.get("new_value"), stamp, stamp]
            return
        if stamp < held[2]:
            held[0], held[2] = event.get("old_value"), stamp
        if stamp > held[3]:
            held[1], held[3] = event.get("new_value"), stamp

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
        if row.get("summary_prefix"):
            parts.append(row["summary_prefix"])
        elif row["kind"] in {CREATED, REMOVED}:
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
        if not asset and row["server_key"].startswith("ITSM:"):
            # 그날 이후 ITSM 에서 지워진 자산. 기간 첫날 스냅샷의 값으로 보여준다.
            asset = context.past_assets.get(row["server_key"][5:]) or {}
            if asset:
                row["itsm_cm_id"] = row.get("itsm_cm_id") or row["server_key"][5:]
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
            "itsm_status_label": (status_name(asset.get("status_code") or raw.get("CM_STA_CD"))
                                  if (asset or raw) else None),
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
            # EOSL 조사에 필요한 ITSM 값이 빈 것. 일 · 주 · 월이 같은 판정을 쓴다.
            "missing": self.scope.describe_itsm(asset)["missing"] if raw else [],
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
        found = asset_matching.find(vm_key, probe, context.index).key
        if found or context.past_index is None:
            return found
        return asset_matching.find(vm_key, probe, context.past_index).key

    def _removed_vm_note(self, row: dict[str, Any], context: _Context, basis: str) -> str:
        """VM 이 사라진 날의 대수 설명. 대수는 늘 ITSM 쪽이 바뀐 날 움직인다."""
        cm_id = str(row.get("itsm_cm_id") or "")
        if not cm_id:
            return "ITSM 자산이 없어 서버 대수는 그대로입니다."
        current = context.assets.get(cm_id)
        if current is None:
            return (f"ITSM 자산({cm_id})과 짝이었고, 지금은 ITSM 에서도 지워졌습니다. 서버 대수는"
                    " 이날이 아니라 ITSM 에서 지운 날 반영됩니다.")
        now_code = current.get("status_code")
        if not self.scope.counts_status(now_code):
            then_code = self._status_on(context, str(row.get("day") or ""), cm_id)
            now_text = f"지금 ITSM 상태는 {status_name(now_code)}입니다"
            if then_code and not self.scope.counts_status(then_code):
                return (f"이날도 ITSM 상태가 {status_name(then_code)}여서 집계 기준({basis}) 밖이었습니다."
                        f" 서버 대수와 무관합니다({now_text}).")
            if then_code:
                return (f"이날 ITSM 은 {status_name(then_code)}(집계 기준 안)였고 {now_text}."
                        " 대수는 이날이 아니라 ITSM 상태를 바꾼 날 하나 줄었습니다.")
            return (f"{now_text}(집계 기준 {basis} 밖). 대수는 이날이 아니라 ITSM 상태가 바뀐 날"
                    " 반영됩니다.")
        return (f"vCenter 에서만 사라졌습니다. ITSM 은 아직 {status_name(now_code)}라, ITSM 상태를 바꾸는 날"
                " 서버 대수가 줄어듭니다.")

    def _status_on(self, context: _Context, day: str, cm_id: str) -> str:
        """그날(그날까지 마지막) ITSM 스냅샷에서의 상태 코드. 없으면 빈 문자열."""
        if not day:
            return ""
        if day not in context.day_status:
            snapshot = context.repo.snapshot_on_or_before("ITSM", day[:10])
            rows = context.repo.conn.execute(
                "SELECT cm_id, status_code FROM itsm_asset_snapshot WHERE snapshot_id=?",
                (int(snapshot["id"]),),
            ).fetchall() if snapshot else []
            context.day_status[day] = {str(r["cm_id"]): str(r["status_code"] or "") for r in rows}
        return context.day_status[day].get(cm_id, "")

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
        basis = self.scope.basis_label

        if "RV_NEW" in types:
            if asset is None:
                return (NOT_REFLECTED, "vCenter 에 새로 생긴 VM 인데 ITSM 에 자산이 없습니다.",
                        "ITSM 에 자산 등록")
            if not self.scope.counts_status(status):
                return (NOT_REFLECTED,
                        f"ITSM 자산은 있으나 상태가 {status_name(status)}라 집계 기준({basis}) 밖입니다.",
                        "ITSM 상태를 운영으로")
            return (REFLECTED, f"ITSM 에 {status_name(status)} 상태로 있습니다(집계 기준 {basis} 안).", "")

        if "RV_REMOVED" in types:
            if asset is None:
                return (REFLECTED, "ITSM 에도 없습니다.", "")
            if self.scope.counts_status(status):
                return (NOT_REFLECTED,
                        f"vCenter 에서 사라졌는데 ITSM 은 아직 {status_name(status)}입니다(집계 기준 {basis} 안).",
                        "ITSM 상태를 미사용·폐기로")
            return (REFLECTED, f"ITSM 상태가 이미 {status_name(status)}입니다(집계 기준 {basis} 밖).", "")

        if "RV_POWER_OFF" in types:
            if asset is None:
                return (NO_ASSET, "전원이 꺼진 VM 의 ITSM 자산을 찾지 못했습니다.", "정합성 화면에서 짝을 지어 주세요")
            if self.scope.counts_status(status):
                return (NOT_REFLECTED,
                        f"전원이 꺼졌는데 ITSM 은 아직 {status_name(status)}입니다(집계 기준 {basis} 안).",
                        "계속 끌 서버면 ITSM 상태를 미사용으로")
            return (REFLECTED, f"ITSM 상태가 이미 {status_name(status)}입니다(집계 기준 {basis} 밖).", "")

        if "RV_POWER_ON" in types:
            if asset is None:
                return (NOT_REFLECTED, "전원이 켜진 VM 인데 ITSM 에 자산이 없습니다.", "ITSM 에 자산 등록")
            if not self.scope.counts_status(status):
                return (NOT_REFLECTED,
                        f"전원이 켜졌는데 ITSM 은 {status_name(status)}라 집계 기준({basis}) 밖입니다.",
                        "ITSM 상태를 운영으로")
            return (REFLECTED, f"ITSM 에 {status_name(status)} 상태로 있습니다(집계 기준 {basis} 안).", "")

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

    # ── 자산 대수를 움직이나 · 생성/변경/삭제 ─────────────────────────────
    def _attach_count_effect(self, row: dict[str, Any], context: _Context) -> None:
        """이 변경이 **서버 대수**를 +1 / -1 / 그대로 중 무엇으로 만드는가, 그리고
        그에 따라 이 줄을 생성 · 변경 · 삭제 중 어디에 둘 것인가.

        ITSM 쪽은 이벤트 종류로 정하지 않는다. 변경 **전 값**과 **후 값**으로 자산
        판정(상태 · 빈 값 규칙 · 수동 제외)을 각각 돌려 보고, 밖 → 안 이면 생성,
        안 → 밖 이면 삭제, 그대로면 변경이다. 미사용 → 운영이 '변경' 으로 섞여
        월간 대수와 어긋나던 것을 이렇게 바로잡는다. 집계 기준을 바꾸면 지난
        기록도 새 기준으로 다시 읽힌다.

        vCenter 쪽 신규 · 삭제는 그대로 생성 · 삭제에 두되, ITSM 기준 대수는 움직이지
        않는다(ITSM 에 반영돼야 잡힌다). 그 사실을 대수 근거에 적는다.
        """
        types = set(row.get("event_types") or [])
        payloads = row.get("_payloads") or {}
        basis = self.scope.basis_label
        before, after, base_known = self._itsm_states(row, context)
        effect, note, prefix = 0, "", ""

        if "ITSM_ASSET_CREATED" in types:
            counted, reason = self._counted(row, after)
            if counted:
                effect, note = 1, f"ITSM 에 집계 기준({basis}) 자산으로 새로 들어와 서버 대수에 더해집니다."
            else:
                note = f"ITSM 에 새로 등록됐지만 자산으로 세지 않습니다({reason}). 서버 대수는 그대로입니다."
                prefix = "ITSM 신규 등록 · 집계 밖"
        elif "ITSM_RECORD_REMOVED" in types:
            counted, reason = self._counted(row, before)
            if counted:
                effect, note = -1, "ITSM 에서 레코드가 지워져 서버 대수에서 하나 줄어듭니다."
            else:
                note = f"원래 자산으로 세지 않던 대상이 ITSM 에서 지워졌습니다({reason})."
                prefix = "ITSM 레코드 삭제 · 집계 밖"
        elif row.get("_itsm_changes"):
            was, why_was = self._counted(row, before)
            now, why_now = self._counted(row, after)
            if not base_known:
                # 지금 스냅샷에 자산이 없어 다른 항목을 모른다. 상태만으로 본다.
                was = self.scope.counts_status(before.get("CM_STA_CD"))
                now = self.scope.counts_status(after.get("CM_STA_CD"))
            if now and not was:
                effect = 1
                note = (f"집계 기준({basis}) 안으로 들어와 서버 대수에 더해집니다"
                        f"{self._status_move(before, after)}.")
            elif was and not now:
                effect = -1
                note = (f"집계 기준({basis}) 밖으로 나가 서버 대수에서 하나 줄어듭니다"
                        f"{self._status_move(before, after)}.")
            elif not now:
                note = f"자산으로 세지 않는 대상이라 서버 대수는 그대로입니다({why_now})."
            else:
                note = "내용만 바뀌어 서버 대수는 그대로입니다."
        elif "RV_NEW" in types:
            # 짝이 되는 ITSM 자산이 '지금' 있어도, 그 자산이 이날 있었다는 뜻은 아니다
            # (며칠 뒤 등록했을 수 있다). 대수는 ITSM 쪽 등록·운영 전환일에 잡힌다.
            note = (f"ITSM 자산({row['itsm_cm_id']}, 지금 {status_name(row.get('itsm_status'))})과 짝이 맞습니다."
                    " 운영 대수는 VM 이 생긴 날이 아니라 ITSM 에 등록 · 운영 전환된 날 잡힙니다."
                    if row.get("itsm_cm_id")
                    else "ITSM 에 자산이 없어 서버 대수에 들어가지 않습니다. 등록하는 날 대수에 잡힙니다.")
        elif "RV_REMOVED" in types:
            note = self._removed_vm_note(row, context, basis)
        elif "RV_POWER_OFF" in types or "RV_POWER_ON" in types:
            word = "꺼졌습니다" if "RV_POWER_OFF" in types else "켜졌습니다"
            itsm = (f"ITSM 은 지금 {status_name(row.get('itsm_status'))}" if row.get("itsm_cm_id")
                    else "ITSM 에 자산이 없습니다")
            note = (f"vCenter 에서 전원이 {word}. 서버 대수는 ITSM 상태가 바뀌어야 움직이므로 그대로입니다"
                    f"({itsm}).")
        else:
            note = "내용만 바뀌어 서버 대수는 그대로입니다."

        # 생성 · 변경 · 삭제. ITSM 이 대수를 움직였으면 그것이 먼저다.
        if effect > 0:
            kind = CREATED
        elif effect < 0:
            kind = REMOVED
        elif "RV_NEW" in types or "RV_POWER_ON" in types:
            # vCenter 쪽 생성 · 삭제(전원 포함)는 생성 · 삭제로 보인다. 대수는 ITSM 이 바뀌어야 움직인다.
            kind = CREATED
        elif "RV_REMOVED" in types or "RV_POWER_OFF" in types:
            kind = REMOVED
        else:
            kind = CHANGED
        row["kind"] = kind
        row["kind_label"] = KIND_LABELS[kind]
        row["summary_prefix"] = prefix
        row["summary"] = self._summary(row)
        row["count_effect"] = effect
        row["count_effect_label"] = {1: "자산 +1", -1: "자산 −1"}.get(effect, "대수 변화 없음")
        row["count_note"] = note

    def _itsm_states(
        self, row: dict[str, Any], context: _Context
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """ITSM 원본의 변경 전 · 후 모습. 지금 값 위에 그날의 전 · 후 값을 얹는다.

        지난 날짜를 볼 때도 그날 바뀐 항목은 그날 값으로 판정해야 한다. 지금 값만
        쓰면 그 뒤에 또 바뀐 상태로 과거를 판정하게 된다.
        """
        payloads = row.get("_payloads") or {}
        asset = context.assets.get(str(row.get("itsm_cm_id") or "")) or {}
        base = dict(asset.get("raw") or payloads.get("ITSM") or {})
        if asset and not base.get("CM_STA_CD") and asset.get("status_code"):
            # 원본에 상태가 없고 정규화된 값에만 있는 경우가 있다. 판정은 상태가 핵심이다.
            base["CM_STA_CD"] = asset["status_code"]
        base_known = bool(asset)
        before, after = dict(base), dict(base)
        # 이벤트에 남은 원본이 그날의 모습이다. 빠진 항목은 지금 값으로 채운다.
        if "ITSM_ASSET_CREATED" in set(row.get("event_types") or []) and payloads.get("ITSM"):
            after.update(payloads["ITSM"])               # 등록되던 날의 모습
        if "ITSM_RECORD_REMOVED" in set(row.get("event_types") or []) and payloads.get("ITSM"):
            before.update(payloads["ITSM"])              # 지워지기 직전의 모습
        for name, (old, new, _, _) in (row.get("_itsm_changes") or {}).items():
            before[name] = old
            after[name] = new
        return before, after, base_known

    def _counted(self, row: dict[str, Any], raw: dict[str, Any]) -> tuple[bool, str]:
        """이 원본 모습이면 자산으로 세는가, 안 세면 왜인가."""
        record = {
            "cm_id": row.get("itsm_cm_id") or raw.get("CM_ID"),
            "status_code": raw.get("CM_STA_CD"),
            "raw": raw,
        }
        decision = self.scope.decide_itsm(record)
        # '제외 포함 보기' 에서는 included 가 늘 참이다. 사유로 판단한다.
        counted = bool(decision.included and not decision.reason)
        return counted, (self.scope.reason_label(decision.reason, raw.get("CM_STA_CD"))
                         if decision.reason else "")

    @staticmethod
    def _status_move(before: dict[str, Any], after: dict[str, Any]) -> str:
        old, new = before.get("CM_STA_CD"), after.get("CM_STA_CD")
        if not old or not new or old == new:
            return ""
        return f" (상태 {status_name(old)} → {status_name(new)})"

    @staticmethod
    def fold_period(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """하루 단위 줄을 서버 단위로 접는다. 성격은 기간의 순변화로 정한다.

        - 대수(ITSM) 순변화가 +면 생성, -면 삭제
        - 대수 변화가 없으면 vCenter 쪽 순변화(없음 < 꺼짐 < 켜짐)로 생성 · 삭제
        - 둘 다 처음과 끝이 같으면(꺼졌다 다시 켜짐, 미사용 갔다 운영 복귀) **변경**
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for row in sorted(rows, key=lambda r: (str(r.get("day") or ""), str(r.get("detected_at") or ""))):
            key = str(row.get("server_key"))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(row)
        folded: list[dict[str, Any]] = []
        for key in order:
            items = groups[key]
            if len(items) == 1:
                folded.append({**items[0], "days": [items[0].get("day")]})
                continue
            last = items[-1]
            effect = sum(int(r.get("count_effect") or 0) for r in items)
            steps = [step for step in (vm_step(r) for r in items) if step]
            vm_net = (steps[-1][1] - steps[0][0]) if steps else 0
            if effect > 0 or (effect == 0 and vm_net > 0):
                kind = CREATED
            elif effect < 0 or (effect == 0 and vm_net < 0):
                kind = REMOVED
            else:
                kind = CHANGED
            path = " → ".join(f"{str(r.get('day') or '')[5:]} {r.get('kind_label')}" for r in items)
            moved = any(r.get("kind") in (CREATED, REMOVED) for r in items)
            if kind == CHANGED and moved:
                vm_text = (f" · vCenter {VM_RANK_NAMES[steps[0][0]]}에서 시작해 {VM_RANK_NAMES[steps[-1][1]]}로 끝남"
                           if steps else "")
                note = (f"기간 중 {path}{vm_text}. 처음과 끝이 같아 서버 상태는 그대로이므로 생성 · 삭제가 아니라"
                        " 변경으로 둡니다.")
            else:
                note = str(last.get("count_note") or "")
            fields: list[dict[str, Any]] = []
            for r in items:
                for field in r.get("fields") or []:
                    fields.append({**field, "label": f"{str(r.get('day') or '')[5:]} {field.get('label')}"})
            with_reflection = next((r for r in reversed(items) if r.get("reflection")), last)
            merged = {
                **last,
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "fields": fields,
                "event_types": sorted({t for r in items for t in r.get("event_types") or []}),
                "sources": sorted({src for r in items for src in r.get("sources") or []}),
                "count_effect": effect,
                "count_effect_label": {1: "자산 +1", -1: "자산 −1"}.get(effect, "대수 변화 없음"),
                "count_note": note,
                "summary": " ; ".join(f"{str(r.get('day') or '')[5:]} {r.get('summary')}" for r in items),
                "days": [r.get("day") for r in items],
                "trivial_count": sum(int(r.get("trivial_count") or 0) for r in items),
                "reflection": with_reflection.get("reflection"),
                "reflection_label": with_reflection.get("reflection_label"),
                "reflection_note": with_reflection.get("reflection_note"),
                "itsm_action": with_reflection.get("itsm_action"),
            }
            folded.append(merged)
        folded.sort(key=lambda r: str(r.get("detected_at") or ""), reverse=True)
        return folded

    # ── 엑셀 ────────────────────────────────────────────────────────────
    EXPORT_COLUMNS = (
        ("일시", "detected_at"), ("구분", "kind_label"), ("서버", "label"),
        ("호스트명", "hostname"), ("IP", "primary_ip"),
        ("ITSM 자산번호", "itsm_cm_id"), ("ITSM 상태", "itsm_status_label"), ("필수 정보 누락", "missing"),
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
            sheet.append([
                ", ".join(map(str, value)) if isinstance(value, list) else value
                for value in (row.get(key) for _, key in cls.EXPORT_COLUMNS)
            ])
        sheet.freeze_panes = sheet.cell(head + 1, 1).coordinate
        last = sheet.cell(head, len(cls.EXPORT_COLUMNS)).column_letter
        sheet.auto_filter.ref = f"A{head}:{last}{sheet.max_row}"
        for column in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in column)
            sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 48)
        workbook.save(path)
        workbook.close()
        return path
