from __future__ import annotations

from datetime import date
from typing import Any

from ..repositories import AssetRepository


ALLOWED_EXCEPTION_TYPES = {
    "ITSM_ONLY",
    "RVTOOLS_ONLY",
    "MATCHED_WITH_DRIFT",
    "IP_CHANGE_CANDIDATE",
    "HOSTNAME_REVIEW",
    "AMBIGUOUS",
    "PAIR",
    "ALL",
}


class ReconciliationExceptionService:
    """Manage operator-approved reconciliation exclusions without deleting history."""

    def __init__(self, repository: AssetRepository) -> None:
        self.repo = repository

    def list(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        return self.repo.reconciliation_exceptions(include_inactive=include_inactive)

    def create_many(self, items: list[dict[str, Any]], user_id: str) -> dict[str, Any]:
        if not items:
            raise ValueError("예외처리 대상이 없습니다.")
        created: list[int] = []
        errors: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            try:
                item = self._validate(raw)
                created.append(self.repo.add_reconciliation_exception(item, user_id))
            except Exception as exc:
                errors.append({"row": index, "error": str(exc), "item": self._safe_item(raw)})
        return {"created_count": len(created), "created_ids": created, "error_count": len(errors), "errors": errors}

    def deactivate(self, exception_id: int, user_id: str) -> bool:
        return self.repo.deactivate_reconciliation_exception(exception_id, user_id)

    @staticmethod
    def applies(result: dict[str, Any], exception: dict[str, Any]) -> bool:
        exception_type = str(exception.get("exception_type") or "").upper()
        status = str(result.get("match_status") or "").upper()
        if exception_type not in {"ALL", "PAIR", status}:
            return False
        cm_id = str(exception.get("cm_id") or "").strip()
        rv_key = str(exception.get("rv_asset_key") or "").strip()
        if cm_id and cm_id != str(result.get("cm_id") or "").strip():
            return False
        if rv_key and rv_key != str(result.get("rv_asset_key") or "").strip():
            return False
        return bool(cm_id or rv_key)

    @staticmethod
    def _validate(raw: dict[str, Any]) -> dict[str, Any]:
        exception_type = str(raw.get("exception_type") or "").strip().upper()
        if exception_type not in ALLOWED_EXCEPTION_TYPES:
            raise ValueError(f"지원하지 않는 예외 유형입니다: {exception_type or '-'}")
        cm_id = str(raw.get("cm_id") or "").strip()
        rv_key = str(raw.get("rv_asset_key") or "").strip()
        if not cm_id and not rv_key:
            raise ValueError("CM_ID 또는 vCenter 자산키 중 하나는 필요합니다.")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise ValueError("예외처리 사유가 필요합니다.")
        valid_from = ReconciliationExceptionService._date_text(raw.get("valid_from"))
        valid_to = ReconciliationExceptionService._date_text(raw.get("valid_to"))
        if valid_from and valid_to and valid_from > valid_to:
            raise ValueError("유효 종료일은 시작일보다 빠를 수 없습니다.")
        return {
            "exception_type": exception_type,
            "cm_id": cm_id or None,
            "rv_asset_key": rv_key or None,
            "server_name": str(raw.get("server_name") or "").strip() or None,
            "reason": reason,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }

    @staticmethod
    def _date_text(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError as exc:
            raise ValueError(f"날짜 형식은 YYYY-MM-DD여야 합니다: {text}") from exc

    @staticmethod
    def _safe_item(raw: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in raw.items() if key not in {"password", "secret"}}
