"""통합 대시보드 패널 수집.

한 화면에 여러 모듈의 결과를 함께 보여준다. 두 가지가 지켜져야 한다.

1. 부분 실패 — 모듈 하나가 죽어도 나머지 패널은 그려진다. 화면 전체가 오류로
   바뀌면 안 된다.
2. 지연 — 모듈을 순차로 호출하면 응답시간이 합산된다. 병렬로 호출하고 전체
   예산을 넘기면 남은 것은 TIMEOUT 으로 처리한다.

각 모듈은 아래 형태의 패널 스펙을 돌려준다. 통합 웹은 도메인 지식 없이 이 스펙만
보고 그리므로, 새 대메뉴가 늘어도 통합 웹 렌더링 코드를 고치지 않는다.

    {
      "title": "용량 현황",
      "metrics": [{"label": "총 VM", "value": 1234, "unit": "대", "state": "info"}],
      "table": {"columns": ["호스트", "CPU%"], "rows": [["esxi-01", 72]]},
      "note": "07:00 수집 기준",
      "updated_at": "2026-07-30T07:00:00"
    }
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Mapping

from .client import STATUS_SKIPPED, STATUS_SUCCESS, STATUS_TIMEOUT, ModuleClient
from .registry import ModuleDefinition, ModuleRegistry

LOGGER = logging.getLogger(__name__)

MAX_METRICS = 8
MAX_TABLE_ROWS = 50
MAX_TABLE_COLUMNS = 12

# 내부 모듈의 패널 제공자. 통합 웹 안에 있는 기능은 HTTP 를 거치지 않는다.
LocalPanelProvider = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
_LOCAL_PROVIDERS: dict[str, LocalPanelProvider] = {}


def register_local_panel(module_id: str, provider: LocalPanelProvider) -> None:
    _LOCAL_PROVIDERS[str(module_id)] = provider


def clear_local_panels() -> None:
    _LOCAL_PROVIDERS.clear()


def normalize_panel(payload: Any) -> dict[str, Any]:
    """모듈이 보낸 스펙을 화면이 신뢰할 수 있는 형태로 다듬는다.

    모듈은 각 팀이 만들기 때문에 크기 제한과 타입 정리는 통합 웹이 해야 한다.
    """
    source = payload if isinstance(payload, Mapping) else {}
    metrics: list[dict[str, Any]] = []
    for item in list(source.get("metrics") or [])[:MAX_METRICS]:
        if not isinstance(item, Mapping):
            continue
        metrics.append(
            {
                "label": str(item.get("label") or ""),
                "value": item.get("value"),
                "unit": str(item.get("unit") or ""),
                "state": str(item.get("state") or "info").lower(),
            }
        )

    table_source = source.get("table") if isinstance(source.get("table"), Mapping) else {}
    columns = [str(column) for column in list(table_source.get("columns") or [])[:MAX_TABLE_COLUMNS]]
    rows: list[list[Any]] = []
    for row in list(table_source.get("rows") or [])[:MAX_TABLE_ROWS]:
        if isinstance(row, Mapping):
            rows.append([row.get(column) for column in columns])
        elif isinstance(row, (list, tuple)):
            rows.append(list(row)[: len(columns)] if columns else list(row))

    return {
        "title": str(source.get("title") or ""),
        "metrics": metrics,
        "table": {"columns": columns, "rows": rows} if columns else None,
        "note": str(source.get("note") or ""),
        "updated_at": source.get("updated_at"),
    }


def _panel_envelope(module: ModuleDefinition, status: str, **extra: Any) -> dict[str, Any]:
    envelope = {
        "module_id": module.id,
        "module_name": module.name,
        "icon": module.icon,
        "location": "LOCAL" if module.is_local else "REMOTE",
        "status": status,
        "panel": None,
        "error": None,
        "elapsed_ms": 0,
    }
    envelope.update(extra)
    return envelope


class PanelAggregator:
    """여러 모듈의 패널을 병렬로 모은다."""

    def __init__(self, registry: ModuleRegistry, client: ModuleClient) -> None:
        self.registry = registry
        self.client = client

    def _collect_one(
        self,
        module: ModuleDefinition,
        user: Mapping[str, Any],
        params: Mapping[str, Any],
        timeout: float | None = None,
        permission: str = "VIEW",
    ) -> dict[str, Any]:
        started = time.monotonic()
        if module.is_local:
            provider = _LOCAL_PROVIDERS.get(module.id)
            if provider is None:
                return _panel_envelope(
                    module, STATUS_SKIPPED,
                    error="내부 모듈에 등록된 패널 제공자가 없습니다.",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            try:
                payload = provider(user, params)
            except Exception as exc:
                LOGGER.exception("내부 패널 생성 실패: %s", module.id)
                return _panel_envelope(
                    module, "FAILED", error=str(exc),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            return _panel_envelope(
                module, STATUS_SUCCESS, panel=normalize_panel(payload),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        response = self.client.call(
            module.id, module.panel_path, user=user, params=params, timeout=timeout, permission=permission
        )
        if not response.ok:
            return _panel_envelope(
                module, response.status, error=response.error,
                http_status=response.http_status, elapsed_ms=response.elapsed_ms,
            )
        return _panel_envelope(
            module, STATUS_SUCCESS, panel=normalize_panel(response.data),
            http_status=response.http_status, elapsed_ms=response.elapsed_ms,
        )

    def collect(
        self,
        user: Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        module_ids: list[str] | None = None,
        granted: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # 명시 부여를 반영해 접근 가능한 모듈만 모은다.
        pairs = self.registry.accessible(user, granted)
        if module_ids:
            wanted = {str(item).strip().lower() for item in module_ids}
            pairs = [pair for pair in pairs if pair[0].id in wanted]
        modules = [module for module, _ in pairs]
        permissions = {module.id: permission for module, permission in pairs}
        if not modules:
            return {"panels": [], "budget_seconds": self.registry.dashboard_budget, "degraded": False}

        request_params = dict(params or {})
        budget = self.registry.dashboard_budget
        started = time.monotonic()
        panels: list[dict[str, Any]] = []
        # 호출별 타임아웃을 예산으로 묶는다. 그러지 않으면 모듈 하나의 긴 타임아웃이
        # 전체 응답을 붙잡고, 남겨진 스레드가 예산을 넘겨서까지 살아 있게 된다.
        pool = ThreadPoolExecutor(max_workers=min(8, len(modules)))
        try:
            futures = {
                pool.submit(
                    self._collect_one, module, user, request_params, budget,
                    permissions.get(module.id, "VIEW"),
                ): module
                for module in modules
            }
            for future, module in futures.items():
                remaining = budget - (time.monotonic() - started)
                try:
                    panels.append(future.result(timeout=max(0.05, remaining)))
                except FutureTimeout:
                    # 전체 예산을 넘겼다. 이미 받은 패널은 그대로 살린다.
                    panels.append(
                        _panel_envelope(
                            module, STATUS_TIMEOUT,
                            error=f"통합 대시보드 응답 예산({budget:.0f}초)을 초과했습니다.",
                        )
                    )
                except Exception as exc:
                    LOGGER.exception("패널 수집 실패: %s", module.id)
                    panels.append(_panel_envelope(module, "FAILED", error=str(exc)))
        finally:
            # wait=True 면 이미 예산을 넘긴 호출이 끝날 때까지 응답이 지연된다.
            pool.shutdown(wait=False, cancel_futures=True)

        order = {module.id: index for index, module in enumerate(modules)}
        panels.sort(key=lambda item: order.get(item["module_id"], 99))
        return {
            "panels": panels,
            "budget_seconds": budget,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "degraded": any(panel["status"] != STATUS_SUCCESS for panel in panels),
            "counts": {
                "total": len(panels),
                "success": sum(1 for panel in panels if panel["status"] == STATUS_SUCCESS),
                "failed": sum(1 for panel in panels if panel["status"] not in (STATUS_SUCCESS, STATUS_SKIPPED)),
                "skipped": sum(1 for panel in panels if panel["status"] == STATUS_SKIPPED),
            },
        }
