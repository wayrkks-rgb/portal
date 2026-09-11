"""서버 현황과 EOSL 현황.

월간 점검 장표 두 개를 같은 데이터로 만든다. 첨부 양식에서 물리서버 소계(925)와
EOSL 서버 행 합계가 같고, 전체 소계(2,668)와 EOSL OS 행 합계가 같다. 즉 한 번 고른
대상을 두 장표가 함께 쓴다. 여기서 한 번만 고른다.

집계 기준을 코드에 묻어두지 않는다. 대수가 ITSM 총 건수와 다를 때 어느 쪽이 틀렸는지
따질 수 있어야 하므로, 무엇을 어떤 기준으로 세고 무엇을 뺐는지 결과에 같이 담는다.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable, Mapping

#: OS 묶음. 양식의 열 순서와 같다. 여기 어디에도 맞지 않으면 '기타' 다.
#: 값은 normalize_os 가 내는 os_family 다(OS_CODES 로 코드를 풀고 난 뒤의 값).
DEFAULT_OS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("HP", ("HP-UX",)),
    ("IBM", ("AIX",)),
    ("Linux", ("Linux Redhat", "CentOS", "Rocky", "Ubuntu", "Oracle Linux", "Debian", "SUSE")),
    ("Windows", ("WINDOWS",)),
)
OTHER_GROUP = "기타"

#: 설치 위치. 양식의 행 순서와 같다.
LOCATIONS = ("IDC", "DR")

#: 물리/논리 구분 코드.
PHYSICAL_CATEGORY = "CMSVRCATCD010"
LOGICAL_CATEGORY = "CMSVRCATCD020"

#: 서버 현황에서 뺄지 판단할 컬럼. 이 값들이 **모두** 비어 있으면 서버로 보지 않는다.
DEFAULT_EXCLUDE_WHEN_ALL_EMPTY = ("CM_OS", "CM_OS_VERSION", "CM_EOL_DT")

#: EOSL 날짜 컬럼. 연도만 본다.
DEFAULT_EOSL_FIELD = "CM_EOL_DT"

#: 설치 위치 컬럼과, DR 로 볼 값의 조각.
DEFAULT_LOCATION_FIELD = "CM_PLACE"
DEFAULT_DR_KEYWORDS = ("DR", "재해", "재해복구")

#: 계획 없음으로 볼 연도.
NO_PLAN_YEAR = 9999


def _settings(config: Any) -> dict[str, Any]:
    """설정에서 서버 현황 기준을 꺼낸다. 없으면 기본값을 쓴다."""
    section = dict(getattr(config, "server_status", None) or {})
    groups = section.get("os_groups")
    if isinstance(groups, Mapping) and groups:
        os_groups = tuple(
            (str(name), tuple(str(token) for token in tokens or ()))
            for name, tokens in groups.items()
        )
    else:
        os_groups = DEFAULT_OS_GROUPS
    return {
        "exclude_when_all_empty": tuple(
            str(name).upper() for name in
            (section.get("exclude_when_all_empty") or DEFAULT_EXCLUDE_WHEN_ALL_EMPTY)
        ),
        "eosl_field": str(section.get("eosl_field") or DEFAULT_EOSL_FIELD).upper(),
        "location_field": str(section.get("location_field") or DEFAULT_LOCATION_FIELD).upper(),
        "dr_keywords": tuple(str(token) for token in (section.get("dr_keywords") or DEFAULT_DR_KEYWORDS)),
        "os_groups": os_groups,
    }


def _blank(value: Any) -> bool:
    text = str(value or "").strip()
    return text == "" or text.lower() in {"-", "nan", "none", "null"}


def os_group(os_family: Any, groups: Iterable[tuple[str, tuple[str, ...]]]) -> str:
    """OS 를 양식의 열로 묶는다. 어디에도 없으면 기타다."""
    text = str(os_family or "").strip().lower()
    if not text:
        return OTHER_GROUP
    for name, tokens in groups:
        for token in tokens:
            if token.strip().lower() in text:
                return name
    return OTHER_GROUP


def eosl_year(value: Any) -> int | None:
    """EOSL 값에서 연도만 뽑는다. 연도를 못 읽으면 None 이다."""
    if _blank(value):
        return None
    match = re.search(r"(\d{4})", str(value))
    return int(match.group(1)) if match else None


class ServerStatusService:
    """ITSM 스냅샷에서 서버 현황·EOSL 현황을 만든다."""

    def __init__(self, config: Any, repository: Any) -> None:
        self.config = config
        self.repo = repository
        self.settings = _settings(config)

    # ── 대상 고르기 ─────────────────────────────────────────────────────
    def _classify(self, record: Mapping[str, Any]) -> dict[str, Any]:
        raw = record.get("raw") or {}
        location = self._location(raw)
        return {
            "cm_id": record.get("CM_ID") or record.get("cm_id"),
            "hostname": record.get("normalized_hostname") or raw.get("CM_HOSTNAME"),
            "primary_ip": record.get("primary_ip") or raw.get("CM_IP"),
            "service_name": raw.get("CM_NAME"),
            "location": location,
            "physical": str(record.get("server_category_code") or "") == PHYSICAL_CATEGORY,
            "os_group": os_group(record.get("os_family"), self.settings["os_groups"]),
            "os_family": record.get("os_family"),
            "eosl_year": eosl_year(raw.get(self.settings["eosl_field"])),
            "eosl_value": raw.get(self.settings["eosl_field"]),
        }

    def _location(self, raw: Mapping[str, Any]) -> str:
        """설치 위치를 IDC/DR 로 가른다. 판단 근거는 설정에 있다."""
        text = str(raw.get(self.settings["location_field"]) or "")
        upper = text.upper()
        for token in self.settings["dr_keywords"]:
            if token.upper() in upper:
                return "DR"
        return "IDC"

    def _is_server(self, record: Mapping[str, Any]) -> bool:
        """서버로 볼지. 기준 컬럼이 모두 비어 있으면 서버가 아니다."""
        raw = record.get("raw") or {}
        return not all(_blank(raw.get(name)) for name in self.settings["exclude_when_all_empty"])

    def select(self, snapshot_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(집계 대상, 제외 대상). 제외한 것도 화면에 보여야 하므로 함께 돌려준다."""
        records = self.repo.load_itsm_records(snapshot_id).values()
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for record in records:
            item = self._classify(record)
            (included if self._is_server(record) else excluded).append(item)
        return included, excluded

    # ── 서버 현황 ───────────────────────────────────────────────────────
    def _table(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """양식의 한 표. 행은 IDC·DR·계, 열은 OS 묶음과 소계다."""
        columns = [name for name, _ in self.settings["os_groups"]] + [OTHER_GROUP]
        counts: dict[str, Counter[str]] = {location: Counter() for location in LOCATIONS}
        for item in items:
            counts[item["location"]][item["os_group"]] += 1
        rows: dict[str, dict[str, int]] = {}
        for location in LOCATIONS:
            row = {column: int(counts[location][column]) for column in columns}
            row["소계"] = sum(row.values())
            rows[location] = row
        total = {column: sum(rows[location][column] for location in LOCATIONS) for column in columns}
        total["소계"] = sum(total.values())
        rows["계"] = total
        return {"columns": columns + ["소계"], "rows": rows}

    def _delta(self, current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        """전월 대비 증감. 양식의 (+8) · (-2) 에 해당한다."""
        if previous is None:
            return {}
        deltas: dict[str, dict[str, int]] = {}
        for row, values in current["rows"].items():
            before = previous["rows"].get(row, {})
            deltas[row] = {
                column: int(values[column]) - int(before.get(column, 0))
                for column in values
            }
        return deltas

    def status(self, snapshot_id: int, previous_snapshot_id: int | None = None) -> dict[str, Any]:
        included, excluded = self.select(snapshot_id)
        physical = [item for item in included if item["physical"]]

        previous_all = previous_physical = None
        if previous_snapshot_id:
            before_included, _ = self.select(previous_snapshot_id)
            previous_all = self._table(before_included)
            previous_physical = self._table([item for item in before_included if item["physical"]])

        all_table = self._table(included)
        physical_table = self._table(physical)
        return {
            "criteria": self.describe_criteria(),
            "counts": {"selected": len(included), "excluded": len(excluded), "physical": len(physical)},
            "all": {"table": all_table, "delta": self._delta(all_table, previous_all)},
            "physical": {"table": physical_table, "delta": self._delta(physical_table, previous_physical)},
            # 제외한 대상은 목록으로 보여야 한다. 빠진 이유를 확인할 수 있어야 하므로
            # 호스트명·IP·업무명을 함께 담는다.
            "excluded": {
                "count": len(excluded),
                "reason": f"{', '.join(self.settings['exclude_when_all_empty'])} 가 모두 비어 있음",
                "items": [
                    {key: item[key] for key in ("cm_id", "hostname", "primary_ip", "service_name", "location")}
                    for item in excluded
                ],
            },
        }

    def describe_criteria(self) -> dict[str, Any]:
        """무엇을 어떤 기준으로 셌는지. 화면에 그대로 보여준다."""
        return {
            "exclude_when_all_empty": list(self.settings["exclude_when_all_empty"]),
            "eosl_field": self.settings["eosl_field"],
            "location_field": self.settings["location_field"],
            "dr_keywords": list(self.settings["dr_keywords"]),
            "os_groups": {name: list(tokens) for name, tokens in self.settings["os_groups"]},
            "other_group": OTHER_GROUP,
            "physical_code": PHYSICAL_CATEGORY,
            "logical_code": LOGICAL_CATEGORY,
        }

    # ── 신규·삭제 상세 ──────────────────────────────────────────────────
    def movements(self, snapshot_id: int, previous_snapshot_id: int) -> dict[str, Any]:
        """양식의 '자산 현황 변동 내역'.

        위치 → 물리/논리 → OS 묶음 순서로 센다. 양식이 그 순서로 읽히기 때문이다.
        """
        current, _ = self.select(snapshot_id)
        before, _ = self.select(previous_snapshot_id)
        current_by_id = {item["cm_id"]: item for item in current}
        before_by_id = {item["cm_id"]: item for item in before}

        created = [current_by_id[key] for key in current_by_id.keys() - before_by_id.keys()]
        removed = [before_by_id[key] for key in before_by_id.keys() - current_by_id.keys()]
        return {"created": self._breakdown(created), "removed": self._breakdown(removed)}

    def _breakdown(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        tree: dict[str, dict[str, Counter[str]]] = {
            location: {"논리서버": Counter(), "물리서버": Counter()} for location in LOCATIONS
        }
        for item in items:
            kind = "물리서버" if item["physical"] else "논리서버"
            tree[item["location"]][kind][item["os_group"]] += 1
        locations: dict[str, Any] = {}
        for location in LOCATIONS:
            kinds = {
                kind: {"total": sum(counter.values()), "os": dict(counter)}
                for kind, counter in tree[location].items()
            }
            locations[location] = {"total": sum(k["total"] for k in kinds.values()), "kinds": kinds}
        return {"total": len(items), "locations": locations}

    # ── EOSL 현황 ───────────────────────────────────────────────────────
    def eosl(self, snapshot_id: int, today: date | None = None) -> dict[str, Any]:
        """양식의 EOSL 표. 대상은 서버 현황과 같아야 한다.

        연도만 본다. 9999 는 계획 없음이고, 연도를 못 읽으면 미사용으로 센다.
        """
        year = (today or date.today()).year
        included, _ = self.select(snapshot_id)
        physical = [item for item in included if item["physical"]]
        return {
            "criteria": {"eosl_field": self.settings["eosl_field"], "base_year": year,
                         "no_plan_year": NO_PLAN_YEAR},
            "columns": self.eosl_columns(year),
            # 양식에서 '서버' 행은 물리서버, 'OS' 행은 전체서버다.
            "physical": {"label": "서버", **self._eosl_row(physical, year)},
            "all": {"label": "OS", **self._eosl_row(included, year)},
        }

    @staticmethod
    def eosl_columns(year: int) -> list[str]:
        return [f"{year}년 이전", f"{year}년", f"{year + 1}년", f"{year + 2}년 이상", "계획 없음", "미사용"]

    def _eosl_row(self, items: list[dict[str, Any]], year: int) -> dict[str, Any]:
        buckets: Counter[str] = Counter()
        columns = self.eosl_columns(year)
        for item in items:
            value = item["eosl_year"]
            if value is None:
                buckets["미사용"] += 1
            elif value >= NO_PLAN_YEAR:
                buckets["계획 없음"] += 1
            elif value < year:
                buckets[f"{year}년 이전"] += 1
            elif value == year:
                buckets[f"{year}년"] += 1
            elif value == year + 1:
                buckets[f"{year + 1}년"] += 1
            else:
                buckets[f"{year + 2}년 이상"] += 1
        counts = {column: int(buckets[column]) for column in columns}
        return {"total": len(items), "counts": counts}
