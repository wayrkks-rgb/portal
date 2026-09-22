"""무엇을 '실제 자산' 으로 셀지 한 곳에서 정한다.

화면마다 각자 세면 숫자가 달라진다. 실제로 그랬다. 통합 대시보드는 운영·대기
상태만 셌고, 월간 점검은 상태를 안 보는 대신 OS·EOSL 이 모두 빈 것을 뺐다.
위치(IDC/DR) 판정도 세 군데에 서로 다르게 적혀 있었고, 그중 하나는 원본을
읽지 않는 자리에서 원본을 찾아 **항상 IDC** 를 돌려주고 있었다.

그래서 판단을 전부 여기로 모은다. 통합 대시보드·일간·주간·월간 점검·보고서가
모두 이 모듈을 쓰므로, 한 화면의 대수가 다른 화면과 다를 수 없다.

제외 사유는 세 가지이고 순서가 있다.

1. **수동 재포함** -- 사람이 "이건 자산이 맞다" 고 정한 것. 무엇보다 우선한다.
2. **수동 제외** -- 사람이 "이건 자산이 아니다" 고 정한 것.
3. **자동 제외** -- 상태가 운영·대기가 아니거나(폐기·미사용), OS·OS버전·EOSL 이
   모두 비어 있는 것. 실물 서버라면 이 셋이 전부 비어 있을 수 없다.

세 가지를 따로 세어 결과에 담는다. 대수가 ITSM 총 건수와 다를 때 어느 사유로
몇 건이 빠졌는지 보이지 않으면 따질 수가 없다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

#: 자산으로 셀 상태. 운영(CMSTA010) 과 대기(CMSTA050).
#: 미사용(CMSTA020)·매각폐기(CMSTA060) 는 자산 대수에 넣지 않는다.
ACTIVE_STATUS = ("CMSTA010", "CMSTA050")

PHYSICAL_CATEGORY = "CMSVRCATCD010"
LOGICAL_CATEGORY = "CMSVRCATCD020"

#: 설치 위치 컬럼과, 그 안에서 찾을 조각. "63DR" 처럼 앞뒤에 무엇이 붙어 있어도
#: 조각이 들어 있으면 그것으로 본다. DR 을 먼저 본다 -- "IDC-DR" 은 DR 이다.
DEFAULT_LOCATION_FIELD = "CM_PLACE"
DEFAULT_DR_KEYWORDS = ("DR", "재해", "재해복구")
DEFAULT_IDC_KEYWORDS = ("IDC", "본사", "주센터")
DEFAULT_LOCATION = "IDC"

#: EOSL 날짜 컬럼 후보. 앞에서부터 찾아 값이 있는 첫 컬럼을 쓴다. ITSM 마다
#: 컬럼 이름이 달라 하나로 못 박으면 전부 '미사용' 으로 떨어진다.
DEFAULT_EOSL_FIELDS = ("CM_EOL_DT", "OS_EOS_DATE", "CM_EOS_DT", "CM_EOSL_DT")

#: 이 컬럼들이 **모두** 비어 있으면 실물 서버로 보지 않는다.
DEFAULT_EXCLUDE_WHEN_ALL_EMPTY = ("CM_OS", "CM_OS_VERSION", "CM_EOL_DT")

#: OS 묶음. 장표의 열 순서와 같다.
DEFAULT_OS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("HP", ("HP-UX",)),
    ("IBM", ("AIX",)),
    ("Linux", ("Linux Redhat", "CentOS", "Rocky", "Ubuntu", "Oracle Linux", "Debian", "SUSE")),
    ("Windows", ("WINDOWS",)),
)
OTHER_GROUP = "기타"
LOCATIONS = ("IDC", "DR")

#: 계획 없음으로 볼 연도.
NO_PLAN_YEAR = 9999

#: 제외 사유 코드. 화면 문구와 함께 쓴다.
REASON_LABELS = {
    "STATUS": "상태가 운영·대기가 아님",
    "EMPTY": "OS·OS버전·EOSL 이 모두 비어 있음",
    "MANUAL": "수동 제외",
}

SOURCES = ("ITSM", "RVTOOLS")
MODES = ("EXCLUDE", "INCLUDE")


def _blank(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    return text == "" or text.lower() in {"-", "nan", "none", "null"}


@dataclass(frozen=True)
class Criteria:
    """집계 기준. 설정에서 만들고, 화면에 그대로 보여준다."""

    location_field: str = DEFAULT_LOCATION_FIELD
    dr_keywords: tuple[str, ...] = DEFAULT_DR_KEYWORDS
    idc_keywords: tuple[str, ...] = DEFAULT_IDC_KEYWORDS
    default_location: str = DEFAULT_LOCATION
    eosl_fields: tuple[str, ...] = DEFAULT_EOSL_FIELDS
    exclude_when_all_empty: tuple[str, ...] = DEFAULT_EXCLUDE_WHEN_ALL_EMPTY
    active_status: tuple[str, ...] = ACTIVE_STATUS
    os_groups: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_OS_GROUPS

    def public(self) -> dict[str, Any]:
        return {
            "location_field": self.location_field,
            "dr_keywords": list(self.dr_keywords),
            "idc_keywords": list(self.idc_keywords),
            "default_location": self.default_location,
            "eosl_fields": list(self.eosl_fields),
            "exclude_when_all_empty": list(self.exclude_when_all_empty),
            "active_status": list(self.active_status),
            "os_groups": {name: list(tokens) for name, tokens in self.os_groups},
            "other_group": OTHER_GROUP,
            "physical_code": PHYSICAL_CATEGORY,
            "logical_code": LOGICAL_CATEGORY,
            "no_plan_year": NO_PLAN_YEAR,
        }


def criteria_from(config: Any) -> Criteria:
    section = dict(getattr(config, "server_status", None) or {})
    groups = section.get("os_groups")
    if isinstance(groups, Mapping) and groups:
        os_groups = tuple(
            (str(name), tuple(str(token) for token in tokens or ()))
            for name, tokens in groups.items()
        )
    else:
        os_groups = DEFAULT_OS_GROUPS

    # eosl_field(단수) 를 쓰던 설정도 그대로 받는다. 후보 목록 맨 앞에 놓는다.
    fields = section.get("eosl_fields")
    if not fields:
        single = section.get("eosl_field")
        fields = [single] if single else list(DEFAULT_EOSL_FIELDS)
    ordered: list[str] = []
    for name in list(fields) + list(DEFAULT_EOSL_FIELDS):
        upper = str(name or "").strip().upper()
        if upper and upper not in ordered:
            ordered.append(upper)

    return Criteria(
        location_field=str(section.get("location_field") or DEFAULT_LOCATION_FIELD).upper(),
        dr_keywords=tuple(str(t) for t in (section.get("dr_keywords") or DEFAULT_DR_KEYWORDS)),
        idc_keywords=tuple(str(t) for t in (section.get("idc_keywords") or DEFAULT_IDC_KEYWORDS)),
        default_location=str(section.get("default_location") or DEFAULT_LOCATION).upper(),
        eosl_fields=tuple(ordered),
        exclude_when_all_empty=tuple(
            str(n).upper() for n in
            (section.get("exclude_when_all_empty") or DEFAULT_EXCLUDE_WHEN_ALL_EMPTY)
        ),
        active_status=tuple(
            str(c).upper() for c in (section.get("active_status") or ACTIVE_STATUS)
        ),
        os_groups=os_groups,
    )


# ── 판정 ────────────────────────────────────────────────────────────────
def location(raw: Mapping[str, Any], criteria: Criteria) -> str:
    """설치 위치를 IDC/DR 로 가른다.

    값이 "63DR", "IDC-2F", "DR센터" 처럼 앞뒤에 무엇이 붙어 온다. 그래서 값이
    같은지 보지 않고 **조각이 들어 있는지** 본다. DR 을 먼저 본다 -- 두 조각이
    함께 있으면 DR 쪽이 맞다.
    """
    text = str(raw.get(criteria.location_field) or "").upper()
    if not text:
        return criteria.default_location
    for token in criteria.dr_keywords:
        if str(token).upper() in text:
            return "DR"
    for token in criteria.idc_keywords:
        if str(token).upper() in text:
            return "IDC"
    return criteria.default_location


def eosl_source(raw: Mapping[str, Any], criteria: Criteria) -> tuple[str | None, Any]:
    """값이 들어 있는 첫 EOSL 컬럼과 그 값. 없으면 (None, None)."""
    for name in criteria.eosl_fields:
        value = raw.get(name)
        if not _blank(value):
            return name, value
    return None, None


def eosl_year(raw: Mapping[str, Any], criteria: Criteria) -> int | None:
    """EOSL 값에서 연도만 뽑는다. 연도를 못 읽으면 None."""
    _, value = eosl_source(raw, criteria)
    return parse_year(value)


def parse_year(value: Any) -> int | None:
    """'2030-12-31', '20301231', '2030.12.31', '31/12/2030' 에서 연도를 뽑는다.

    네 자리 숫자를 아무 데서나 집으면 '1231' 같은 조각을 연도로 읽는다. 그래서
    연도로 말이 되는 범위(1990~9999)에 드는 것만 고른다.
    """
    if _blank(value):
        return None
    text = str(value).strip()
    for match in re.finditer(r"\d{4}", text):
        year = int(match.group())
        if 1990 <= year <= NO_PLAN_YEAR:
            return year
    return None


def os_group(os_family: Any, criteria: Criteria) -> str:
    text = str(os_family or "").strip().lower()
    if not text:
        return OTHER_GROUP
    for name, tokens in criteria.os_groups:
        for token in tokens:
            if str(token).strip().lower() in text:
                return name
    return OTHER_GROUP


def is_physical(record: Mapping[str, Any]) -> bool:
    return str(record.get("server_category_code") or "") == PHYSICAL_CATEGORY


def itsm_key(record: Mapping[str, Any]) -> str:
    raw = record.get("raw") or {}
    return str(record.get("cm_id") or raw.get("CM_ID") or record.get("asset_key") or "").strip()


def vcenter_key(record: Mapping[str, Any]) -> str:
    return str(record.get("asset_key") or record.get("vm_uuid") or record.get("vm_name") or "").strip()


# ── 한 건의 판정 결과 ────────────────────────────────────────────────────
@dataclass
class Decision:
    included: bool
    reason: str = ""          # 제외 사유 코드. 포함이면 빈 값
    manual: bool = False      # 사람이 정한 것인가
    note: str = ""            # 수동일 때 적어 둔 사유


# ── 대상 고르기 ──────────────────────────────────────────────────────────
@dataclass
class AssetScope:
    """실제 자산으로 셀 것을 고른다.

    ``include_all`` 이 참이면 아무것도 빼지 않는다. 원본 전체를 뽑아야 할 때
    쓰며, 그때도 각 건의 판정 사유는 그대로 담아 어떤 것이 평소 빠지는지 알 수
    있게 한다.
    """

    criteria: Criteria
    rules: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    include_all: bool = False

    @classmethod
    def load(cls, config: Any, repository: Any, *, include_all: bool = False) -> "AssetScope":
        return cls(criteria_from(config), load_rules(repository), include_all=include_all)

    def rule_for(self, source: str, key: str) -> dict[str, Any] | None:
        return self.rules.get((str(source).upper(), str(key)))

    def decide_itsm(self, record: Mapping[str, Any]) -> Decision:
        rule = self.rule_for("ITSM", itsm_key(record))
        if rule and str(rule.get("mode")) == "INCLUDE":
            return Decision(True, manual=True, note=str(rule.get("reason") or ""))
        if rule and str(rule.get("mode")) == "EXCLUDE":
            return Decision(self.include_all, "MANUAL", manual=True, note=str(rule.get("reason") or ""))
        raw = record.get("raw") or {}
        status = str(record.get("status_code") or "").upper()
        if status not in self.criteria.active_status:
            return Decision(self.include_all, "STATUS")
        if all(_blank(raw.get(name)) for name in self.criteria.exclude_when_all_empty):
            return Decision(self.include_all, "EMPTY")
        return Decision(True)

    def decide_vcenter(self, record: Mapping[str, Any]) -> Decision:
        """vCenter 쪽은 자동 규칙이 없다. 템플릿·SRM 은 수집 단계에서 이미 빠진다."""
        rule = self.rule_for("RVTOOLS", vcenter_key(record))
        if rule and str(rule.get("mode")) == "EXCLUDE":
            return Decision(self.include_all, "MANUAL", manual=True, note=str(rule.get("reason") or ""))
        return Decision(True)

    def describe_itsm(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """한 건을 화면에 쓸 모양으로 편다. 제외 판단 근거 값까지 담는다."""
        raw = record.get("raw") or {}
        decision = self.decide_itsm(record)
        eosl_field, eosl_value = eosl_source(raw, self.criteria)
        item = {
            "asset_key": itsm_key(record),
            "cm_id": itsm_key(record),
            "hostname": record.get("normalized_hostname") or raw.get("CM_HOSTNAME"),
            "primary_ip": record.get("primary_ip") or raw.get("CM_IP"),
            "service_name": raw.get("CM_NAME"),
            "location": location(raw, self.criteria),
            "physical": is_physical(record),
            "status_code": record.get("status_code"),
            "os_family": record.get("os_family"),
            "os_group": os_group(record.get("os_family"), self.criteria),
            "os_version": record.get("os_version") or raw.get("CM_OS_VERSION"),
            "cpu_cores": record.get("cpu_cores"),
            "memory_mb": record.get("memory_mb"),
            "eosl_field": eosl_field,
            "eosl_value": eosl_value,
            "eosl_year": parse_year(eosl_value),
            "included": decision.included,
            "exclude_reason": decision.reason,
            "exclude_label": REASON_LABELS.get(decision.reason, ""),
            "manual": decision.manual,
            "manual_note": decision.note,
        }
        # 왜 빠졌는지 따지려면 판단에 쓴 값이 보여야 한다. 비어 있는 것도 보여준다.
        item["exclude_fields"] = {
            name: raw.get(name) for name in self.criteria.exclude_when_all_empty
        }
        item["place"] = raw.get(self.criteria.location_field)
        return item

    def split_itsm(self, records: Iterable[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for record in records:
            item = self.describe_itsm(record)
            (included if item["included"] else excluded).append(item)
        return included, excluded

    def excluded_itsm_keys(self, records: Iterable[Mapping[str, Any]]) -> set[str]:
        return {
            itsm_key(record) for record in records
            if not self.decide_itsm(record).included
        }

    def summary(self, records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """무엇을 몇 건 뺐는지. 화면이 이 값을 그대로 보여준다."""
        reasons: Counter[str] = Counter()
        total = selected = 0
        for record in records:
            total += 1
            decision = self.decide_itsm(record)
            if decision.included and not (self.include_all and decision.reason):
                selected += 1
            if decision.reason:
                reasons[decision.reason] += 1
        return {
            "snapshot_total": total,
            "selected": selected,
            "excluded": sum(reasons.values()),
            "by_reason": {
                code: {"label": REASON_LABELS.get(code, code), "count": count}
                for code, count in reasons.items()
            },
            "include_all": self.include_all,
            "criteria": self.criteria.public(),
        }


# ── 수동 규칙 읽고 쓰기 ──────────────────────────────────────────────────
def load_rules(repository: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """저장된 수동 제외·재포함 규칙. 표가 아직 없으면 빈 값이다."""
    try:
        rows = repository.conn.execute(
            "SELECT source, asset_key, mode, reason, updated_by, updated_at FROM asset_exclusion"
        ).fetchall()
    except Exception:
        return {}
    return {
        (str(row["source"]).upper(), str(row["asset_key"])): dict(row)
        for row in rows
    }


class AssetScopeError(ValueError):
    pass


def save_rules(
    repository: Any,
    source: str,
    items: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    reason: str = "",
    updated_by: str = "",
) -> int:
    """여러 건을 한 번에 제외하거나 되돌린다.

    ``mode`` 가 ``AUTO`` 면 저장된 규칙을 지운다. 지우면 자동 판정으로 돌아간다.
    """
    source = str(source or "").upper()
    if source not in SOURCES:
        raise AssetScopeError(f"source 는 {', '.join(SOURCES)} 중 하나여야 합니다: {source}")
    mode = str(mode or "").upper()
    if mode not in (*MODES, "AUTO"):
        raise AssetScopeError(f"mode 는 EXCLUDE, INCLUDE, AUTO 중 하나여야 합니다: {mode}")

    keys = []
    for item in items:
        if isinstance(item, Mapping):
            keys.append((str(item.get("asset_key") or item.get("cm_id") or "").strip(), item))
        else:
            keys.append((str(item).strip(), {}))
    keys = [(key, extra) for key, extra in keys if key]
    if not keys:
        raise AssetScopeError("대상이 없습니다.")

    now = datetime.now().isoformat()
    changed = 0
    for key, extra in keys:
        if mode == "AUTO":
            repository.conn.execute(
                "DELETE FROM asset_exclusion WHERE source=? AND asset_key=?", (source, key)
            )
            changed += 1
            continue
        repository.conn.execute(
            "DELETE FROM asset_exclusion WHERE source=? AND asset_key=?", (source, key)
        )
        repository.conn.execute(
            "INSERT INTO asset_exclusion(source, asset_key, mode, reason, hostname,"
            " primary_ip, service_name, updated_by, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (source, key, mode, reason or None, extra.get("hostname"), extra.get("primary_ip"),
             extra.get("service_name"), updated_by or None, now),
        )
        changed += 1
    repository.conn.commit()
    return changed


def list_rules(repository: Any, source: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM asset_exclusion"
    params: list[Any] = []
    if source:
        sql += " WHERE source=?"
        params.append(str(source).upper())
    sql += " ORDER BY source, asset_key"
    try:
        return [dict(row) for row in repository.conn.execute(sql, params or None).fetchall()]
    except Exception:
        return []
