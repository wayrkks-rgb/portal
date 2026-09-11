"""통합기·ESXi·데이터스토어의 업무명.

vCenter 가 붙인 이름(`vc_0001`, `esxi-07`, `DS_SAS_01`)으로는 보고서에서 무엇인지
알 수 없다. 업무에서 쓰는 이름("Linux 통합기 #1")은 수집 결과와 별도로 둔다.
그래야 다시 수집해도 남고, vCenter 쪽을 고칠 필요도 없다.

**통합기 = 클러스터**다. 연동은 클러스터 단위로 하고, 그 안의 여러 ESXi 가 하나의
통합기를 이룬다. 그래서 보고서의 한 줄은 클러스터 하나이고, ESXi 는 그 구성요소다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from ..repositories import AssetRepository

#: 이름을 붙일 수 있는 대상. 값은 화면 표기용이다.
SCOPES: dict[str, str] = {
    "VCENTER": "vCenter",
    "CLUSTER": "통합기(클러스터)",
    "ESXI": "ESXi 호스트",
    "DATASTORE": "데이터스토어",
}

MAX_NAME_LENGTH = 255
MAX_NOTE_LENGTH = 500


class DisplayNameError(ValueError):
    pass


def normalize_scope(value: Any) -> str:
    scope = str(value or "").strip().upper()
    if scope not in SCOPES:
        raise DisplayNameError(f"대상 구분은 {', '.join(SCOPES)} 중 하나여야 합니다: {value!r}")
    return scope


def _normalize_text(value: Any, *, label: str, limit: int, required: bool) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise DisplayNameError(f"{label}을(를) 입력하세요.")
    if len(text) > limit:
        raise DisplayNameError(f"{label}이(가) 너무 깁니다({limit}자 이내).")
    return text


class DisplayNameService:
    """업무명을 읽고 쓴다. 수집 결과에는 손대지 않는다."""

    def __init__(self, repository: AssetRepository) -> None:
        self.repo = repository

    # ── 읽기 ────────────────────────────────────────────────────────────
    def all(self) -> list[dict[str, Any]]:
        return self.repo.display_names()

    def lookup(self) -> dict[tuple[str, str, str], str]:
        """(scope, vcenter_id, object_key) → 업무명"""
        return {
            (row["scope"], row["vcenter_id"], row["object_key"]): row["display_name"]
            for row in self.repo.display_names()
        }

    def resolver(self) -> "NameResolver":
        return NameResolver(self.lookup())

    # ── 쓰기 ────────────────────────────────────────────────────────────
    def save_many(self, items: Iterable[Mapping[str, Any]], updated_by: str) -> dict[str, Any]:
        """여러 건을 한 번에 저장한다. 빈 이름은 지정 해제로 본다.

        한 건이라도 잘못되면 아무것도 쓰지 않는다. 절반만 반영되면 어느 줄이
        저장됐는지 화면에서 알 수 없다.
        """
        saved: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        now = datetime.now().isoformat(timespec="seconds")
        for index, item in enumerate(items, 1):
            try:
                scope = normalize_scope(item.get("scope"))
                object_key = _normalize_text(
                    item.get("object_key"), label="대상 이름", limit=MAX_NAME_LENGTH, required=True
                )
                display_name = _normalize_text(
                    item.get("display_name"), label="업무명", limit=MAX_NAME_LENGTH, required=False
                )
                note = _normalize_text(item.get("note"), label="비고", limit=MAX_NOTE_LENGTH, required=False)
            except DisplayNameError as exc:
                raise DisplayNameError(f"{index}번째 줄: {exc}") from exc
            vcenter_id = _normalize_text(
                item.get("vcenter_id"), label="vCenter", limit=64, required=False
            )
            row = {
                "scope": scope, "vcenter_id": vcenter_id, "object_key": object_key,
                "display_name": display_name, "note": note,
                "updated_by": updated_by, "updated_at": now,
            }
            (removed if not display_name else saved).append(row)

        self.repo.replace_display_names(saved, removed)
        return {"saved_count": len(saved), "removed_count": len(removed)}


class NameResolver:
    """업무명이 있으면 그것을, 없으면 원래 이름을 돌려준다.

    이름을 아직 안 붙인 대상도 화면에는 나와야 한다. 빈칸으로 두면 목록에서
    사라진 것처럼 보인다.
    """

    def __init__(self, mapping: Mapping[tuple[str, str, str], str]) -> None:
        self._mapping = dict(mapping)

    def name(self, scope: str, object_key: Any, vcenter_id: Any = "") -> str:
        key = str(object_key or "")
        if not key:
            return ""
        scope = str(scope).upper()
        vc = str(vcenter_id or "")
        # vCenter 를 지정한 항목이 먼저다. 같은 ESXi 이름이 다른 vCenter 에 있을 수 있다.
        for candidate in ((scope, vc, key), (scope, "", key)):
            if candidate in self._mapping:
                return self._mapping[candidate]
        return key

    def named(self, scope: str, object_key: Any, vcenter_id: Any = "") -> bool:
        """업무명을 붙였는지. 화면에서 '미지정' 을 표시하는 데 쓴다."""
        key = str(object_key or "")
        scope = str(scope).upper()
        return any(
            candidate in self._mapping
            for candidate in ((scope, str(vcenter_id or ""), key), (scope, "", key))
        )
