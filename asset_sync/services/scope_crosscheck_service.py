"""VM 제외와 서버현황이 서로 맞는지 본다.

VM 을 전원 꺼짐 등으로 자원 집계에서 뺐는데, 같은 서버가 ITSM 서버현황에는
자산으로 남아 있다면 **확인이 필요한 서버**다. 둘 중 하나가 틀렸다.

- VM 을 뺀 판단이 맞다면 ITSM 상태를 미사용·폐기로 고쳐야 한다.
- ITSM 이 맞다면(실제로 쓰는 서버다) VM 을 다시 세야 한다.

반대 방향도 같다. ITSM 은 미사용인데 VM 은 켜져 돌고 있으면, 실물은 살아 있고
장부만 죽은 것이다. 그것도 확인이 필요하다.

어느 쪽이 맞는지는 사람이 안다. 이 서비스는 **어긋난 것을 찾아 이유와 함께**
보여주고, 그 자리에서 고칠 수 있게 양쪽 값을 같이 담는다.

짝짓기는 정합성 화면과 **같은 규칙**(``asset_matching``)을 쓴다. 따로 두면 한쪽은
짝을 찾고 다른 쪽은 못 찾아, 어느 쪽을 믿어야 하는지 알 수 없게 된다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import asset_matching
from .asset_scope import AssetScope, REASON_LABELS, load_itsm_records

#: 판정. 앞의 둘이 '확인 필요' 다.
VM_EXCLUDED = "VM_EXCLUDED_BUT_ASSET"
ITSM_EXCLUDED = "ASSET_EXCLUDED_BUT_VM_LIVE"
POWERED_OFF = "ASSET_POWERED_OFF"
NO_VM = "ASSET_WITHOUT_VM"
BOTH_EXCLUDED = "BOTH_EXCLUDED"
VM_ONLY = "VM_WITHOUT_ASSET"
AMBIGUOUS = "AMBIGUOUS"
AGREED = "AGREED"

#: 화면에 그대로 쓸 문구. 코드만 보여 주면 받은 사람이 읽을 수 없다.
VERDICTS: dict[str, dict[str, str]] = {
    VM_EXCLUDED: {
        "label": "VM 은 뺐는데 자산으로 셈",
        "action": "VM 을 뺀 판단이 맞으면 ITSM 상태를 미사용·폐기로 고치고,"
                  " 실제로 쓰는 서버면 VM 제외를 되돌리세요.",
        "review": "1",
    },
    ITSM_EXCLUDED: {
        "label": "자산에서 뺐는데 VM 은 살아 있음",
        "action": "실물이 돌고 있습니다. ITSM 상태를 확인하거나 VM 을 내리세요.",
        "review": "1",
    },
    POWERED_OFF: {
        "label": "자산인데 VM 전원이 꺼져 있음",
        "action": "오래 꺼져 있으면 쓰지 않는 서버입니다. 자산에서 뺄지 결정하세요.",
        "review": "1",
    },
    NO_VM: {
        "label": "논리서버 자산인데 VM 이 없음",
        "action": "vCenter 에서 사라졌거나 이름·IP 가 달라 짝을 못 찾았습니다."
                  " 정합성 화면에서 짝을 지어 주세요.",
        "review": "1",
    },
    BOTH_EXCLUDED: {"label": "양쪽 모두 뺀 것", "action": "", "review": ""},
    VM_ONLY: {
        "label": "VM 만 있고 ITSM 에 없음",
        "action": "ITSM 에 등록되지 않은 VM 입니다.",
        "review": "",
    },
    AMBIGUOUS: {
        "label": "짝 후보가 여럿",
        "action": "호스트명·IP 가 겹칩니다. 정합성 화면에서 짝을 확정하세요.",
        "review": "",
    },
    AGREED: {"label": "양쪽이 맞음", "action": "", "review": ""},
}

#: 확인이 필요한 판정. 화면 기본 목록이 이것만 보여준다.
REVIEW_VERDICTS = (VM_EXCLUDED, ITSM_EXCLUDED, POWERED_OFF, NO_VM)


class ScopeCrossCheckService:
    """양쪽의 '셀지 말지' 판단을 견주어 어긋난 것을 찾는다."""

    def __init__(self, config: Any, repository: Any) -> None:
        self.config = config
        self.repo = repository
        # 교차 점검은 뺀 것까지 봐야 한다. include_all 로 읽어 판단만 꺼내 쓴다.
        self.scope = AssetScope.load(config, repository, include_all=True)

    def check(
        self,
        itsm_snapshot_id: int | None = None,
        rv_snapshot_id: int | None = None,
    ) -> dict[str, Any]:
        itsm_snapshot = (self.repo.snapshot_by_id(itsm_snapshot_id) if itsm_snapshot_id
                         else self.repo.latest_snapshot("ITSM"))
        rv_snapshot = (self.repo.snapshot_by_id(rv_snapshot_id) if rv_snapshot_id
                       else self.repo.latest_snapshot("RVTOOLS"))
        if not itsm_snapshot or not rv_snapshot:
            return {
                "status": "NO_SNAPSHOT",
                "message": "ITSM 과 vCenter 스냅샷이 모두 있어야 견줄 수 있습니다.",
                "items": [], "counts": {}, "summary": {},
            }

        assets = load_itsm_records(self.config, self.repo, int(itsm_snapshot["id"]))
        vms = self.repo.load_rv_records(int(rv_snapshot["id"]))
        index = asset_matching.build_index(vms)
        identity_map = self.repo.identity_maps()
        allow_vm_name = bool(self.config.matching.get("allow_vm_name_auto_match", False))
        power_on = str(self.config.rvtools.get("power_on_value", "poweredon")).strip().lower()

        items: list[dict[str, Any]] = []
        used: set[str] = set()
        for asset in assets:
            described = self.scope.describe_itsm(asset)
            cm_id = str(described.get("cm_id") or "")
            match = asset_matching.find(
                cm_id, asset, index, identity_map=identity_map, allow_vm_name=allow_vm_name
            )
            if match.key:
                used.add(match.key)
            items.append(self._row(described, vms.get(match.key or ""), match, power_on))

        # ITSM 에 없는 VM. 세는 것만 올린다 -- vCLS 까지 올리면 목록이 쓸모없어진다.
        for key in sorted(set(vms) - used):
            vm = vms[key]
            decision = self.scope.decide_vcenter(vm)
            if decision.reason:
                continue
            items.append(self._vm_only_row(vm, key))

        counts = Counter(str(item["verdict"]) for item in items)
        return {
            "status": "SUCCESS",
            "as_of": {
                "itsm": itsm_snapshot["snapshot_date"],
                "vcenter": rv_snapshot["snapshot_date"],
            },
            "counts": {
                code: {
                    "count": int(counts.get(code, 0)),
                    "label": VERDICTS[code]["label"],
                    "review": bool(VERDICTS[code]["review"]),
                }
                for code in VERDICTS
            },
            "summary": {
                "checked": len(items),
                "review": sum(int(counts.get(code, 0)) for code in REVIEW_VERDICTS),
                "agreed": int(counts.get(AGREED, 0)),
            },
            "items": items,
        }

    # ── 한 줄 만들기 ────────────────────────────────────────────────────
    def _row(
        self,
        asset: dict[str, Any],
        vm: dict[str, Any] | None,
        match: asset_matching.Match,
        power_on: str,
    ) -> dict[str, Any]:
        # ``include_all`` 로 읽었으므로 included 는 늘 True 다. 사유가 있으면
        # 뺀 것으로 본다 -- 사유가 판단이고 included 는 '보여줄지' 일 뿐이다.
        asset_included = not asset.get("exclude_reason")
        row: dict[str, Any] = {
            "cm_id": asset.get("cm_id"),
            "hostname": asset.get("hostname"),
            "primary_ip": asset.get("primary_ip"),
            "service_name": asset.get("service_name"),
            "location": asset.get("location"),
            "physical": bool(asset.get("physical")),
            "kind": "물리서버" if asset.get("physical") else "논리서버",
            "status_code": asset.get("status_code"),
            "os_group": asset.get("os_group"),
            "asset_included": asset_included,
            "asset_reason": asset.get("exclude_reason") or "",
            "asset_reason_label": asset.get("exclude_label") or "",
            "match_method": match.method,
            "match_score": match.score,
            "vm_name": None, "vm_uuid": None, "vcenter_id": None,
            "cluster_name": None, "esxi_host": None, "power_state": None,
            "vm_included": None, "vm_reason": "", "vm_reason_label": "",
        }
        if match.ambiguous:
            row["verdict"] = AMBIGUOUS
            row["candidates"] = list(match.candidates)
            return self._finish(row)
        if vm is None:
            # 물리서버는 VM 이 없는 것이 당연하다. 논리서버만 확인 대상이다.
            row["verdict"] = (
                NO_VM if asset_included and not asset.get("physical") else AGREED
            )
            return self._finish(row)

        decision = self.scope.decide_vcenter(vm)
        powered_on = str(vm.get("power_state") or "").strip().lower() == power_on
        row.update({
            "vm_name": vm.get("vm_name"), "vm_uuid": vm.get("vm_uuid"),
            "vcenter_id": vm.get("vcenter"),
            "cluster_name": vm.get("cluster_name") or vm.get("cluster"),
            "esxi_host": vm.get("esxi_host"),
            "power_state": vm.get("power_state"),
            "vm_included": not decision.reason,
            "vm_reason": decision.reason or "",
            "vm_reason_label": REASON_LABELS.get(decision.reason, "") if decision.reason else "",
            "vm_note": decision.note or "",
        })
        # 전원이 꺼져 있으면 사유에 함께 적는다. 수집기는 전원만으로 빼지 않으므로
        # 사람이 뺀 것인지 꺼진 것인지 구분해서 보여야 한다.
        if not powered_on:
            row["powered_off"] = True

        if asset_included and not row["vm_included"]:
            row["verdict"] = VM_EXCLUDED
        elif asset_included and not powered_on:
            row["verdict"] = POWERED_OFF
        elif not asset_included and row["vm_included"] and powered_on:
            row["verdict"] = ITSM_EXCLUDED
        elif not asset_included and not row["vm_included"]:
            row["verdict"] = BOTH_EXCLUDED
        else:
            row["verdict"] = AGREED
        return self._finish(row)

    def _vm_only_row(self, vm: dict[str, Any], key: str) -> dict[str, Any]:
        return self._finish({
            "cm_id": None, "hostname": vm.get("normalized_hostname") or vm.get("dns_name"),
            "primary_ip": vm.get("primary_ip"), "service_name": vm.get("vm_name"),
            "location": None, "physical": False, "kind": "논리서버",
            "status_code": None, "os_group": None,
            "asset_included": None, "asset_reason": "", "asset_reason_label": "",
            "match_method": None, "match_score": 0,
            "vm_name": vm.get("vm_name"), "vm_uuid": vm.get("vm_uuid"),
            "vcenter_id": vm.get("vcenter"),
            "cluster_name": vm.get("cluster_name") or vm.get("cluster"),
            "esxi_host": vm.get("esxi_host"), "power_state": vm.get("power_state"),
            "vm_included": True, "vm_reason": "", "vm_reason_label": "",
            "verdict": VM_ONLY, "asset_key": key,
        })

    #: 엑셀 칸. 받아서 담당자에게 돌릴 목록이므로 양쪽 값을 다 담는다.
    EXPORT_COLUMNS = (
        ("판정", "verdict_label"), ("해야 할 일", "action"), ("왜", "why"),
        ("자산번호", "cm_id"), ("호스트명", "hostname"), ("IP", "primary_ip"),
        ("업무명", "service_name"), ("위치", "location"), ("물리/논리", "kind"),
        ("OS", "os_group"), ("ITSM 상태코드", "status_code"),
        ("ITSM 제외 사유", "asset_reason_label"),
        ("VM 명", "vm_name"), ("전원상태", "power_state"),
        ("VM 제외 사유", "vm_reason_label"), ("vCenter", "vcenter_id"),
        ("클러스터", "cluster_name"), ("통합기(ESXi)", "esxi_host"),
        ("짝짓기 방법", "match_method"),
    )

    @classmethod
    def write_xlsx(cls, result: dict[str, Any], items: list[dict[str, Any]], path: Any) -> Any:
        """엑셀 한 장. 판정과 해야 할 일을 맨 앞에 둔다."""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "제외교차점검"
        as_of = result.get("as_of") or {}
        sheet["A1"] = "VM 제외 ↔ 서버현황 교차 점검"
        sheet["A1"].font = Font(bold=True, size=13)
        sheet["A2"] = (
            f"ITSM {as_of.get('itsm') or '-'} · vCenter {as_of.get('vcenter') or '-'} 기준"
            f" · 확인 필요 {int((result.get('summary') or {}).get('review') or 0):,}건"
        )
        sheet["A2"].font = Font(size=9, color="666666")
        sheet.append([])
        sheet.append([label for label, _ in cls.EXPORT_COLUMNS])
        head = sheet.max_row
        for cell in sheet[head]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="ED7D31")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for item in items:
            sheet.append([item.get(key) for _, key in cls.EXPORT_COLUMNS])
        sheet.freeze_panes = sheet.cell(head + 1, 1).coordinate
        sheet.auto_filter.ref = f"A{head}:{sheet.cell(head, len(cls.EXPORT_COLUMNS)).column_letter}{sheet.max_row}"
        for column in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in column)
            sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 46)
        workbook.save(path)
        workbook.close()
        return path

    @staticmethod
    def _finish(row: dict[str, Any]) -> dict[str, Any]:
        verdict = VERDICTS[str(row["verdict"])]
        row["verdict_label"] = verdict["label"]
        row["action"] = verdict["action"]
        row["review"] = bool(verdict["review"])
        # 왜 이렇게 판정됐는지 한 줄로. 표만 보고도 알 수 있어야 한다.
        parts = []
        if row.get("asset_included") is True:
            parts.append("ITSM: 자산")
        elif row.get("asset_included") is False:
            parts.append(f"ITSM: 제외({row.get('asset_reason_label') or row.get('asset_reason')})")
        if row.get("vm_included") is True:
            parts.append(f"vCenter: 세는 중({row.get('power_state') or '전원 미상'})")
        elif row.get("vm_included") is False:
            parts.append(f"vCenter: 제외({row.get('vm_reason_label') or row.get('vm_reason')})")
        elif row.get("vm_name") is None and row.get("cm_id"):
            parts.append("vCenter: VM 없음")
        row["why"] = " · ".join(parts)
        return row
