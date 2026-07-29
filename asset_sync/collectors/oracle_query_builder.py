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
import re
from typing import Any, Iterable, Mapping

from ..utils.validation import validate_oracle_identifier

MAX_FILTER_VALUES = 50
MAX_FILTER_VALUE_LENGTH = 128

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


def normalize_filter_values(raw: Any) -> list[str]:
    """Accept a list or a comma/newline separated string of filter code values."""
    if raw is None:
        return []
    items: list[str] = []
    if isinstance(raw, str):
        items = re.split(r"[,\n\r;]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(value) for value in raw]
    else:
        items = [str(raw)]
    values: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value in values:
            continue
        values.append(value)
    return values


def _filter_literal(value: str) -> str:
    """Quote one code value as an Oracle string literal."""
    if len(value) > MAX_FILTER_VALUE_LENGTH:
        raise ValueError(f"필터 코드값이 너무 깁니다({MAX_FILTER_VALUE_LENGTH}자 이내): {value[:40]}…")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError("필터 코드값에 줄바꿈이나 제어문자를 넣을 수 없습니다.")
    return "'" + value.replace("'", "''") + "'"


def build_asset_query(
    *,
    source_columns: Iterable[str],
    itsm_cfg: Mapping[str, Any],
    asset_source: str = "",
    overrides: Mapping[str, Any] | None = None,
    filter_column: str = "",
    filter_values: Any = None,
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return the generated SQL plus the mapping report for the admin screen.

    ``overrides`` maps a logical column to the source column chosen by the
    operator; an empty value forces that column to be selected as NULL. When
    ``filter_column`` is given, its code values become a WHERE ... IN clause.
    """
    available_list = [str(name).strip().upper() for name in source_columns if str(name).strip()]
    available: dict[str, str] = {}
    for name in available_list:
        available.setdefault(_normalize(name), name)
        available.setdefault(f"~{_without_cm_prefix(name)}", name)

    override_map: dict[str, str | None] = {}
    for logical, actual in (overrides or {}).items():
        logical_name = str(logical).strip().upper()
        if not logical_name:
            continue
        actual_name = str(actual or "").strip().upper()
        if not actual_name:
            # An explicit blank means "do not map this column"; it is a real choice,
            # not a missing value, so it must survive as NULL instead of falling back
            # to the automatic match.
            override_map[logical_name] = None
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
        forced = logical in override_map
        source = override_map[logical] if forced else _auto_match(logical, available)
        alias = alias_tokens.get(logical, logical)
        if source is None:
            select_lines.append(f"    NULL AS {alias}")
            match_type = "MISSING"
        elif source == logical:
            select_lines.append(f"    {alias}" if alias == logical else f"    {source} AS {alias}")
            match_type = "OVERRIDE" if forced else "EXACT"
        else:
            select_lines.append(f"    {source} AS {alias}")
            match_type = "OVERRIDE" if forced else "MAPPED"
        mapping.append(
            {
                "logical_column": logical,
                "source_column": source,
                "match_type": match_type,
                "forced": forced,
                "required": logical in REQUIRED_LOGICAL_COLUMNS or logical in alias_tokens,
            }
        )

    selected_filter = str(filter_column or "").strip().upper()
    values = normalize_filter_values(filter_values)
    where_line = ""
    if selected_filter:
        if selected_filter not in available_list:
            raise ValueError(f"조회 조건 컬럼이 테이블에 없습니다: {selected_filter}")
        selected_filter = validate_oracle_identifier(selected_filter, "filter_column")
        if not values:
            raise ValueError("조회 조건 컬럼을 선택하면 코드값을 1개 이상 입력해야 합니다.")
        if len(values) > MAX_FILTER_VALUES:
            raise ValueError(f"조회 조건 코드값은 최대 {MAX_FILTER_VALUES}개까지 지정할 수 있습니다.")
        where_line = f"WHERE {selected_filter} IN ({', '.join(_filter_literal(value) for value in values)})"
    elif values:
        raise ValueError("조회 조건 코드값을 사용하려면 조건 컬럼을 함께 선택해야 합니다.")

    stamp = (generated_at or dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "-- 자동 생성 파일: 관리 화면의 Oracle 자산 테이블 찾기에서 생성되었습니다.",
        f"-- 생성 시각: {stamp}",
        f"-- 대상 테이블: {asset_source or '${ASSET_SOURCE}'}",
        "-- 원본 테이블에 없는 컬럼은 NULL로 조회하여 필수 컬럼 검증을 통과시킵니다.",
        "-- 컬럼 매핑과 조회 조건은 화면에서 다시 지정할 수 있으며 이 파일을 직접 수정해도 됩니다.",
    ]
    missing = [item["logical_column"] for item in mapping if item["match_type"] == "MISSING"]
    if missing:
        header.append(f"-- 매핑되지 않은 컬럼: {', '.join(missing)}")
    body = ["SELECT", ",\n".join(select_lines), "FROM ${ASSET_SOURCE}"]
    if where_line:
        body.append(where_line)
    else:
        suggested = available.get(_normalize("CM_CAT_CD")) or available.get("~CATCD")
        header.append(
            f"-- 조회 조건 없음: 대상 테이블 전체 행을 수집합니다."
            + (f" 자산 구분 컬럼 후보: {suggested}" if suggested else "")
        )
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
        "source_columns": available_list,
        "filter_column": selected_filter,
        "filter_values": values,
        "where_clause": where_line,
    }
