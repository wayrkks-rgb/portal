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
MAX_FILTERS = 10

#: 조회 조건에 쓸 수 있는 비교. 값 개수 규칙이 서로 달라 함께 둔다.
#: 자유 입력 SQL 을 받지 않는 이유는, 조회 전용 계정이라도 문장을 그대로 넣게 하면
#: 주석·부질의로 원래 의도와 다른 조회가 만들어질 수 있기 때문이다.
FILTER_OPERATORS: dict[str, dict[str, str]] = {
    "IN": {"label": "다음 값 중 하나", "arity": "many"},
    "NOT IN": {"label": "다음 값 제외", "arity": "many"},
    "=": {"label": "같음", "arity": "one"},
    "!=": {"label": "다르다", "arity": "one"},
    "LIKE": {"label": "패턴 일치 (% 사용)", "arity": "one"},
    "NOT LIKE": {"label": "패턴 제외 (% 사용)", "arity": "one"},
    "IS NULL": {"label": "값이 비어 있음", "arity": "none"},
    "IS NOT NULL": {"label": "값이 있음", "arity": "none"},
}
DEFAULT_FILTER_OPERATOR = "IN"

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


def normalize_filters(
    raw_filters: Any = None,
    *,
    filter_column: str = "",
    filter_values: Any = None,
) -> list[dict[str, Any]]:
    """조건 목록을 하나의 형태로 맞춘다.

    조건이 하나뿐이던 시절의 filter_column/filter_values 도 같이 받는다. 저장된
    설정과 화면이 섞여 들어오므로, 어느 쪽이 와도 같은 결과가 나와야 한다.
    """
    items: list[Mapping[str, Any]] = []
    if isinstance(raw_filters, Mapping):
        items = [raw_filters]
    elif isinstance(raw_filters, (list, tuple)):
        items = [item for item in raw_filters if isinstance(item, Mapping)]
    if not items and str(filter_column or "").strip():
        items = [{"column": filter_column, "operator": DEFAULT_FILTER_OPERATOR, "values": filter_values}]
    elif not items and normalize_filter_values(filter_values):
        # 컬럼 없이 값만 온 경우. 아래 검증에서 잡히도록 그대로 넘긴다.
        items = [{"column": "", "operator": DEFAULT_FILTER_OPERATOR, "values": filter_values}]

    normalized: list[dict[str, Any]] = []
    for item in items:
        column = str(item.get("column") or "").strip().upper()
        operator = str(item.get("operator") or DEFAULT_FILTER_OPERATOR).strip().upper()
        values = normalize_filter_values(item.get("values"))
        if not column and not values:
            continue  # 빈 줄은 화면에서 흔하다. 조건으로 세지 않는다.
        normalized.append({"column": column, "operator": operator, "values": values})
    return normalized


def _condition_sql(condition: Mapping[str, Any], available_list: list[str]) -> str:
    """조건 하나를 SQL 한 줄로 만든다. 컬럼·비교·값 개수를 모두 확인한다."""
    column = str(condition.get("column") or "").strip().upper()
    operator = str(condition.get("operator") or DEFAULT_FILTER_OPERATOR).strip().upper()
    values = normalize_filter_values(condition.get("values"))

    if not column:
        raise ValueError("조회 조건의 컬럼을 선택하세요.")
    if column not in available_list:
        raise ValueError(f"조회 조건 컬럼이 테이블에 없습니다: {column}")
    column = validate_oracle_identifier(column, "filter_column")
    if operator not in FILTER_OPERATORS:
        raise ValueError(
            f"지원하지 않는 조회 조건입니다: {operator} "
            f"(사용 가능: {', '.join(FILTER_OPERATORS)})"
        )

    arity = FILTER_OPERATORS[operator]["arity"]
    if arity == "none":
        if values:
            raise ValueError(f"{column} {operator} 에는 코드값을 넣지 않습니다.")
        return f"{column} {operator}"
    if not values:
        raise ValueError(f"{column} {operator} 에 쓸 코드값을 1개 이상 입력하세요.")
    if len(values) > MAX_FILTER_VALUES:
        raise ValueError(f"조회 조건 코드값은 조건당 최대 {MAX_FILTER_VALUES}개까지 지정할 수 있습니다.")
    if arity == "one":
        if len(values) > 1:
            raise ValueError(
                f"{column} {operator} 에는 코드값을 1개만 넣을 수 있습니다. "
                f"여러 개를 쓰려면 IN 을 고르세요."
            )
        return f"{column} {operator} {_filter_literal(values[0])}"
    return f"{column} {operator} ({', '.join(_filter_literal(value) for value in values)})"


def build_asset_query(
    *,
    source_columns: Iterable[str],
    itsm_cfg: Mapping[str, Any],
    asset_source: str = "",
    overrides: Mapping[str, Any] | None = None,
    filters: Any = None,
    filter_column: str = "",
    filter_values: Any = None,
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return the generated SQL plus the mapping report for the admin screen.

    ``overrides`` maps a logical column to the source column chosen by the
    operator; an empty value forces that column to be selected as NULL.
    ``filters`` is a list of ``{column, operator, values}`` conditions joined
    with AND; ``filter_column``/``filter_values`` is the older single-condition
    form and still works.
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

    conditions = normalize_filters(filters, filter_column=filter_column, filter_values=filter_values)
    if len(conditions) > MAX_FILTERS:
        raise ValueError(f"조회 조건은 최대 {MAX_FILTERS}개까지 지정할 수 있습니다.")
    # 조건은 모두 만족해야 한다(AND). OR 가 필요하면 한 컬럼에 IN 을 쓰면 된다.
    condition_lines = [_condition_sql(condition, available_list) for condition in conditions]
    where_line = ""
    if condition_lines:
        where_line = "WHERE " + "\n  AND ".join(condition_lines)

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
        "filters": conditions,
        # 조건이 하나뿐이던 시절의 화면·설정과 계속 맞추기 위해 함께 돌려준다.
        "filter_column": conditions[0]["column"] if len(conditions) == 1 else "",
        "filter_values": conditions[0]["values"] if len(conditions) == 1 else [],
        "where_clause": where_line,
    }
