"""서버 현황과 EOSL 현황.

월간 점검 장표 두 개를 같은 데이터로 만든다. 첨부 양식에서 물리서버 소계(925)와
EOSL 서버 행 합계가 같고, 전체 소계(2,668)와 EOSL OS 행 합계가 같다. 즉 한 번 고른
대상을 두 장표가 함께 쓴다.

**무엇을 셀지는 여기서 정하지 않는다.** ``asset_scope`` 가 정한다. 통합 대시보드·
일간·주간 점검·보고서가 같은 모듈을 쓰므로 화면마다 대수가 달라질 수 없다.

집계 기준을 코드에 묻어두지 않는다. 대수가 ITSM 총 건수와 다를 때 어느 쪽이 틀렸는지
따질 수 있어야 하므로, 무엇을 어떤 기준으로 세고 무엇을 뺐는지 결과에 같이 담는다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from .asset_scope import (
    LOCATIONS,
    LOGICAL_CATEGORY,
    NO_PLAN_YEAR,
    OTHER_GROUP,
    PHYSICAL_CATEGORY,
    AssetScope,
    criteria_from,
    eosl_year,
    os_group,
    parse_year,
)

#: 예전 이름으로 import 하던 곳이 있어 남겨 둔다.
DEFAULT_OS_GROUPS = criteria_from(None).os_groups


class ServerStatusService:
    """ITSM 스냅샷에서 서버 현황·EOSL 현황을 만든다."""

    def __init__(self, config: Any, repository: Any, *, include_all: bool = False) -> None:
        self.config = config
        self.repo = repository
        self.scope = AssetScope.load(config, repository, include_all=include_all)
        self.criteria = self.scope.criteria
        self.include_all = include_all

    # ── 대상 고르기 ─────────────────────────────────────────────────────
    def select(self, snapshot_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(집계 대상, 제외 대상). 제외한 것도 화면에 보여야 하므로 함께 돌려준다."""
        records = self.repo.load_itsm_records(snapshot_id).values()
        return self.scope.split_itsm(records)

    def records(self, snapshot_id: int) -> list[dict[str, Any]]:
        """한 건씩 펼친 전체 목록. 제외된 것도 사유를 달고 들어 있다.

        화면에서 OS·위치를 눌러 그 대수의 실물이 무엇인지 볼 때 쓴다.
        """
        records = self.repo.load_itsm_records(snapshot_id).values()
        return [self.scope.describe_itsm(record) for record in records]

    # ── 서버 현황 ───────────────────────────────────────────────────────
    def _table(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """양식의 한 표. 행은 IDC·DR·계, 열은 OS 묶음과 소계다."""
        columns = [name for name, _ in self.criteria.os_groups] + [OTHER_GROUP]
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
        records = list(self.repo.load_itsm_records(snapshot_id).values())
        return {
            "criteria": self.describe_criteria(),
            "counts": {"selected": len(included), "excluded": len(excluded), "physical": len(physical)},
            "scope": self.scope.summary(records),
            "all": {"table": all_table, "delta": self._delta(all_table, previous_all)},
            "physical": {"table": physical_table, "delta": self._delta(physical_table, previous_physical)},
            # 제외한 대상은 목록으로 보여야 한다. 왜 빠졌는지 따지려면 판단에 쓴
            # 값(CM_OS·CM_OS_VERSION·CM_EOL_DT)이 비어 있는 것까지 보여야 한다.
            "excluded": {
                "count": len(excluded),
                "reason": self.describe_exclusion(),
                "items": excluded,
            },
        }

    def describe_exclusion(self) -> str:
        fields = ", ".join(self.criteria.exclude_when_all_empty)
        return (
            f"상태가 운영·대기가 아니거나({', '.join(self.criteria.active_status)}) "
            f"{fields} 가 모두 비어 있거나, 수동으로 제외한 자산"
        )

    def describe_criteria(self) -> dict[str, Any]:
        """무엇을 어떤 기준으로 셌는지. 화면에 그대로 보여준다."""
        criteria = self.criteria.public()
        # 예전 화면이 eosl_field(단수) 를 읽는다. 실제로 쓰는 첫 후보를 준다.
        criteria["eosl_field"] = self.criteria.eosl_fields[0] if self.criteria.eosl_fields else ""
        criteria["include_all"] = self.include_all
        return criteria

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
            "criteria": {
                "eosl_field": self.criteria.eosl_fields[0] if self.criteria.eosl_fields else "",
                "eosl_fields": list(self.criteria.eosl_fields),
                "base_year": year, "no_plan_year": NO_PLAN_YEAR,
            },
            "columns": self.eosl_columns(year),
            # 양식에서 '서버' 행은 물리서버, 'OS' 행은 전체서버다.
            "physical": {"label": "서버", **self._eosl_row(physical, year)},
            "all": {"label": "OS", **self._eosl_row(included, year)},
            "diagnosis": self.eosl_diagnosis(included),
        }

    def eosl_diagnosis(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """EOSL 값이 어디서 왔고 어떻게 읽혔는지.

        표가 온통 '계획 없음' 이거나 '미사용' 이면, 그게 실제 데이터인지 컬럼을
        잘못 짚은 것인지 표만 봐서는 알 수 없다. 어느 컬럼에서 몇 건을 읽었고
        원래 값이 어떻게 생겼는지 같이 내려보내 화면에서 바로 확인하게 한다.
        """
        fields: Counter[str] = Counter()
        samples: dict[str, list[str]] = {}
        unreadable: list[str] = []
        for item in items:
            name = item.get("eosl_field") or "(값 없음)"
            fields[name] += 1
            if item.get("eosl_field"):
                bucket = samples.setdefault(name, [])
                text = str(item.get("eosl_value"))
                if len(bucket) < 5 and text not in bucket:
                    bucket.append(text)
                if item.get("eosl_year") is None and len(unreadable) < 10:
                    unreadable.append(text)
        return {
            "by_field": dict(fields),
            "samples": samples,
            # 값은 있는데 연도를 못 읽은 것. 형식이 예상과 다르다는 뜻이다.
            "unreadable_samples": unreadable,
            "checked_fields": list(self.criteria.eosl_fields),
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
