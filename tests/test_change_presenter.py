"""변경 이벤트가 화면에서 읽을 수 있는 형태로 나오는지 확인한다."""

from __future__ import annotations

import json

from asset_sync.services.change_presenter import (
    MAX_VALUE_LENGTH,
    label_field,
    label_value,
    present,
    summarize_record,
)

ITSM_ASSET = {
    "CM_ID": "CM000123", "CM_NAME": "결제서버", "CM_HOSTNAME": "PRD-PAY-01",
    "CM_IP": "10.20.30.40", "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.6",
    "CM_CPU_CORE_CNT": "8", "CM_MEMORY": "32", "CM_SVR_CAT_CD": "CMSVRCATCD020",
    "CM_STA_CD": "CMSTA010", "CM_DESCR": "x" * 500,
}


def test_a_code_becomes_a_label_but_keeps_the_code():
    """담당자가 ITSM 화면에서 코드로 찾는 일이 있어 코드도 남긴다."""
    assert label_value("CMSTA060") == "매각/폐기(CMSTA060)"
    assert label_value("CMSVRCATCD020") == "논리(CMSVRCATCD020)"


def test_a_plain_value_is_left_alone():
    assert label_value("PRD-PAY-01") == "PRD-PAY-01"
    assert label_value(None) == ""


def test_field_names_become_korean():
    assert label_field("CM_STA_CD") == "상태"
    assert label_field("memory_mb") == "메모리"
    assert label_field("CM_UNKNOWN_FIELD") == "CM_UNKNOWN_FIELD"


def test_a_created_asset_is_summarised_not_dumped():
    """원본 전체를 한 줄로 뿌리면 어떤 자산인지 알 수 없고 표가 옆으로 길어진다."""
    event = present({
        "source": "ITSM", "event_type": "ITSM_ASSET_CREATED", "asset_key": "CM000123",
        "field_name": None, "old_value": None,
        "new_value": json.dumps(ITSM_ASSET, ensure_ascii=False),
    })
    summary = event["new_display"]
    assert "PRD-PAY-01" in summary
    assert "10.20.30.40" in summary
    assert "운영(CMSTA010)" in summary
    assert "논리(CMSVRCATCD020)" in summary
    assert len(summary) < 200, f"요약이 아니라 덤프다: {summary}"
    # 원본은 사라지지 않고 접힌 자리에 남는다.
    assert event["detail"]["new"]["CM_DESCR"] == ITSM_ASSET["CM_DESCR"]


def test_a_removed_vm_is_summarised_too():
    event = present({
        "source": "RVTOOLS", "event_type": "RV_REMOVED", "asset_key": "vc1|vm-1",
        "field_name": None, "new_value": None,
        "old_value": json.dumps({
            "vm_name": "PAY01", "primary_ip": "10.1.1.1", "os_family": "Linux",
            "cpus": 4, "memory_mb": 32768, "power_state": "poweredOn", "esxi_host": "esx-01",
        }, ensure_ascii=False),
    })
    assert "PAY01" in event["old_display"]
    assert "32GB" in event["old_display"], event["old_display"]


def test_a_status_change_shows_both_labels():
    event = present({
        "source": "ITSM", "event_type": "ITSM_STATUS_TO_DISPOSED", "asset_key": "CM000123",
        "field_name": "CM_STA_CD", "old_value": "CMSTA010", "new_value": "CMSTA060",
    })
    assert event["field_label"] == "상태"
    assert event["old_display"] == "운영(CMSTA010)"
    assert event["new_display"] == "매각/폐기(CMSTA060)"


def test_memory_values_carry_their_unit():
    """4 와 4096 을 같은 화면에서 보면 무엇이 GB 인지 알 수 없다."""
    itsm = present({"source": "ITSM", "event_type": "ITSM_MEMORY_CHANGED",
                    "field_name": "CM_MEMORY", "old_value": "16", "new_value": "32"})
    assert itsm["old_display"] == "16GB" and itsm["new_display"] == "32GB"

    vcenter = present({"source": "RVTOOLS", "event_type": "RV_MEMORY_CHANGED",
                       "field_name": "memory_mb", "old_value": "16384", "new_value": "32768"})
    assert vcenter["old_display"] == "16GB" and vcenter["new_display"] == "32GB"


def test_a_very_long_value_is_shortened_and_kept():
    event = present({"source": "ITSM", "event_type": "ITSM_FIELD_CHANGED",
                     "field_name": "CM_DESCR", "old_value": "가" * 400, "new_value": "나"})
    assert len(event["old_display"]) == MAX_VALUE_LENGTH + 1
    assert event["detail"]["old"] == "가" * 400


def test_the_original_values_are_never_lost():
    original = {"source": "ITSM", "event_type": "ITSM_CPU_CHANGED",
                "field_name": "CM_CPU_CORE_CNT", "old_value": "4", "new_value": "8"}
    event = present(original)
    assert event["old_value"] == "4" and event["new_value"] == "8"


def test_summarising_skips_fields_that_are_empty():
    assert summarize_record("ITSM", {"CM_HOSTNAME": "H1", "CM_IP": "", "CM_OS": None}) == "H1"
