"""대수 감사: 화면마다 나온 숫자를 한 자리에 모아 어디서 갈라지는지 밝힌다.

"대수가 안 맞는다" 는 말에는 두 가지가 섞여 있다.

1. **같아야 하는데 다른 것** — 서버 현황 표의 계와 EOSL 표의 전체 수량은 같은
   대상을 세므로 반드시 같아야 한다. 다르면 버그다.
2. **달라야 정상인 것** — ITSM 자산 대수와 vCenter VM 대수는 애초에 다른 것을
   센다. 통합기를 새로 붙이면 vCenter 쪽이 먼저 늘고 ITSM 은 등록될 때까지
   안 늘어난다. 자원사용률은 07시 배치가 돌아야 반영되므로 그 사이에는 새
   통합기의 VM 이 통합서버 자원사용현황에서 빠져 있다.

표만 보면 둘을 가릴 수 없다. 그래서 각 숫자가 **어느 스냅샷의 어느 시점**에서
나왔는지까지 같이 적고, 같아야 하는 쌍은 직접 견주어 결과를 낸다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .asset_scope import AssetScope, load_itsm_records
from .resource_usage_service import VMResourceUsageExportService
from .server_status_service import ServerStatusService

#: 판정. OK = 같아야 하는 것이 같다, MISMATCH = 같아야 하는데 다르다,
#: INFO = 달라도 되는 것(이유를 적는다), STALE = 자료 시점이 어긋났다.
OK = "OK"
MISMATCH = "MISMATCH"
INFO = "INFO"
STALE = "STALE"


class CountAuditService:
    """월간 점검 기준일로 모든 화면의 대수를 다시 세어 견준다."""

    def __init__(self, config: Any, repository: Any, *, include_all: bool = False) -> None:
        self.config = config
        self.repo = repository
        self.include_all = include_all

    def audit(self, base_day: date) -> dict[str, Any]:
        sources = self._sources(base_day)
        counts = self._counts(base_day, sources)
        checks = self._checks(sources, counts)
        worst = (
            MISMATCH if any(c["verdict"] == MISMATCH for c in checks)
            else STALE if any(c["verdict"] == STALE for c in checks)
            else OK
        )
        return {
            "base_day": base_day.isoformat(),
            "verdict": worst,
            "sources": sources,
            "counts": counts,
            "checks": checks,
        }

    # ── 자료가 어디서 왔나 ──────────────────────────────────────────────
    def _sources(self, base_day: date) -> dict[str, Any]:
        """각 숫자의 출처. 기준일이 다르면 대수가 다른 것이 당연하다."""
        itsm = self.repo.snapshot_on_or_before("ITSM", base_day.isoformat())
        previous_day = base_day.replace(day=1) - timedelta(days=1)
        previous = self.repo.snapshot_on_or_before("ITSM", previous_day.isoformat())
        rv = self.repo.snapshot_on_or_before("RVTOOLS", base_day.isoformat())
        latest_rv = self.repo.latest_snapshot("RVTOOLS")
        run = self.repo.conn.execute(
            "SELECT * FROM resource_usage_run WHERE status<>'FAILED'"
            " ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        batch = self.repo.latest_daily_batch()
        result: dict[str, Any] = {
            "itsm": self._snapshot_info(itsm),
            "itsm_previous": self._snapshot_info(previous),
            "vcenter": self._snapshot_info(rv),
            "vcenter_latest": self._snapshot_info(latest_rv),
            "resource_usage": None,
            "reconciliation": None,
            "batch": None,
        }
        if run:
            row = dict(run)
            result["resource_usage"] = {
                "run_id": int(row["id"]),
                "status": row.get("status"),
                "period_start": row.get("period_start"),
                "period_end": row.get("period_end"),
                "started_at": row.get("started_at"),
                # 어느 vCenter 스냅샷을 보고 VM 을 확정했는가. 이것이 최신이 아니면
                # 새로 붙인 통합기가 자원사용현황에 아직 안 들어와 있다.
                "vcenter_snapshot_id": row.get("vcenter_snapshot_id"),
                "host_count": row.get("host_count"),
                "vm_count": row.get("vm_count"),
                "failed_scopes": self._json_list(row.get("failed_scope_json")),
                "success_scopes": self._json_list(row.get("success_scope_json")),
            }
        # 정합성이 어느 스냅샷 쌍을 보고 돌았나. 통합기를 붙인 뒤 수집만 하고
        # 정합성을 다시 돌리지 않으면 옛 쌍의 결과가 화면에 남는다.
        pair = self.repo.conn.execute(
            "SELECT itsm_snapshot_id, rv_snapshot_id, MAX(created_at) AS created_at,"
            " COUNT(*) AS cnt FROM reconciliation_result"
            " GROUP BY itsm_snapshot_id, rv_snapshot_id ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if pair:
            row = dict(pair)
            result["reconciliation"] = {
                "itsm_snapshot_id": row.get("itsm_snapshot_id"),
                "vcenter_snapshot_id": row.get("rv_snapshot_id"),
                "created_at": row.get("created_at"),
                "result_count": int(row.get("cnt") or 0),
            }
        if batch:
            result["batch"] = {
                "status": batch.get("status"),
                "started_at": batch.get("started_at"),
                "ended_at": batch.get("ended_at"),
            }
        return result

    @staticmethod
    def _snapshot_info(row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        return {
            "id": int(item["id"]),
            "snapshot_date": item.get("snapshot_date"),
            "collected_at": item.get("collected_at"),
            "status": item.get("status"),
            "record_count": int(item.get("record_count") or 0),
        }

    @staticmethod
    def _json_list(text: Any) -> list[str]:
        try:
            value = json.loads(text or "[]")
        except (TypeError, ValueError):
            return []
        if isinstance(value, dict):
            return sorted(str(key) for key in value)
        return [str(item) for item in value] if isinstance(value, list) else []

    # ── 다시 세기 ───────────────────────────────────────────────────────
    def _counts(self, base_day: date, sources: dict[str, Any]) -> dict[str, Any]:
        """화면이 쓰는 그 함수로 다시 센다. 따로 세면 감사가 의미 없다."""
        counts: dict[str, Any] = {"itsm": None, "vcenter": None, "resource_usage": None}
        itsm = sources.get("itsm")
        if itsm:
            service = ServerStatusService(self.config, self.repo, include_all=self.include_all)
            previous = sources.get("itsm_previous")
            status = service.status(itsm["id"], previous["id"] if previous else None)
            eosl = service.eosl(itsm["id"], today=base_day)
            counts["itsm"] = {
                "snapshot_total": int(itsm["record_count"]),
                "loaded": len(service.load_records(itsm["id"])),
                "selected": int(status["counts"]["selected"]),
                "excluded": int(status["counts"]["excluded"]),
                "physical": int(status["counts"]["physical"]),
                # 표의 '계 · 소계' 칸. 화면에 그려지는 바로 그 숫자다.
                "table_all": int(status["all"]["table"]["rows"]["계"]["소계"]),
                "table_physical": int(status["physical"]["table"]["rows"]["계"]["소계"]),
                "eosl_all": int(eosl["all"]["total"]),
                "eosl_physical": int(eosl["physical"]["total"]),
                "by_reason": status["scope"].get("by_reason", {}),
            }
        rv = sources.get("vcenter")
        if rv:
            scope = AssetScope.load(self.config, self.repo, include_all=self.include_all)
            records = list(self.repo.load_rv_records(rv["id"]).values())
            summary = scope.vcenter_summary(records)
            counts["vcenter"] = {
                "snapshot_total": int(rv["record_count"]),
                "loaded": len(records),
                "selected": int(summary.get("selected", 0)),
                "excluded": int(summary.get("excluded", 0)),
                "by_reason": summary.get("by_reason", {}),
                "scopes": sorted({str(r.get("vcenter") or "") for r in records if r.get("vcenter")}),
            }
        run = sources.get("resource_usage")
        if run and run.get("period_end"):
            usage = VMResourceUsageExportService(self.config, self.repo).summary(
                str(run["period_start"] or run["period_end"]), str(run["period_end"])
            )
            hosts = usage.get("hosts") or []
            counts["resource_usage"] = {
                "host_rows": len(hosts),
                # 통합기 표가 더해 보여주는 VM 대수와 아래 VM 목록 줄 수.
                "host_vm_total": sum(int(h.get("vm_count") or 0) for h in hosts),
                "vm_rows": len(usage.get("vms") or []),
                "cluster_rows": len(usage.get("clusters") or []),
                "datastore_rows": len(usage.get("datastores") or []),
                "excluded_rows": int((usage.get("scope") or {}).get("excluded_rows", 0)),
                "scopes": sorted({str(h.get("vcenter_id") or "") for h in hosts if h.get("vcenter_id")}),
                "vms_without_host": sum(
                    1 for vm in (usage.get("vms") or [])
                    if not vm.get("esxi_host") and str(vm.get("inventory_status") or "CURRENT") == "CURRENT"
                ),
            }
        return counts

    # ── 견주기 ──────────────────────────────────────────────────────────
    def _checks(self, sources: dict[str, Any], counts: dict[str, Any]) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        def add(name: str, verdict: str, message: str, **extra: Any) -> None:
            checks.append({"name": name, "verdict": verdict, "message": message, **extra})

        itsm = counts.get("itsm")
        if not itsm:
            add("ITSM 스냅샷", STALE, "기준일까지의 ITSM 스냅샷이 없습니다. 수집을 먼저 실행하세요.")
        else:
            self._same(add, "서버 현황 표 = 자산 대수",
                       itsm["table_all"], itsm["selected"],
                       "서버 현황 전체 표의 계와 자산 대수")
            self._same(add, "서버 현황 물리 표 = 물리 대수",
                       itsm["table_physical"], itsm["physical"],
                       "서버 현황 물리 표의 계와 물리서버 대수")
            self._same(add, "EOSL 전체 = 자산 대수",
                       itsm["eosl_all"], itsm["selected"],
                       "EOSL OS 행의 전체 수량과 자산 대수")
            self._same(add, "EOSL 서버 = 물리 대수",
                       itsm["eosl_physical"], itsm["physical"],
                       "EOSL 서버 행의 전체 수량과 물리서버 대수")
            self._same(add, "자산 + 제외 = ITSM 전체",
                       itsm["selected"] + itsm["excluded"], itsm["loaded"],
                       "자산 대수 + 제외 대수와 ITSM 스냅샷 건수")

        usage = counts.get("resource_usage")
        if usage:
            self._same(add, "통합기 표 VM 합 = VM 목록 줄 수",
                       usage["host_vm_total"] + usage["vms_without_host"], usage["vm_rows"],
                       "통합기별 VM 대수의 합과 VM 목록 줄 수")
            if usage["vms_without_host"]:
                add("소속 통합기 없는 VM", INFO,
                    f"ESXi 가 비어 있는 VM {usage['vms_without_host']:,}대가 있습니다."
                    " 통합기 표의 합에는 들어가지 않습니다(전원이 꺼진 채 등록만 된 VM 등).",
                    count=usage["vms_without_host"])

        # 출처가 서로 다른 시점이면 대수가 다른 것이 당연하다. 그걸 먼저 알려야
        # '버그' 를 찾아 헤매지 않는다.
        itsm_src, rv_src = sources.get("itsm"), sources.get("vcenter")
        if itsm_src and rv_src and itsm_src["snapshot_date"] != rv_src["snapshot_date"]:
            add("ITSM · vCenter 기준일", STALE,
                f"ITSM 은 {itsm_src['snapshot_date']}, vCenter 는 {rv_src['snapshot_date']} 자료입니다."
                " 기준일이 다르면 대수가 다른 것이 당연합니다.")

        run = sources.get("resource_usage")
        latest_rv = sources.get("vcenter_latest")
        if run and latest_rv:
            used = run.get("vcenter_snapshot_id")
            if used and int(used) != int(latest_rv["id"]):
                add("자원사용률 기준 스냅샷", STALE,
                    "통합서버 자원사용현황은 최신 vCenter 스냅샷이 아니라 이전 스냅샷을 보고 있습니다."
                    " 통합기를 새로 붙였다면 07시 배치가 한 번 돌기 전까지는 그 통합기의 VM 이"
                    " 자원사용현황에 나오지 않습니다.",
                    used_snapshot_id=int(used), latest_snapshot_id=int(latest_rv["id"]))
        if run and run.get("failed_scopes"):
            add("수집 실패 통합기", STALE,
                f"수집에 실패한 통합기 {len(run['failed_scopes'])}개가 있습니다:"
                f" {', '.join(run['failed_scopes'])}. 그 통합기의 VM 은 대수에서 빠집니다.",
                scopes=run["failed_scopes"])

        recon = sources.get("reconciliation")
        latest_itsm = sources.get("itsm")
        if recon and latest_rv and latest_itsm:
            stale_rv = recon.get("vcenter_snapshot_id") and int(recon["vcenter_snapshot_id"]) != int(latest_rv["id"])
            stale_itsm = recon.get("itsm_snapshot_id") and int(recon["itsm_snapshot_id"]) != int(latest_itsm["id"])
            if stale_rv or stale_itsm:
                add("정합성 기준 스냅샷", STALE,
                    "정합성 결과가 최신 스냅샷이 아닌 옛 스냅샷 쌍으로 만들어졌습니다."
                    " 통합기를 붙인 뒤 수집만 하고 정합성을 다시 돌리지 않으면 이렇게 됩니다."
                    " [정합성 실행] 을 한 번 누르거나 07시 배치를 기다리세요.",
                    reconciliation_pair=[recon.get("itsm_snapshot_id"), recon.get("vcenter_snapshot_id")],
                    latest_pair=[int(latest_itsm["id"]), int(latest_rv["id"])])
            else:
                add("정합성 기준 스냅샷", OK,
                    f"최신 스냅샷 쌍으로 {recon['result_count']:,}건을 맞춰 봤습니다.")

        vcenter = counts.get("vcenter")
        if vcenter and usage:
            missing = sorted(set(vcenter["scopes"]) - set(usage["scopes"]))
            if missing:
                add("자원사용률에 없는 통합기", STALE,
                    f"vCenter 스냅샷에는 있는데 자원사용현황에는 없는 통합기: {', '.join(missing)}."
                    " 새로 붙인 통합기라면 07시 배치가 한 번 돌아야 나옵니다.",
                    scopes=missing)

        if itsm and vcenter:
            add("ITSM 자산 · vCenter VM", INFO,
                f"ITSM 자산 {itsm['selected']:,}대와 vCenter VM {vcenter['selected']:,}대는"
                " 같은 것을 세지 않습니다. ITSM 에는 물리서버와 vCenter 밖의 서버가 들어 있고,"
                " vCenter 에는 ITSM 에 아직 등록되지 않은 VM 이 들어 있습니다."
                " 둘을 맞춰 보는 것은 정합성 화면입니다.")
        return checks

    @staticmethod
    def _same(add: Any, name: str, left: int, right: int, what: str) -> None:
        if int(left) == int(right):
            add(name, OK, f"{what}가 같습니다 ({int(left):,}).", left=int(left), right=int(right))
            return
        add(name, MISMATCH,
            f"{what}가 다릅니다: {int(left):,} vs {int(right):,} (차이 {int(left) - int(right):+,}).",
            left=int(left), right=int(right))
