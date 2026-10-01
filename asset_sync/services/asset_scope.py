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

from ..normalization.code_maps import PLACE
from datetime import datetime
from typing import Any, Iterable, Mapping

#: 자산으로 셀 상태. 운영(CMSTA010) 과 대기(CMSTA050).
#: 미사용(CMSTA020)·매각폐기(CMSTA060) 는 자산 대수에 넣지 않는다.
ACTIVE_STATUS = ("CMSTA010", "CMSTA050")

PHYSICAL_CATEGORY = "CMSVRCATCD010"
LOGICAL_CATEGORY = "CMSVRCATCD020"

#: 설치 위치 컬럼. 코드(CMPLACE010/020)로 오거나 글("63DR")로 온다.
#: 코드는 글자로 찾을 수 없으므로 코드표를 먼저 맞춰 본다.
DEFAULT_LOCATION_FIELD = "CM_PLACE"
DEFAULT_PLACE_CODES = dict(PLACE)
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
    #: 코드 -> IDC/DR. dict 는 얼릴 수 없으므로 튜플 짝으로 둔다.
    place_code_items: tuple[tuple[str, str], ...] = tuple(sorted(DEFAULT_PLACE_CODES.items()))
    dr_keywords: tuple[str, ...] = DEFAULT_DR_KEYWORDS
    idc_keywords: tuple[str, ...] = DEFAULT_IDC_KEYWORDS
    default_location: str = DEFAULT_LOCATION
    eosl_fields: tuple[str, ...] = DEFAULT_EOSL_FIELDS
    exclude_when_all_empty: tuple[str, ...] = DEFAULT_EXCLUDE_WHEN_ALL_EMPTY
    active_status: tuple[str, ...] = ACTIVE_STATUS
    os_groups: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_OS_GROUPS

    @property
    def place_codes(self) -> dict[str, str]:
        return {str(code).upper(): value for code, value in self.place_code_items}

    def public(self) -> dict[str, Any]:
        return {
            "location_field": self.location_field,
            "place_codes": self.place_codes,
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

    # 코드표는 설정으로 덮어쓸 수 있다. ITSM 마다 코드가 다를 수 있다.
    codes = dict(DEFAULT_PLACE_CODES)
    configured = section.get("place_codes")
    if isinstance(configured, Mapping):
        codes.update({str(k).upper(): str(v).upper() for k, v in configured.items()})

    return Criteria(
        location_field=str(section.get("location_field") or DEFAULT_LOCATION_FIELD).upper(),
        place_code_items=tuple(sorted(codes.items())),
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

    CM_PLACE 는 두 가지 모양으로 온다.

    * **코드** -- ``CMPLACE010`` = IDC, ``CMPLACE020`` = DR. 코드는 글자로
      찾을 수 없다(``CMPLACE020`` 안에는 "DR" 도 "IDC" 도 없다). 그래서 코드를
      **먼저** 정확히 맞춰 본다. 이걸 안 하면 전부 기본값인 IDC 로 떨어진다.
    * **글로 적힌 위치** -- "63DR", "IDC-2F", "DR센터". 앞뒤에 무엇이 붙어 오므로
      값이 같은지가 아니라 **조각이 들어 있는지** 본다. DR 을 먼저 본다 --
      두 조각이 함께 있으면("IDC-DR-2F") DR 쪽이 맞다.
    """
    text = str(raw.get(criteria.location_field) or "").strip()
    if not text:
        return criteria.default_location

    # 1) 코드로 온 경우. 공백·대소문자만 맞춰 정확히 비교한다.
    coded = criteria.place_codes.get(text.upper().replace(" ", ""))
    if coded:
        return coded

    upper = text.upper()
    for token in criteria.dr_keywords:
        if str(token).upper() in upper:
            return "DR"
    for token in criteria.idc_keywords:
        if str(token).upper() in upper:
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
        # ITSM 원본 전체. 엑셀로 뽑을 때 전 컬럼이 필요하다. 화면에 내려보낼 때는
        # 응답이 커지므로 떼어낸다(라우트에서 pop).
        item["raw"] = dict(raw)
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


# ── 엑셀로 한꺼번에 제외하기 ─────────────────────────────────────────────
# 200~300 건을 화면에서 하나씩 체크하는 것은 현실적이지 않다. 목록을 엑셀로
# 받아 거기에 표시하고 다시 올리면 한 번에 적용한다.

#: 자산을 알아보는 열. 이 중 하나가 있으면 그 열을 키로 쓴다.
KEY_HEADERS = ("CM_ID", "자산번호", "자산ID", "ASSET_KEY", "자산키")

#: 무엇을 할지 적는 열.
ACTION_HEADERS = ("처리", "제외", "제외여부", "ACTION", "MODE")

#: 사유를 적는 열.
REASON_HEADERS = ("제외 사유", "사유", "비고", "REASON", "NOTE")

#: 적어 넣을 수 있는 말. 사람이 빨리 쓰는 표기까지 받는다.
ACTION_WORDS: dict[str, str] = {
    "제외": "EXCLUDE", "EXCLUDE": "EXCLUDE", "Y": "EXCLUDE", "O": "EXCLUDE",
    "1": "EXCLUDE", "TRUE": "EXCLUDE", "V": "EXCLUDE",
    "포함": "INCLUDE", "재포함": "INCLUDE", "INCLUDE": "INCLUDE",
    "자동": "AUTO", "AUTO": "AUTO", "해제": "AUTO", "취소": "AUTO",
}

#: 엑셀에서 고를 수 있게 넣어 주는 값. 300 줄을 손으로 쓰면 오타가 난다.
ACTION_CHOICES = ("제외", "포함", "자동")


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _find_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]]:
    """머리글 줄과 열 위치를 찾는다.

    우리가 내보낸 파일은 머리글이 두 줄이고(한글 이름 / 원래 컬럼명) 넷째·다섯째
    줄에 있다. 담당자가 직접 만든 파일은 첫 줄일 수도 있다. 그래서 줄 번호를
    가정하지 않고 자산번호 열이 보이는 줄을 찾는다.
    """
    for index, row in enumerate(rows[:12]):
        texts = [_cell_text(cell).upper() for cell in row]
        if not any(text in {h.upper() for h in KEY_HEADERS} for text in texts):
            continue
        found: dict[str, int] = {}
        for position, text in enumerate(texts):
            if "key" not in found and text in {h.upper() for h in KEY_HEADERS}:
                found["key"] = position
            elif "action" not in found and text in {h.upper() for h in ACTION_HEADERS}:
                found["action"] = position
            elif "reason" not in found and text in {h.upper() for h in REASON_HEADERS}:
                found["reason"] = position
        if "key" in found:
            return index, found
    raise AssetScopeError(
        "자산번호 열을 찾지 못했습니다. 머리글에 "
        + " 또는 ".join(KEY_HEADERS[:2])
        + " 가 있어야 합니다."
    )


def _sheet_rows(path: Any) -> list[list[Any]]:
    from pathlib import Path

    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        workbook = load_workbook(target, read_only=True, data_only=True)
        try:
            # 첫 시트를 본다. 우리가 내보낸 파일은 시트가 하나다.
            sheet = workbook.worksheets[0]
            return [list(row) for row in sheet.iter_rows(values_only=True)]
        finally:
            workbook.close()
    if suffix in {".csv", ".txt", ".tsv"}:
        import csv

        delimiter = "\t" if suffix == ".tsv" else ","
        with target.open("r", encoding="utf-8-sig", newline="") as stream:
            return [list(row) for row in csv.reader(stream, delimiter=delimiter)]
    raise AssetScopeError("지원 파일은 XLSX, CSV, TSV 입니다.")


def read_bulk_sheet(path: Any, default_mode: str = "") -> dict[str, Any]:
    """올린 파일을 읽어 (자산번호, 처리) 목록을 만든다.

    ``처리`` 열이 비어 있으면 ``default_mode`` 를 쓴다. 엑셀에서 걸러 남긴
    목록을 그대로 올리고 화면에서 "제외" 를 고르는 쪽이 빠른 경우가 많다.

    알 수 없는 말이 적혀 있으면 조용히 넘기지 않고 돌려준다. 300 줄 중 몇 줄이
    오타라서 빠졌다는 것을 모르면 더 나쁘다.
    """
    default_mode = str(default_mode or "").upper()
    if default_mode and default_mode not in (*MODES, "AUTO"):
        raise AssetScopeError(f"기본 처리는 EXCLUDE, INCLUDE, AUTO 중 하나여야 합니다: {default_mode}")

    rows = _sheet_rows(path)
    header_index, columns = _find_header(rows)
    key_at = columns["key"]
    action_at = columns.get("action")
    reason_at = columns.get("reason")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    unknown: list[dict[str, Any]] = []
    blank = 0
    duplicated: list[str] = []

    for line, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        key = _cell_text(row[key_at] if key_at < len(row) else "")
        # 머리글 두 줄짜리 파일의 둘째 줄("(집계값)" 등)은 자료가 아니다.
        if not key or key.upper() in {h.upper() for h in KEY_HEADERS} or key.startswith("("):
            continue
        written = _cell_text(row[action_at]) if action_at is not None and action_at < len(row) else ""
        mode = ACTION_WORDS.get(written.upper()) if written else default_mode
        if written and mode is None:
            unknown.append({"row": line, "asset_key": key, "value": written})
            continue
        if not mode:
            blank += 1
            continue
        if key in seen:
            duplicated.append(key)
            continue
        seen.add(key)
        items.append({
            "asset_key": key,
            "mode": mode,
            "reason": _cell_text(row[reason_at]) if reason_at is not None and reason_at < len(row) else "",
        })

    return {
        "items": items,
        "header_row": header_index + 1,
        "has_action_column": action_at is not None,
        "counts": {
            "total": len(items),
            **{mode: sum(1 for item in items if item["mode"] == mode) for mode in (*MODES, "AUTO")},
            "blank": blank,
            "unknown": len(unknown),
            "duplicated": len(duplicated),
        },
        "unknown": unknown[:50],
        "duplicated": duplicated[:50],
        "action_choices": list(ACTION_CHOICES),
    }


def apply_bulk(
    repository: Any,
    source: str,
    items: list[Mapping[str, Any]],
    *,
    reason: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    """읽어 들인 목록을 처리별로 묶어 한 번에 적용한다."""
    applied: dict[str, int] = {}
    for mode in (*MODES, "AUTO"):
        chosen = [item for item in items if str(item.get("mode")) == mode]
        if not chosen:
            continue
        # 줄마다 사유가 다를 수 있다. 사유가 같은 것끼리 묶어 한 번에 넣는다.
        by_reason: dict[str, list[Mapping[str, Any]]] = {}
        for item in chosen:
            by_reason.setdefault(str(item.get("reason") or reason), []).append(item)
        count = 0
        for text, group in by_reason.items():
            count += save_rules(
                repository, source, group, mode=mode, reason=text, updated_by=updated_by
            )
        applied[mode] = count
    return {"applied": applied, "total": sum(applied.values())}
