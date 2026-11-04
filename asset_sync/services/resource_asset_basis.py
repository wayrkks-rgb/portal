"""통합서버 자원사용현황의 **VM 대수** 판정: vCenter 와 ITSM 이 함께 바뀌어야 움직인다.

vCenter 에서 VM 을 만들거나 지운 것만으로는 대수가 바뀌지 않는다.

- 추가: vCenter 에 VM 이 있고 ITSM 상태가 집계 상태(기본 운영 · 대기)가 된 날 +1
  (vCenter 생성이 먼저든 ITSM 등록이 먼저든, 둘이 다 갖춰진 날)
- 삭제: ITSM 이 미사용 · 매각/폐기(또는 ITSM 에서 사라짐)로 바뀐 날 -1.
  vCenter 에서만 지운 VM 은 ITSM 을 바꿀 때까지 **마지막 통합기에 '삭제 대기'로 남아 대수에 든다**
- 이동(vMotion): 늘 반영한다. 전체 대수는 그대로이고 통합기별 대수만 옮겨 간다

ITSM 스냅샷이 아예 없으면(처음 설치 · 시험 자료) 판정할 수 없으므로 예전처럼 vCenter
목록 그대로 센다. ``rvtools.resource_usage.asset_basis: false`` 로도 예전 방식으로 돌린다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Mapping

from ..normalization.code_maps import status_name
from . import asset_matching
from .asset_scope import AssetScope, basis_label, load_itsm_records, normalize_basis

LOGGER = logging.getLogger(__name__)

#: 기본 집계 상태. 운영 · 대기 VM 은 통합기 자원을 쓰고 있는 VM 이다.
DEFAULT_STATUS = ("CMSTA010", "CMSTA050")

COUNTED = "COUNTED"                # vCenter 에 있고 ITSM 집계 상태
NO_ITSM = "NO_ITSM"                # vCenter 에만 있음 (ITSM 미등록)
ITSM_OUT = "ITSM_OUT"              # vCenter 에 있으나 ITSM 상태가 집계 밖
REMOVE_PENDING = "REMOVE_PENDING"  # vCenter 에서 지워졌으나 ITSM 은 아직 집계 상태
GONE = "GONE"                      # vCenter 에서도 ITSM 집계에서도 빠짐

STATE_LABELS = {
    COUNTED: "자산",
    NO_ITSM: "ITSM 미등록 · 대수 제외",
    ITSM_OUT: "ITSM 집계 밖 · 대수 제외",
    REMOVE_PENDING: "vCenter 삭제 · ITSM 미반영 · 대수 유지",
    GONE: "삭제됨",
}

ADDED, REMOVED, MOVED = "ADDED", "REMOVED", "MOVED"
PENDING_ADD, PENDING_REMOVE = "PENDING_ADD", "PENDING_REMOVE"
EXCLUDED = "EXCLUDED"
MOVE_LABELS = {
    ADDED: "추가",
    REMOVED: "삭제",
    MOVED: "이동(vMotion)",
    PENDING_ADD: "추가 대기(ITSM 미반영)",
    PENDING_REMOVE: "삭제 대기(ITSM 미반영)",
    EXCLUDED: "제외(vCLS · 수동 제외 등)",
}
MOVE_ORDER = {ADDED: 0, REMOVED: 1, MOVED: 2, PENDING_ADD: 3, PENDING_REMOVE: 4, EXCLUDED: 5}


def vm_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    """VM 한 대. 자원사용현황 집계와 같은 열쇠(UUID → 자산키 → 이름)를 쓴다."""
    vcenter = str(row.get("vcenter_id") or "")
    for field in ("vm_uuid", "asset_key", "vm_name"):
        value = str(row.get(field) or "").strip()
        if value:
            return (vcenter, value)
    return (vcenter, "")


def _as_match_record(asset: Mapping[str, Any]) -> dict[str, Any]:
    raw = asset.get("raw") or {}
    return {
        "vm_uuid": None,
        "normalized_hostname": asset.get("normalized_hostname") or raw.get("CM_HOSTNAME"),
        "ip_addresses": asset.get("ip_addresses") or ([asset.get("primary_ip")] if asset.get("primary_ip") else []),
        "vm_name": raw.get("CM_NAME"),
    }


class ResourceAssetBasis:
    """하루치 VM 목록에 ITSM 판정을 붙이고, 두 날 사이 추가 · 삭제 · 이동을 가린다."""

    def __init__(self, config: Any, repository: Any, scope: AssetScope | None = None) -> None:
        self.config = config
        self.repo = repository
        self.scope = scope or AssetScope.load(config, repository)
        settings = (getattr(config, "rvtools", {}) or {}).get("resource_usage", {}) or {}
        self.enabled = str(settings.get("asset_basis", True)).strip().lower() not in {"false", "0", "off", "no"}
        try:
            self.statuses = normalize_basis(settings.get("asset_status") or list(DEFAULT_STATUS))
        except ValueError as exc:
            LOGGER.warning("resource_usage.asset_status 값이 잘못돼 기본값(운영 · 대기)을 씁니다: %s", exc)
            self.statuses = DEFAULT_STATUS
        self.lookback_days = max(1, int(settings.get("pending_removal_days") or 180))
        self._itsm: dict[int, dict[str, Any]] = {}
        self._rv: dict[int, dict[str, dict[str, Any]]] = {}
        self._identity = {str(v).lower(): k for k, v in repository.identity_maps().items()}
        self._members: dict[str, tuple[dict, dict]] = {}

    # ── 설명 ─────────────────────────────────────────────────────────────
    @property
    def label(self) -> str:
        return basis_label(self.statuses)

    def describe(self, applied: bool) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "applied": applied,
            "statuses": list(self.statuses),
            "label": self.label,
            "rule": (f"vCenter 에 VM 이 있고 ITSM 상태가 {self.label} 이면 VM 대수에 든다. vCenter 에서만 지운 VM 은"
                     " ITSM 이 바뀔 때까지 마지막 통합기에 남는다. vMotion 은 늘 반영한다."
                     if applied else "ITSM 과 견주지 않고 vCenter VM 목록 그대로 센다."),
        }

    def counts(self, status: Any) -> bool:
        return str(status or "").upper() in self.statuses

    # ── 밑자료 ───────────────────────────────────────────────────────────
    def itsm_on(self, day: str) -> dict[str, Any] | None:
        """그날까지 마지막 ITSM. 없으면 None (판정 불가 → vCenter 기준)."""
        if not self.enabled:
            return None
        snapshot = self.repo.snapshot_on_or_before("ITSM", str(day)[:10])
        if not snapshot:
            return None
        sid = int(snapshot["id"])
        if sid not in self._itsm:
            assets = {str(a.get("cm_id")): a for a in load_itsm_records(self.config, self.repo, sid)}
            self._itsm[sid] = {
                "snapshot": snapshot,
                "assets": assets,
                "index": asset_matching.build_index({k: _as_match_record(a) for k, a in assets.items()}),
            }
        return self._itsm[sid]

    def _rv_record(self, snapshot_id: Any, asset_key: Any, bulk: bool) -> dict[str, Any] | None:
        if not snapshot_id or not asset_key:
            return None
        sid = int(snapshot_id)
        if sid in self._rv:
            return self._rv[sid].get(str(asset_key))
        if bulk:
            self._rv[sid] = self.repo.load_rv_records(sid, with_raw=False)
            return self._rv[sid].get(str(asset_key))
        row = self.repo.conn.execute(
            "SELECT * FROM rv_asset_snapshot WHERE asset_key=? AND snapshot_id=?", (str(asset_key), sid)
        ).fetchone()
        return self.repo._row_to_record(row, False) if row else None

    def _match(self, row: Mapping[str, Any], rv: Mapping[str, Any] | None, itsm: dict[str, Any]) -> dict | None:
        assets = itsm["assets"]
        remembered = self._identity.get(str(row.get("vm_uuid") or "").lower())
        if remembered and remembered in assets:
            return assets[remembered]
        rv = rv or {}
        ips = rv.get("ip_addresses") or ([rv["primary_ip"]] if rv.get("primary_ip") else [])
        hostname = rv.get("normalized_hostname") or rv.get("dns_name")
        if not hostname and not ips and not rv:
            # 수집 스냅샷이 정리돼 VM 정보가 없을 때. VM 이름이 곧 호스트명인 경우가 많다.
            hostname = str(row.get("vm_name") or "").strip().lower() or None
        if not hostname and not ips:
            return None
        key = str(row.get("asset_key") or row.get("vm_name") or "")
        found = asset_matching.find(key, {"normalized_hostname": hostname, "ip_addresses": ips}, itsm["index"]).key
        return assets.get(found) if found else None

    def _entry(self, row: Mapping[str, Any], itsm: dict[str, Any], present: bool, bulk: bool) -> dict[str, Any]:
        rv = self._rv_record(row.get("vcenter_snapshot_id"), row.get("asset_key"), bulk)
        asset = self._match(row, rv, itsm)
        raw = (asset or {}).get("raw") or {}
        status = str((asset or {}).get("status_code") or raw.get("CM_STA_CD") or "")
        active = bool(asset) and self.counts(status)
        if present:
            state = COUNTED if active else (ITSM_OUT if asset else NO_ITSM)
        else:
            state = REMOVE_PENDING if active else GONE
        return {
            "identity": vm_identity(row),
            "vcenter_id": row.get("vcenter_id"),
            "cluster_name": row.get("cluster_name"),
            "esxi_host": row.get("esxi_host"),
            "vm_name": row.get("vm_name"),
            "vm_uuid": row.get("vm_uuid"),
            "asset_key": row.get("asset_key"),
            "power_state": row.get("power_state"),
            "allocated_cpu_cores": row.get("allocated_cpu_cores"),
            "allocated_memory_mb": row.get("allocated_memory_mb"),
            "last_seen": row.get("stat_date"),
            "present": present,
            "counted": state in (COUNTED, REMOVE_PENDING),
            "state": state,
            "state_label": STATE_LABELS[state],
            "cm_id": (asset or {}).get("cm_id"),
            "itsm_status": status or None,
            "itsm_status_label": status_name(status) if asset else "ITSM 자산 없음",
            "service_name": raw.get("CM_NAME"),
            "hostname": (asset or {}).get("normalized_hostname") or (rv or {}).get("normalized_hostname"),
            "primary_ip": (asset or {}).get("primary_ip") or (rv or {}).get("primary_ip"),
            "os": (asset or {}).get("os_family") or (rv or {}).get("os_family"),
        }

    # ── 하루치 판정 ──────────────────────────────────────────────────────
    def membership(self, day: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
        """그날 VM 마다 판정. (VM → 판정, 부가정보). ITSM 이 없으면 부가정보 applied=False."""
        day = str(day)[:10]
        if day in self._members:
            return self._members[day]
        itsm = self.itsm_on(day)
        conn = self.repo.conn
        vcenters = {str(r["vcenter_id"] or "") for r in conn.execute(
            "SELECT DISTINCT vcenter_id FROM host_resource_usage_daily WHERE stat_date=?", (day,)).fetchall()}
        meta = {"day": day, "applied": itsm is not None, "vcenters": vcenters,
                "itsm_as_of": (itsm or {}).get("snapshot", {}).get("snapshot_date") if itsm else None,
                # 그날 vCenter 에 있지만 자산에서 뺀 VM(vCLS · 템플릿 · 수동 제외). 대수 · 반영 대기와 무관.
                "excluded": {}}
        entries: dict[tuple[str, str], dict[str, Any]] = {}
        if itsm is None:
            self._members[day] = (entries, meta)
            return entries, meta
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM vm_resource_usage_daily WHERE stat_date=? AND inventory_status='CURRENT'", (day,)).fetchall()]
        for row in rows:
            decision = self.scope.decide_vcenter(row)
            if not decision.included or decision.reason:
                label = self.scope.reason_label(decision.reason)
                meta["excluded"][vm_identity(row)] = {
                    "identity": vm_identity(row), "vcenter_id": row.get("vcenter_id"),
                    "cluster_name": row.get("cluster_name"), "esxi_host": row.get("esxi_host"),
                    "vm_name": row.get("vm_name"), "vm_uuid": row.get("vm_uuid"), "asset_key": row.get("asset_key"),
                    "power_state": row.get("power_state"), "allocated_cpu_cores": row.get("allocated_cpu_cores"),
                    "allocated_memory_mb": row.get("allocated_memory_mb"), "last_seen": row.get("stat_date"),
                    "present": True, "counted": False, "state": "EXCLUDED",
                    "exclude_reason": decision.reason,
                    "exclude_label": f"{label}: {decision.note}" if decision.note else label,
                }
                continue
            entries[vm_identity(row)] = self._entry(row, itsm, True, True)

        # 그 전 기간에 있었는데 그날 없는 VM. ITSM 이 아직 집계 상태면 '삭제 대기' 로 대수에 남는다.
        since = (date.fromisoformat(day) - timedelta(days=self.lookback_days)).isoformat()
        last: dict[tuple[str, str], dict[str, Any]] = {}
        for cand in conn.execute(
            "SELECT vcenter_id, vm_uuid, asset_key, vm_name, MAX(stat_date) AS last_day FROM vm_resource_usage_daily"
            " WHERE stat_date>=? AND stat_date<? AND inventory_status='CURRENT'"
            " GROUP BY vcenter_id, vm_uuid, asset_key, vm_name", (since, day)).fetchall():
            cand = dict(cand)
            ident = vm_identity(cand)
            if ident in entries or ident in meta["excluded"] or str(cand.get("vcenter_id") or "") not in vcenters:
                continue   # 그날 있거나, 그날 그 vCenter 수집이 없어 사라졌는지 알 수 없다
            if ident not in last or str(cand["last_day"]) > str(last[ident]["last_day"]):
                last[ident] = cand
        for ident, cand in last.items():
            row = conn.execute(
                "SELECT * FROM vm_resource_usage_daily WHERE stat_date=? AND vcenter_id=? AND vm_name=?"
                " AND IFNULL(vm_uuid,'')=? ORDER BY id DESC LIMIT 1",
                (cand["last_day"], cand["vcenter_id"], cand["vm_name"], str(cand.get("vm_uuid") or "")),
            ).fetchone()
            if row is None or not self.scope.decide_vcenter(dict(row)).included:
                continue
            entries[ident] = self._entry(dict(row), itsm, False, False)
        self._members[day] = (entries, meta)
        return entries, meta

    # ── 두 날 사이 ───────────────────────────────────────────────────────
    def moves(self, early: str, late: str) -> dict[str, Any]:
        """두 날의 판정을 견주어 VM 추가 · 삭제 · 이동 · 반영 대기를 가린다."""
        m0, meta0 = self.membership(early)
        m1, meta1 = self.membership(late)
        applied = meta0["applied"] and meta1["applied"]
        rows: list[dict[str, Any]] = []
        unknown = 0
        if applied:
            for ident in sorted(set(m0) | set(m1)):
                e0, e1 = m0.get(ident), m1.get(ident)
                if e0 is None and str(e1["vcenter_id"] or "") not in meta0["vcenters"]:
                    unknown += 1          # 비교일에 그 vCenter 수집이 없었다
                    continue
                if e1 is None and str(e0["vcenter_id"] or "") not in meta1["vcenters"]:
                    unknown += 1
                    continue
                kind = self._kind(e0, e1)
                if kind:
                    rows.append(self._move_row(kind, e0, e1))
            # 두 날 사이 vCenter 에 새로 생긴 제외 대상(vCLS 등). 추가 대기로 두지 않고 [제외] 로 보인다.
            for ident, item in meta1["excluded"].items():
                if ident in m0 or ident in meta0["excluded"]:
                    continue
                if str(item["vcenter_id"] or "") not in meta0["vcenters"]:
                    continue
                rows.append(self._excluded_row(item))
        rows.sort(key=lambda r: (MOVE_ORDER[r["change"]], str(r.get("cluster_after") or r.get("cluster_before") or ""),
                                 str(r.get("vm_name") or "")))
        counts = {k: sum(1 for r in rows if r["change"] == k) for k in MOVE_LABELS}
        excluded_now: dict[str, int] = {}
        for item in meta1["excluded"].values():
            excluded_now[item["exclude_label"]] = excluded_now.get(item["exclude_label"], 0) + 1
        return {
            "applied": applied,
            "basis": self.describe(applied),
            "from": early, "to": late,
            "rows": rows,
            "counts": counts,
            "net": counts[ADDED] - counts[REMOVED],
            "unknown": unknown,
            # 기준일에 vCenter 에 있지만 자산에서 뺀 VM 수(사유별). 새로 생긴 것만 rows 에 [제외] 로 들어간다.
            "excluded_total": len(meta1["excluded"]),
            "excluded_by_reason": excluded_now,
            "by_host": self._by_host(rows),
        }

    @staticmethod
    def _excluded_row(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "change": EXCLUDED, "change_label": MOVE_LABELS[EXCLUDED], "count_effect": 0,
            "reason": f"vCenter 생성 · 자산에서 뺀 VM({item['exclude_label']}) — 대수 · ITSM 등록과 무관",
            "exclude_reason": item.get("exclude_reason"), "exclude_label": item.get("exclude_label"),
            "vm_name": item.get("vm_name"), "vm_uuid": item.get("vm_uuid"), "hostname": None, "primary_ip": None,
            "cm_id": None, "service_name": None, "os": None, "power_state": item.get("power_state"),
            "allocated_cpu_cores": item.get("allocated_cpu_cores"),
            "allocated_memory_gb": (round(int(item["allocated_memory_mb"]) / 1024, 1)
                                    if item.get("allocated_memory_mb") is not None else None),
            "vcenter_id": item.get("vcenter_id"),
            "cluster_before": None, "esxi_before": None,
            "cluster_after": item.get("cluster_name"), "esxi_after": item.get("esxi_host"),
            "vcenter_before": "없음", "vcenter_after": "있음 · 제외 대상",
            "itsm_before": "-", "itsm_after": "-", "last_seen": item.get("last_seen"),
        }

    @staticmethod
    def _kind(e0: dict | None, e1: dict | None) -> str | None:
        c0, c1 = bool(e0 and e0["counted"]), bool(e1 and e1["counted"])
        p0, p1 = bool(e0 and e0["present"]), bool(e1 and e1["present"])
        if c1 and not c0:
            return ADDED
        if c0 and not c1:
            return REMOVED
        if c0 and c1:
            if (e0["vcenter_id"], e0["esxi_host"]) != (e1["vcenter_id"], e1["esxi_host"]):
                return MOVED
            if p0 and not p1:
                return PENDING_REMOVE
            return None
        if p1 and not p0:
            return PENDING_ADD
        return None

    def _move_row(self, kind: str, e0: dict | None, e1: dict | None) -> dict[str, Any]:
        now = e1 or e0 or {}
        s0 = e0["itsm_status_label"] if e0 else "ITSM 자산 없음"
        s1 = e1["itsm_status_label"] if e1 else "ITSM 자산 없음"
        p0, p1 = bool(e0 and e0["present"]), bool(e1 and e1["present"])
        if kind == ADDED:
            if not p0 and p1:
                reason = f"vCenter 생성 + ITSM {s1}"
            elif p0 and p1:
                reason = (f"ITSM 에 {s1}(으)로 등록 (VM 은 그 전부터 vCenter 에 있었음)" if not (e0 or {}).get("cm_id")
                          else f"ITSM {s0} → {s1} (VM 은 그 전부터 vCenter 에 있었음)")
            else:
                reason = f"vCenter 에 생겼다가 지워짐 · ITSM 은 {s1}"
        elif kind == REMOVED:
            if not p1:
                reason = f"vCenter 삭제 + ITSM {s1}"
            elif not (e1 or {}).get("cm_id"):
                reason = "ITSM 에서 자산이 없어짐(삭제 · 짝 끊김) · VM 은 아직 vCenter 에 있음"
            else:
                reason = f"ITSM {s0} → {s1} (VM 은 아직 vCenter 에 있음 — 삭제할 VM 이면 vCenter 정리)"
        elif kind == MOVED:
            reason = "vMotion · 통합기만 옮김(전체 대수 그대로)"
        elif kind == PENDING_ADD:
            reason = (f"vCenter 생성 · ITSM {s1} — ITSM 을 {self.label} 로 반영하면 대수에 듭니다"
                      if e1 and e1.get("cm_id") else
                      "vCenter 생성 · ITSM 미등록 — ITSM 에 등록하면 대수에 듭니다")
        else:
            reason = f"vCenter 삭제 · ITSM 은 아직 {s1} — ITSM 을 미사용 · 폐기로 바꾸면 대수에서 빠집니다"
        return {
            "change": kind,
            "change_label": MOVE_LABELS[kind],
            "count_effect": {ADDED: 1, REMOVED: -1}.get(kind, 0),
            "reason": reason,
            "vm_name": now.get("vm_name"),
            "vm_uuid": now.get("vm_uuid"),
            "hostname": now.get("hostname") or (e0 or {}).get("hostname"),
            "primary_ip": now.get("primary_ip") or (e0 or {}).get("primary_ip"),
            "cm_id": now.get("cm_id") or (e0 or {}).get("cm_id"),
            "service_name": now.get("service_name") or (e0 or {}).get("service_name"),
            "os": now.get("os") or (e0 or {}).get("os"),
            "power_state": now.get("power_state"),
            "allocated_cpu_cores": now.get("allocated_cpu_cores"),
            "allocated_memory_gb": (round(int(now["allocated_memory_mb"]) / 1024, 1)
                                    if now.get("allocated_memory_mb") is not None else None),
            "vcenter_id": now.get("vcenter_id"),
            "cluster_before": (e0 or {}).get("cluster_name"), "esxi_before": (e0 or {}).get("esxi_host"),
            "cluster_after": (e1 or {}).get("cluster_name"), "esxi_after": (e1 or {}).get("esxi_host"),
            "vcenter_before": self._vc_state(e0), "vcenter_after": self._vc_state(e1),
            "itsm_before": s0, "itsm_after": s1,
            "last_seen": now.get("last_seen"),
        }

    @staticmethod
    def _vc_state(entry: dict | None) -> str:
        if not entry:
            return "없음"
        if not entry["present"]:
            return f"없음 (마지막 {entry.get('last_seen') or '-'})"
        power = str(entry.get("power_state") or "").lower()
        return "있음 · 켜짐" if power in {"poweredon", "on"} else ("있음 · 꺼짐" if power else "있음")

    @staticmethod
    def _by_host(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """통합기(ESXi)별 대수 변화. 이동은 나간 곳 -1, 들어온 곳 +1."""
        table: dict[tuple[str, str], dict[str, Any]] = {}

        def bucket(vc: Any, cluster: Any, esxi: Any) -> dict[str, Any]:
            key = (str(vc or ""), str(esxi or ""))
            if key not in table:
                table[key] = {"vcenter_id": vc, "cluster_name": cluster, "esxi_host": esxi, "added": 0,
                              "removed": 0, "moved_in": 0, "moved_out": 0, "pending_add": 0, "pending_remove": 0,
                              "excluded": 0}
            return table[key]

        for row in rows:
            vc = row.get("vcenter_id")
            if row["change"] == EXCLUDED:
                bucket(vc, row["cluster_after"], row["esxi_after"])["excluded"] += 1
            elif row["change"] in (ADDED, PENDING_ADD):
                bucket(vc, row["cluster_after"], row["esxi_after"])["added" if row["change"] == ADDED else "pending_add"] += 1
            elif row["change"] in (REMOVED, PENDING_REMOVE):
                side = ("cluster_before", "esxi_before") if row["change"] == REMOVED or row["esxi_before"] else ("cluster_after", "esxi_after")
                bucket(vc, row[side[0]], row[side[1]])["removed" if row["change"] == REMOVED else "pending_remove"] += 1
            else:
                bucket(vc, row["cluster_before"], row["esxi_before"])["moved_out"] += 1
                bucket(vc, row["cluster_after"], row["esxi_after"])["moved_in"] += 1
        result = list(table.values())
        for item in result:
            item["net"] = item["added"] - item["removed"] + item["moved_in"] - item["moved_out"]
        result.sort(key=lambda r: (str(r.get("cluster_name") or ""), str(r.get("esxi_host") or "")))
        return result
