"""서버 한 대의 판정 추적: 왜 생성 · 삭제로 잡혔(안 잡혔)나.

"ITSM 에서 운영 → 미사용으로 바꿨는데 삭제로 안 잡혔다" 같은 질문에 추측하지 않고
답하려면, 그 서버가 기간 동안 **날마다 어떤 상태로 수집됐고, 그 상태가 집계 기준에
들었는지**를 봐야 한다. 대수 판정은 늘 ITSM 스냅샷끼리의 비교라서 다음 중 하나가
원인이다.

1. 바뀐 뒤 ITSM 수집이 없다(배치가 안 돌았거나 실패) → 아직 '운영' 으로 남아 있다.
2. 바뀌기 전부터 집계 밖이었다(이미 미사용 · 수동 제외 등) → 빠질 것이 없다.
3. 수동 '포함' 규칙이 있다 → 상태와 무관하게 계속 센다.
4. 집계 기준에 미사용이 들어 있다(관리 → 자산 집계 기준).

이 서비스가 그 근거(스냅샷별 상태 · 집계 여부 · 수집 결과 · 이벤트 · VM 전원)를 모아
결론 한 줄과 함께 돌려준다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..normalization.code_maps import status_name
from .asset_scope import AssetScope

MAX_MATCHES = 5


class AssetTraceService:
    def __init__(self, config: Any, repository: Any, scope: AssetScope | None = None) -> None:
        self.config = config
        self.repo = repository
        self.scope = scope or AssetScope.load(config, repository)

    # ── 찾기 ────────────────────────────────────────────────────────────
    def find(self, query: str) -> list[str]:
        """자산번호 · 호스트명 · IP 로 자산번호를 찾는다. 지워진 자산도 찾도록 모든 스냅샷을 본다."""
        text = str(query or "").strip()
        if not text:
            return []
        rows = self.repo.conn.execute(
            "SELECT DISTINCT cm_id FROM itsm_asset_snapshot"
            " WHERE cm_id = ? OR LOWER(normalized_hostname) = ? OR primary_ip = ?"
            " ORDER BY cm_id LIMIT ?",
            (text, text.lower(), text, MAX_MATCHES + 1),
        ).fetchall()
        return [str(row["cm_id"]) for row in rows]

    # ── 추적 ────────────────────────────────────────────────────────────
    def trace(self, query: str, start: date, end: date) -> dict[str, Any]:
        matches = self.find(query)
        if not matches:
            return {"status": "NOT_FOUND", "query": query,
                    "message": "ITSM 스냅샷에서 이 자산번호 · 호스트명 · IP 를 찾지 못했습니다."}
        return {
            "status": "SUCCESS",
            "query": query,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "basis": self.scope.basis(),
            "more": len(matches) > MAX_MATCHES,
            "assets": [self._trace_one(cm_id, start, end) for cm_id in matches[:MAX_MATCHES]],
        }

    def _snapshots(self, start: date, end: date) -> list[dict[str, Any]]:
        """기간 전날까지의 마지막 하나 + 기간 안의 ITSM 스냅샷(정상 · 부분 성공)."""
        before = self.repo.snapshot_on_or_before("ITSM", (start - timedelta(days=1)).isoformat())
        rows = self.repo.conn.execute(
            "SELECT s.id, s.snapshot_date, s.collected_at, s.status, r.metadata_json"
            " FROM snapshot s LEFT JOIN collection_run r ON r.id = s.collection_run_id"
            " WHERE s.source='ITSM' AND s.snapshot_date >= ? AND s.snapshot_date <= ?"
            " AND s.status IN ('SUCCESS','PARTIAL_SUCCESS') ORDER BY s.snapshot_date, s.collected_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        found = [dict(row) for row in rows]
        if before and all(int(item["id"]) != int(before["id"]) for item in found):
            found.insert(0, {**dict(before), "metadata_json": None, "is_baseline": True})
        return found

    def _state(self, snapshot_id: int, cm_id: str) -> dict[str, Any]:
        row = self.repo.conn.execute(
            "SELECT status_code, normalized_hostname, primary_ip, raw_json FROM itsm_asset_snapshot"
            " WHERE snapshot_id = ? AND cm_id = ?",
            (int(snapshot_id), cm_id),
        ).fetchone()
        if row is None:
            return {"present": False, "counted": False, "status": None, "status_label": "ITSM 에 없음",
                    "reason": "ITSM 레코드 없음"}
        raw = json.loads(row["raw_json"] or "{}")
        decision = self.scope.decide_itsm({"cm_id": cm_id, "status_code": row["status_code"], "raw": raw})
        counted = bool(decision.included and not decision.reason)
        reason = ""
        if decision.manual:
            reason = f"수동 {'포함' if decision.included else '제외'}" + (f"({decision.note})" if decision.note else "")
        elif decision.reason:
            reason = self.scope.reason_label(decision.reason, row["status_code"])
        return {
            "present": True, "counted": counted, "status": row["status_code"],
            "status_label": status_name(row["status_code"]), "reason": reason,
            "manual": decision.manual, "hostname": row["normalized_hostname"], "ip": row["primary_ip"],
            "service_name": raw.get("CM_NAME"),
        }

    @staticmethod
    def _held_events(metadata: Any) -> str:
        """그 수집에서 변경 이벤트를 보류했나(조회 SQL 변경 · 건수 이상)."""
        try:
            meta = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            return ""
        if meta.get("query_changed"):
            return "조회 SQL 이 바뀌어 이날 변경 이벤트를 보류함"
        if (meta.get("baseline") or {}).get("critical"):
            return "수집 건수가 기준 미만이라 이날 변경 이벤트를 보류함"
        return ""

    def _trace_one(self, cm_id: str, start: date, end: date) -> dict[str, Any]:
        snapshots = self._snapshots(start, end)
        timeline: list[dict[str, Any]] = []
        for snap in snapshots:
            state = self._state(int(snap["id"]), cm_id)
            timeline.append({
                "date": snap["snapshot_date"], "collected_at": snap["collected_at"],
                "baseline": bool(snap.get("is_baseline")), "held": self._held_events(snap.get("metadata_json")),
                **state,
            })
        events = [
            {"detected_at": e.get("detected_at"), "event_type": e.get("event_type"),
             "field": e.get("field_name"), "old": e.get("old_value"), "new": e.get("new_value")}
            for e in self.repo.changes("ITSM", 5000, f"{start.isoformat()}T00:00:00",
                                       f"{(end + timedelta(days=1)).isoformat()}T00:00:00")
            if str(e.get("asset_key")) == cm_id and e.get("event_type") != "ITSM_ASSET_UPDATED"
        ]
        events.reverse()
        known = next((t for t in reversed(timeline) if t.get("hostname")), {})
        rule = self.scope.rule_for("ITSM", cm_id)
        return {
            "cm_id": cm_id,
            "hostname": known.get("hostname"),
            "ip": known.get("ip"),
            "service_name": known.get("service_name"),
            "manual_rule": ({"mode": rule.get("mode"), "reason": rule.get("reason")} if rule else None),
            "timeline": self._compress(timeline),
            "events": list({(t["at"], t["text"]): t for t in (self._event_text(e) for e in events)}.values()),
            "vm": self._vm_history(known.get("hostname"), known.get("ip"), start, end),
            "verdict": self._verdict(timeline, events, end),
        }

    @staticmethod
    def _compress(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """같은 상태가 이어지는 날은 한 줄로 묶는다(10/01~10/31 운영 · 집계 안)."""
        out: list[dict[str, Any]] = []
        for item in timeline:
            last = out[-1] if out else None
            same = last and all(last.get(k) == item.get(k) for k in ("present", "status", "counted", "reason")) \
                and not item.get("held") and not last.get("held")
            if same:
                last["until"] = item["date"]
                last["days"] += 1
            else:
                out.append({**item, "until": item["date"], "days": 1})
        return out

    def _event_text(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = str(event.get("event_type") or "")
        if event.get("field") == "CM_STA_CD":
            text = f"상태 {status_name(event.get('old'))} → {status_name(event.get('new'))}"
        elif kind == "ITSM_ASSET_CREATED":
            text = "ITSM 신규 등록"
        elif kind == "ITSM_RECORD_REMOVED":
            text = "ITSM 레코드 삭제"
        else:
            text = f"{event.get('field') or kind} 변경"
        return {"at": event.get("detected_at"), "type": kind, "text": text}

    def _vm_history(self, hostname: Any, ip: Any, start: date, end: date) -> list[dict[str, Any]]:
        """같은 호스트명 · IP 의 VM 이 기간 동안 어땠는지(있음 · 전원). 짝이 없으면 빈 목록."""
        if not hostname and not ip:
            return []
        rows = self.repo.conn.execute(
            "SELECT s.snapshot_date, a.vm_name, a.power_state, a.vcenter FROM snapshot s"
            " JOIN rv_asset_snapshot a ON a.snapshot_id = s.id"
            " WHERE s.source='RVTOOLS' AND s.snapshot_date >= ? AND s.snapshot_date <= ?"
            " AND (LOWER(a.normalized_hostname) = ? OR a.primary_ip = ?)"
            " ORDER BY s.snapshot_date",
            ((start - timedelta(days=1)).isoformat(), end.isoformat(), str(hostname or "").lower(), str(ip or "")),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            power = "켜짐" if str(row["power_state"] or "").lower() == "poweredon" else "꺼짐"
            if out and out[-1]["power"] == power and out[-1]["vm_name"] == row["vm_name"]:
                out[-1]["until"] = row["snapshot_date"]
                continue
            out.append({"date": row["snapshot_date"], "until": row["snapshot_date"], "vm_name": row["vm_name"],
                        "power": power, "vcenter": row["vcenter"]})
        return out

    def _verdict(self, timeline: list[dict[str, Any]], events: list[dict[str, Any]], end: date) -> dict[str, str]:
        basis = self.scope.basis_label
        in_period = [t for t in timeline if not t.get("baseline")]
        if not timeline:
            return {"level": "error", "text": "이 기간 앞뒤로 ITSM 수집이 하나도 없습니다. 배치부터 확인하세요."}
        first, last = timeline[0], timeline[-1]
        if not in_period:
            return {"level": "error",
                    "text": f"기간 안에 ITSM 수집이 없습니다(마지막 수집 {first['date']}). ITSM 에서 바꾼 것이 아직"
                            " 들어오지 않았으므로 삭제로 잡힐 수 없습니다. 일배치를 확인하세요."}
        stale = last["date"] < end.isoformat() and (end - date.fromisoformat(last["date"])).days >= 2
        tail = f" (마지막 ITSM 수집 {last['date']} — 그 뒤로 수집이 없습니다)" if stale else ""
        held = [t for t in timeline if t.get("held")]
        held_text = f" 참고: {held[0]['date']} {held[0]['held']}." if held else ""
        if first["counted"] and not last["counted"]:
            moved = next((t for t in timeline if not t["counted"]), last)
            why = "ITSM 에서 지워져" if not moved["present"] else f"ITSM 상태가 바뀌어(→ {moved['status_label']})"
            return {"level": "ok",
                    "text": f"{moved['date']} 에 {why} 집계 기준({basis}) 밖으로 나갔습니다 → 이 기간 [삭제] −1 로"
                            f" 잡힙니다.{held_text}"}
        if not first["counted"] and last["counted"]:
            moved = next((t for t in timeline if t["counted"]), last)
            return {"level": "ok",
                    "text": f"{moved['date']} 에 ITSM 상태가 바뀌어(→ {moved['status_label']}) 집계 기준({basis}) 안에 들어왔습니다 →"
                            f" 이 기간 [생성] +1 로 잡힙니다.{held_text}"}
        if first["counted"] and last["counted"]:
            if last.get("manual") and last.get("reason", "").startswith("수동 포함"):
                return {"level": "warn",
                        "text": f"ITSM 상태는 {last['status_label']}이지만 수동 '포함' 규칙이 있어 계속 자산으로 셉니다."
                                " 빼려면 자산 목록에서 수동 포함을 풀어 주세요."}
            if not self.scope.counts_status(last.get("status")):
                return {"level": "warn", "text": f"상태는 {last['status_label']}인데 자산으로 셉니다({last.get('reason')})."}
            if any(t.get("status") != first.get("status") for t in timeline):
                return {"level": "info",
                        "text": f"상태가 바뀌었지만 둘 다 집계 기준({basis}) 안이라 대수는 그대로입니다"
                                f"({first['status_label']} → {last['status_label']}).{tail}"}
            return {"level": "warn",
                    "text": f"수집된 ITSM 은 기간 내내 {last['status_label']}입니다{tail}. ITSM 에서 바꿨다면 그 뒤에 수집이"
                            " 아직 안 됐거나, 다른 자산번호를 바꾼 것입니다."}
        # 처음도 끝도 집계 밖
        why = first.get("reason") or first.get("status_label")
        return {"level": "info",
                "text": f"기간 시작 전부터 집계 기준({basis}) 밖이었습니다({first['date']}: {why}). 빠질 것이 없으므로"
                        " 삭제로 잡히지 않습니다."}
