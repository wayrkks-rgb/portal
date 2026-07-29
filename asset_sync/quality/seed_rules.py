from __future__ import annotations

from ..config import AppConfig
from ..repositories import AssetRepository


def seed_default_rules(repository: AssetRepository, config: AppConfig) -> None:
    rules = [
        ("논리 운영 IP", "CM_IP", "REQUIRED", "ERROR"),
        ("논리 운영 Hostname", "CM_HOSTNAME", "REQUIRED", "ERROR"),
        ("CPU", str(config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")), "REQUIRED", "ERROR"),
        ("Memory", str(config.itsm.get("memory_field", "CM_MEMORY")), "REQUIRED", "ERROR"),
        ("OS", "CM_OS", "REQUIRED", "ERROR"),
        ("OS EOS", str(config.itsm.get("os_eos_field", "OS_EOS_DATE")), "DATE_EOS", "WARNING"),
        ("담당자", str(config.itsm.get("owner_field", "CM_WOR_MNG_EMP_ID")), "REQUIRED", "WARNING"),
    ]
    repository.conn.executemany(
        "INSERT OR IGNORE INTO data_quality_rule(rule_name, field_name, rule_type, severity, enabled) VALUES (?, ?, ?, ?, 1)",
        rules,
    )
