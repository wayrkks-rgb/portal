"""Build the ITSM asset SELECT statement from a real Oracle table.

The collector validates a fixed set of logical columns (CM_ID, CM_HOSTNAME, the
configured CPU/Memory/EOS fields, ...). A site table rarely carries every one of
them under exactly that name, which is why a hand-written query file so often
ends in ORA-00904 or ORA-00942. Given the columns that actually exist in the
selected table, this module produces a query that always satisfies the
validation: matched columns are aliased to the logical name, missing ones are
selected as NULL and reported back so the operator can map them by hand.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping

from ..utils.validation import validate_oracle_identifier

# Logical columns the pipeline reads, in output order. cpu/memory/eos entries are
# filled from the ITSM settings because they are configurable per site.
BASE_LOGICAL_COLUMNS = (
    "CM_ID", "CM_NAME", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_OS", "CM_OS_VERSION",
    "CM_CPU_CNT", "CM_OWN_CAT_CD", "CM_SVR_CAT_CD", "CM_CAT_CD", "CM_STA_CD", "CM_NET_CD",
    "CM_DR_YN", "CM_MTN_YN", "CM_FMTN_YN", "CM_OWN_EMP_ID", "CM_OWN_DPT_ID",
    "CM_USER_EMP_ID", "CM_USER_DPT_ID", "CM_WOR_MNG_EMP_ID", "CM_PLACE", "CM_RACK_LOC",
    "CM_SERIAL_NO", "CM_MAKE_NAME", "CM_MODEL_NAME", "CM_TAKIN_DTTM", "CM_REG_DTTM",
    "CM_MOD_DTTM", "CM_DESCR", "CM_DESCR2",
)

# Columns the collector refuses to run without.
REQUIRED_LOGICAL_COLUMNS = (
    "CM_ID", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_OS", "CM_OS_VERSION",
    "CM_SVR_CAT_CD", "CM_STA_CD",
)

# Common ITSM naming variants seen outside the CM_* convention. Exact and
# normalised matching runs first; these are the fallback.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "CM_ID": ("ASSET_ID", "ASSETNO", "ASSET_NO", "CI_ID", "CONFIG_ID", "EQUIP_ID", "RESOURCE_ID", "MGMT_NO"),
    "CM_NAME": ("ASSET_NAME", "CI_NAME", "EQUIP_NAME", "RESOURCE_NAME", "SYSTEM_NAME"),
    "CM_HOSTNAME": ("HOSTNAME", "HOST_NAME", "SERVER_NAME", "SVR_NAME", "NODE_NAME", "MACHINE_NAME"),
    "CM_IP": ("IP", "IP_ADDR", "IP_ADDRESS", "MAIN_IP", "SERVER_IP", "SVR_IP", "PRIMARY_IP"),
    "CM_SUB_IP": ("SUB_IP", "SUB_IP_ADDR", "SECOND_IP", "SECONDARY_IP", "BACKUP_IP"),
    "CM_OS": ("OS", "OS_NAME", "OS_TYPE", "OS_KIND"),
    "CM_OS_VERSION": ("OS_VERSION", "OS_VER", "OS_RELEASE"),
    "CM_CPU_CORE_CNT": ("CPU_CORE_CNT", "CORE_CNT", "CPU_CORES", "CORE_COUNT", "VCPU_CNT", "CPU_CORE"),
    "CM_CPU_CNT": ("CPU_CNT", "CPU_COUNT", "SOCKET_CNT", "CPU_SOCKET_CNT"),
    "CM_MEMORY": ("MEMORY", "MEM_SIZE", "MEMORY_SIZE", "MEMORY_GB", "MEM_GB", "RAM", "RAM_SIZE"),
    "CM_SVR_CAT_CD": ("SVR_CAT_CD", "SERVER_TYPE", "SVR_TYPE_CD", "SERVER_CAT_CD"),
    "CM_CAT_CD": ("CAT_CD", "CATEGORY_CD", "ASSET_CAT_CD", "CLASS_CD"),
    "CM_STA_CD": ("STA_CD", "STATUS_CD", "STATUS", "USE_STATUS", "USE_YN", "OPER_STATUS"),
    "CM_NET_CD": ("NET_CD", "NETWORK_CD", "NETWORK_ZONE", "ZONE_CD"),
    "CM_SERIAL_NO": ("SERIAL_NO", "SERIAL", "SERIAL_NUM", "SN"),
    "CM_MAKE_NAME": ("MAKER", "MAKER_NAME", "VENDOR", "VENDOR_NAME", "MANUFACTURER"),
    "CM_MODEL_NAME": ("MODEL", "MODEL_NAME", "MODEL_NO"),
    "CM_PLACE": ("PLACE", "LOCATION", "SITE", "CENTER", "IDC"),
    "CM_RACK_LOC": ("RACK_LOC", "RACK", "RACK_NAME", "RACK_POSITION"),
    "CM_OWN_DPT_ID": ("OWN_DPT_ID", "DEPT_ID", "DEPT_CD", "OWNER_DEPT_ID"),
    "CM_OWN_EMP_ID": ("OWN_EMP_ID", "OWNER_ID", "OWNER_EMP_ID"),
    "CM_USER_EMP_ID": ("USER_EMP_ID", "USER_ID"),
    "CM_USER_DPT_ID": ("USER_DPT_ID", "USER_DEPT_ID"),
    "CM_WOR_MNG_EMP_ID": ("WOR_MNG_EMP_ID", "MANAGER_ID", "MNG_EMP_ID", "ADMIN_ID"),
    "CM_TAKIN_DTTM": ("TAKIN_DTTM", "TAKE_IN_DATE", "INTRO_DATE", "INSTALL_DATE"),
    "CM_REG_DTTM": ("REG_DTTM", "REG_DATE", "CREATED_AT", "CREATE_DTTM", "INSERT_DTTM"),
    "CM_MOD_DTTM": ("MOD_DTTM", "MOD_DATE", "UPDATED_AT", "UPDATE_DTTM", "CHG_DTTM"),
    "CM_DESCR": ("DESCR", "DESCRIPTION", "REMARK", "REMARKS", "NOTE"),
    "CM_DESCR2": ("DESCR2", "DESCRIPTION2", "REMARK2", "NOTE2"),
    "OS_EOS_DATE": ("EOS_DATE", "EOS_DT", "OS_EOS", "OS_EOS_DT", "SUPPORT_END_DATE", "VENDOR_EOS_DATE"),
}

# Placeholders the collector substitutes at run time, keeping the generated file
# in sync when the CPU/EOS field settings change.
_CPU_TOKEN = "${CPU_FIELD}"
_EOS_TOKEN = "${EOS_FIELD}"


def _normalize(name: str) -> str:
    return "".join(character for character in str(name).upper() if character.isalnum())


def _without_cm_prefix(name: str) -> str:
    normalized = _normalize(name)
    return normalized[2:] if normalized.startswith("CM") and len(normalized) > 2 else normalized


def logical_columns(itsm_cfg: Mapping[str, Any]) -> list[str]:
    """Full ordered list of logical columns, including the configurable fields."""
    cpu_field = str(itsm_cfg.get("cpu_compare_field") or "CM_CPU_CORE_CNT").strip().upper()
    memory_field = str(itsm_cfg.get("memory_field") or "CM_MEMORY").strip().upper()
    eos_field = str(itsm_cfg.get("os_eos_field") or "OS_EOS_DATE").strip().upper()
    ordered: list[str] = []
    for name in BASE_LOGICAL_COLUMNS:
        ordered.append(name)
        if name == "CM_OS_VERSION":
            ordered.extend([cpu_field, memory_field])
    ordered.append(eos_field)
    seen: set[str] = set()
    return [name for name in ordered if not (name in seen or seen.add(name))]


def _auto_match(logical: str, available: Mapping[str, str]) -> str | None:
    """Find the source column for one logical column, most reliable rule first."""
    candidates = [logical, *COLUMN_ALIASES.get(logical, ())]
    for candidate in candidates:
        exact = available.get(_normalize(candidate))
        if exact:
            return exact
    for candidate in candidates:
        loose = available.get(f"~{_without_cm_prefix(candidate)}")
        if loose:
            return loose
    return None


def build_asset_query(
    *,
    source_columns: Iterable[str],
    itsm_cfg: Mapping[str, Any],
    asset_source: str = "",
    overrides: Mapping[str, str] | None = None,
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return the generated SQL plus the mapping report for the admin screen."""
    available_list = [str(name).strip().upper() for name in source_columns if str(name).strip()]
    available: dict[str, str] = {}
    for name in available_list:
        available.setdefault(_normalize(name), name)
        available.setdefault(f"~{_without_cm_prefix(name)}", name)

    override_map: dict[str, str] = {}
    for logical, actual in (overrides or {}).items():
        logical_name = str(logical).strip().upper()
        actual_name = str(actual).strip().upper()
        if not logical_name or not actual_name:
            continue
        if actual_name not in available_list:
            raise ValueError(f"{logical_name}에 지정한 컬럼이 테이블에 없습니다: {actual_name}")
        override_map[logical_name] = validate_oracle_identifier(actual_name, "column_name")

    cpu_field = str(itsm_cfg.get("cpu_compare_field") or "CM_CPU_CORE_CNT").strip().upper()
    eos_field = str(itsm_cfg.get("os_eos_field") or "OS_EOS_DATE").strip().upper()
    alias_tokens = {cpu_field: _CPU_TOKEN, eos_field: _EOS_TOKEN}

    mapping: list[dict[str, Any]] = []
    select_lines: list[str] = []
    for logical in logical_columns(itsm_cfg):
        validate_oracle_identifier(logical, "logical_column")
        source = override_map.get(logical) or _auto_match(logical, available)
        alias = alias_tokens.get(logical, logical)
        if source is None:
            select_lines.append(f"    NULL AS {alias}")
            match_type = "MISSING"
        elif source == logical:
            select_lines.append(f"    {alias}" if alias == logical else f"    {source} AS {alias}")
            match_type = "EXACT"
        else:
            select_lines.append(f"    {source} AS {alias}")
            match_type = "OVERRIDE" if logical in override_map else "MAPPED"
        mapping.append(
            {
                "logical_column": logical,
                "source_column": source,
                "match_type": match_type,
                "required": logical in REQUIRED_LOGICAL_COLUMNS or logical in alias_tokens,
            }
        )

    stamp = (generated_at or dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "-- 자동 생성 파일: 관리 화면의 Oracle 자산 테이블 찾기에서 생성되었습니다.",
        f"-- 생성 시각: {stamp}",
        f"-- 대상 테이블: {asset_source or '${ASSET_SOURCE}'}",
        "-- 원본 테이블에 없는 컬럼은 NULL로 조회하여 필수 컬럼 검증을 통과시킵니다.",
        "-- 실제 컬럼으로 교체하거나 WHERE 조건을 추가하려면 이 파일을 직접 수정하세요.",
    ]
    missing = [item["logical_column"] for item in mapping if item["match_type"] == "MISSING"]
    if missing:
        header.append(f"-- 매핑되지 않은 컬럼: {', '.join(missing)}")
    filter_column = available.get(_normalize("CM_CAT_CD")) or available.get("~CATCD")
    body = ["SELECT", ",\n".join(select_lines), "FROM ${ASSET_SOURCE}"]
    if filter_column:
        body.append(f"-- WHERE {filter_column} IN ('HW0101', 'HW0102', 'HW0104')")
    sql = "\n".join(header) + "\n" + "\n".join(body) + "\n"

    return {
        "sql": sql,
        "mapping": mapping,
        "missing_columns": missing,
        "missing_required_columns": [
            item["logical_column"]
            for item in mapping
            if item["match_type"] == "MISSING" and item["required"]
        ],
        "matched_count": sum(1 for item in mapping if item["match_type"] != "MISSING"),
        "total_count": len(mapping),
    }
