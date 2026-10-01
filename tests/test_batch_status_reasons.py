"""배치가 PARTIAL_SUCCESS 인 이유를 기록하는지 확인한다.

PARTIAL_SUCCESS 는 "아무것도 안 됐다" 가 아니라 "한 군데가 모자라다" 는 뜻이다.
상태만 남기면 무엇이 모자란지 알 수 없어 로그를 뒤져야 한다. 그래서 사유를
배치 기록에 남긴다.

PARTIAL_SUCCESS 가 될 수 있는 경우는 넷뿐이다. AIX(HMC) 는 아직 배치에 들어
있지 않으므로 원인이 될 수 없다 -- 그 성질도 함께 지킨다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asset_sync.config import AppConfig
from asset_sync.db.manager import create_manager
from asset_sync.repositories import AssetRepository
from asset_sync.services.collection_service import CollectionService

REASONS = CollectionService._status_reasons


def test_a_count_drop_is_named_with_both_numbers():
    reasons = REASONS(
        {
            "itsm": {"status": "PARTIAL_SUCCESS", "baseline": {
                "warning": True, "critical": False,
                "previous_count": 2400, "current_count": 1500, "ratio": 0.625,
            }},
            "vcenter": {"status": "SUCCESS"},
        },
        {"status": "SUCCESS"},
    )
    assert len(reasons) == 1
    assert reasons[0]["area"] == "ITSM"
    assert reasons[0]["code"] == "COUNT_DROP_WARNING"
    # 몇 건에서 몇 건으로 줄었는지 숫자가 있어야 판단할 수 있다.
    assert "2,400" in reasons[0]["message"]
    assert "1,500" in reasons[0]["message"]


def test_a_failed_vcenter_names_which_one():
    reasons = REASONS(
        {
            "itsm": {"status": "SUCCESS"},
            "vcenter": {"status": "PARTIAL_SUCCESS", "failed_scopes": {
                "vc_0003": "TCP 443 연결 실패",
            }},
        },
        {"status": "SUCCESS"},
    )
    assert reasons[0]["code"] == "SCOPE_FAILED"
    assert "vc_0003" in reasons[0]["message"]
    assert "TCP 443" in reasons[0]["message"]


def test_a_skipped_resource_usage_is_reported_too():
    reasons = REASONS(
        {"itsm": {"status": "SUCCESS"}, "vcenter": {"status": "SUCCESS"}},
        {"status": "SKIPPED", "reason": "정상 vCenter 인벤토리가 없어 건너뜁니다."},
    )
    assert reasons[0]["area"] == "자원사용률"
    assert "건너뜁니다" in reasons[0]["message"]


def test_a_failed_collection_carries_its_error():
    reasons = REASONS(
        {
            "itsm": {"status": "FAILED", "error": "DPY-1001 연결이 닫혔습니다"},
            "vcenter": {"status": "SUCCESS"},
        },
        {"status": "SUCCESS"},
    )
    assert reasons[0]["code"] == "COLLECTION_FAILED"
    assert "DPY-1001" in reasons[0]["message"]


def test_a_clean_run_has_nothing_to_report():
    assert REASONS(
        {"itsm": {"status": "SUCCESS"}, "vcenter": {"status": "SUCCESS"}},
        {"status": "SUCCESS"},
    ) == []


def test_the_reason_is_stored_on_the_batch_record(tmp_path: Path):
    """다음에 열어 봐도 사유가 남아 있어야 한다."""
    config = AppConfig(
        root_dir=tmp_path,
        sqlite_path=Path("data/reasons.db"),
        itsm={"collection_mode": "DEMO", "memory_unit": "GB", "tracked_fields": [], "ignore_fields": []},
        rvtools={"collection_mode": "DEMO", "resource_usage": {"enabled": False},
                 "power_on_value": "poweredon", "hostname_suffixes": []},
        matching={"memory_tolerance_mb": 1},
        quality={"minimum_itsm_records": 1, "minimum_rvtools_records": 1},
    )
    manager = create_manager(config)
    manager.initialize()
    result = CollectionService(config, manager).run_daily(demo=True)

    with manager.connect() as conn:
        batch = AssetRepository(conn).latest_daily_batch()
    stored = json.loads(dict(batch)["metadata_json"])
    assert "status_reasons" in stored
    assert stored["status_reasons"] == result["status_reasons"]
    # 자원사용률을 끈 설정이므로 그 사유가 남는다.
    if result["status"] != "SUCCESS":
        assert stored["status_reasons"], "SUCCESS 가 아니면 사유가 있어야 한다"


def test_aix_is_not_part_of_the_daily_batch():
    """AIX(HMC) 수집은 아직 배치에 없다. PARTIAL_SUCCESS 의 원인이 될 수 없다.

    붙이는 날 이 테스트가 깨진다. 그때 사유 목록에도 AIX 를 넣어야 한다는
    표시가 된다.
    """
    import inspect

    source = inspect.getsource(CollectionService.run_daily)
    assert "hmc" not in source.lower()
    assert "HMCCollector" not in source
