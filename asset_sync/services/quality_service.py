from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..config import AppConfig
from ..repositories import AssetRepository
from .override_service import OverrideService


class DataQualityService:
    """Run initial ITSM required-field and EOS quality checks."""

    def __init__(self, config: AppConfig, repository: AssetRepository) -> None:
        self.config = config
        self.repo = repository

    def run(self, snapshot_id: int) -> dict[str, Any]:
        records = OverrideService(self.config, self.repo).apply(self.repo.load_itsm_records(snapshot_id))
        eos_field = str(self.config.itsm.get("os_eos_field", "OS_EOS_DATE"))
        cpu_field = str(self.config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT"))
        memory_field = str(self.config.itsm.get("memory_field", "CM_MEMORY"))
        owner_field = str(self.config.itsm.get("owner_field", "CM_WOR_MNG_EMP_ID"))
        near_days = int(self.config.quality.get("eos_near_days", 180))
        now = date.today()
        rows: list[tuple[Any, ...]] = []
        counts: dict[str, int] = {}
        for cm_id, record in records.items():
            raw = record["raw"]
            required = {
                "CM_IP": record.get("primary_ip"), "CM_HOSTNAME": record.get("normalized_hostname"),
                cpu_field: record.get("cpu_cores"), memory_field: record.get("memory_mb"),
                "CM_OS": record.get("os_family"), eos_field: raw.get(eos_field), owner_field: raw.get(owner_field),
                "CM_SVR_CAT_CD": raw.get("CM_SVR_CAT_CD"), "CM_PLACE": raw.get("CM_PLACE"),
                "CM_OWN_CAT_CD": raw.get("CM_OWN_CAT_CD"), "CM_RACK_LOC": raw.get("CM_RACK_LOC"),
                "CM_STA_CD": raw.get("CM_STA_CD"),
            }
            status = raw.get("CM_STA_CD")
            server_cat = raw.get("CM_SVR_CAT_CD")
            for field, value in required.items():
                if status in {"CMSTA020", "CMSTA060"} and field in {"CM_IP", "CM_HOSTNAME", "CM_RACK_LOC"}:
                    continue
                if server_cat == "CMSVRCATCD020" and field in {"CM_RACK_LOC"}:
                    continue
                if value in (None, "", [], "-"):
                    rows.append((snapshot_id, cm_id, field, "MISSING", "필수정보 누락", datetime.now().isoformat()))
                    counts["MISSING"] = counts.get("MISSING", 0) + 1
            raw_eos = raw.get(eos_field)
            eos_text = "" if raw_eos is None else str(raw_eos).strip()
            eos = self._parse_date(raw_eos)
            if eos_text.startswith("9999"):
                pass  # 업무상 미정 값. 날짜 오류로 보지 않는다.
            elif raw_eos and eos is None:
                rows.append((snapshot_id, cm_id, eos_field, "INVALID_FORMAT", "EOS 날짜 형식 오류", datetime.now().isoformat()))
                counts["INVALID_FORMAT"] = counts.get("INVALID_FORMAT", 0) + 1
            elif eos and eos < now:
                rows.append((snapshot_id, cm_id, eos_field, "EXPIRED", "OS EOS 종료", datetime.now().isoformat()))
                counts["EXPIRED"] = counts.get("EXPIRED", 0) + 1
            elif eos and eos <= now + timedelta(days=near_days):
                rows.append((snapshot_id, cm_id, eos_field, "NEAR_EXPIRY", f"{near_days}일 이내 EOS", datetime.now().isoformat()))
                counts["NEAR_EXPIRY"] = counts.get("NEAR_EXPIRY", 0) + 1
        self.repo.conn.execute("DELETE FROM data_quality_result WHERE snapshot_id=?", (snapshot_id,))
        self.repo.conn.executemany(
            "INSERT INTO data_quality_result(snapshot_id, cm_id, field_name, quality_status, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        return {"snapshot_id": snapshot_id, "total_issues": len(rows), "counts": counts}

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if value is None:
            return None
        text = str(value).strip().replace("-", "").replace("/", "")
        if text.startswith("9999"):
            return None
        if len(text) >= 8 and text[:8].isdigit():
            try:
                return datetime.strptime(text[:8], "%Y%m%d").date()
            except ValueError:
                return None
        return None
