"""변경 이벤트를 사람이 읽을 수 있는 형태로 바꾼다.

저장된 변경 이벤트는 기계가 쓰기 좋은 값이다. 코드값(CMSTA060), 필드명(CM_STA_CD),
그리고 생성·삭제일 때는 원본 레코드 전체가 JSON 한 줄로 들어 있다. 그대로 화면에
뿌리면 "그래서 뭐가 바뀐 건데?" 를 알 수 없고, 표가 옆으로 한없이 길어진다.

여기서 한 번만 다듬어 모든 화면이 같은 표현을 쓰게 한다.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..normalization.code_maps import (
    ASSET_CATEGORY,
    ASSET_STATUS,
    ENVIRONMENT,
    NETWORK,
    OS_CODES,
    SERVER_CATEGORY,
)

#: 코드값을 라벨로 바꾸기 위한 통합 사전. 코드 체계가 서로 겹치지 않아 하나로 합쳐도 된다.
CODE_LABELS: dict[str, str] = {
    **ASSET_STATUS, **SERVER_CATEGORY, **ENVIRONMENT,
    **ASSET_CATEGORY, **NETWORK, **OS_CODES,
}

#: 필드명을 화면 문구로. 여기 없는 필드는 원래 이름을 그대로 쓴다.
FIELD_LABELS: dict[str, str] = {
    "CM_ID": "자산번호", "CM_NAME": "자산명", "CM_HOSTNAME": "호스트명",
    "CM_IP": "IP", "CM_SUB_IP": "보조 IP", "CM_OS": "OS", "CM_OS_VERSION": "OS 버전",
    "CM_CPU_CORE_CNT": "CPU 코어", "CM_CPU_CNT": "CPU 수", "CM_MEMORY": "메모리",
    "CM_STA_CD": "상태", "CM_SVR_CAT_CD": "물리/논리", "CM_CAT_CD": "자산 구분",
    "CM_OWN_CAT_CD": "환경", "CM_NET_CD": "망 구분", "CM_PLACE": "위치",
    "CM_RACK_LOC": "랙 위치", "CM_OWN_EMP_ID": "소유자", "CM_OWN_DPT_ID": "소유 부서",
    "CM_USER_EMP_ID": "사용자", "CM_USER_DPT_ID": "사용 부서",
    "CM_WOR_MNG_EMP_ID": "운영 담당", "CM_SERIAL_NO": "시리얼",
    "CM_MAKE_NAME": "제조사", "CM_MODEL_NAME": "모델", "OS_EOS_DATE": "OS 지원종료일",
    "vm_name": "VM 이름", "normalized_hostname": "호스트명", "primary_ip": "IP",
    "cpus": "vCPU", "memory_mb": "메모리", "os_family": "OS", "os_version": "OS 버전",
    "power_state": "전원", "esxi_host": "ESXi 호스트", "vcenter": "vCenter",
    "datacenter": "데이터센터", "cluster_name": "클러스터",
}

#: 생성·삭제 이벤트에서 요약에 쓸 필드. 순서가 곧 표시 순서다.
SUMMARY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "ITSM": (
        ("CM_HOSTNAME", ""), ("CM_IP", ""), ("CM_OS", ""), ("CM_OS_VERSION", ""),
        ("CM_CPU_CORE_CNT", "Core"), ("CM_MEMORY", "GB"),
        ("CM_SVR_CAT_CD", ""), ("CM_STA_CD", ""),
    ),
    "RVTOOLS": (
        ("vm_name", ""), ("primary_ip", ""), ("os_family", ""), ("os_version", ""),
        ("cpus", "vCPU"), ("memory_mb", "MB"), ("power_state", ""), ("esxi_host", ""),
    ),
}

#: 원본 전체를 담는 이벤트. 여기만 요약이 필요하다.
WHOLE_RECORD_EVENTS = frozenset({
    "ITSM_ASSET_CREATED", "ITSM_RECORD_REMOVED", "RV_NEW", "RV_REMOVED",
})

#: 값이 이보다 길면 화면에서 잘라 보여준다. 원문은 detail 로 따로 내려보낸다.
MAX_VALUE_LENGTH = 120


def label_field(name: Any) -> str:
    text = str(name or "").strip()
    return FIELD_LABELS.get(text, text)


def label_value(value: Any) -> str:
    """코드값이면 라벨을, 아니면 원래 값을 돌려준다.

    라벨을 붙일 때 코드도 같이 남긴다. 담당자가 ITSM 화면에서 코드로 찾는 일이 있다.
    """
    if value is None:
        return ""
    text = str(value).strip()
    label = CODE_LABELS.get(text)
    return f"{label}({text})" if label else text


def _memory_display(value: Any, unit: str) -> str:
    """메모리는 단위를 붙여야 4 와 4096 을 헷갈리지 않는다."""
    text = str(value or "").strip()
    if not text:
        return ""
    if unit == "MB":
        try:
            return f"{int(float(text)) // 1024:,}GB"
        except (TypeError, ValueError):
            return f"{text}MB"
    return f"{text}{unit}" if unit else text


def summarize_record(source: str, raw: Mapping[str, Any]) -> str:
    """생성·삭제된 자산이 무엇인지 한 줄로 만든다."""
    parts: list[str] = []
    for field, unit in SUMMARY_FIELDS.get(str(source).upper(), ()):
        value = raw.get(field)
        if value is None or str(value).strip() == "":
            continue
        if unit in {"GB", "MB", "Core", "vCPU"}:
            text = _memory_display(value, unit) if unit in {"GB", "MB"} else f"{value}{unit}"
        else:
            text = label_value(value)
        if text:
            parts.append(text)
    return " · ".join(parts)


def _parse(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, str) or not value.strip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _display(source: str, field: Any, value: Any) -> str:
    if value is None:
        return ""
    field_name = str(field or "").strip()
    if field_name in {"CM_MEMORY"}:
        return _memory_display(value, "GB")
    if field_name == "memory_mb":
        return _memory_display(value, "MB")
    return label_value(value)


def present(event: Mapping[str, Any]) -> dict[str, Any]:
    """이벤트 하나에 화면용 표현을 덧붙인다. 원래 값은 그대로 남긴다."""
    result = dict(event)
    source = str(event.get("source") or "").upper()
    field = event.get("field_name")
    result["field_label"] = label_field(field) if field else ""

    if str(event.get("event_type") or "") in WHOLE_RECORD_EVENTS:
        # 원본 전체가 들어 있는 자리다. 요약을 만들고 원문은 detail 로 옮긴다.
        for side, key in (("old_value", "old"), ("new_value", "new")):
            raw = _parse(event.get(side))
            if raw is None:
                continue
            result[f"{key}_display"] = summarize_record(source, raw)
            result.setdefault("detail", {})[key] = raw
        result.setdefault("old_display", _display(source, field, event.get("old_value")))
        result.setdefault("new_display", _display(source, field, event.get("new_value")))
        return result

    result["old_display"] = _display(source, field, event.get("old_value"))
    result["new_display"] = _display(source, field, event.get("new_value"))
    for key in ("old_display", "new_display"):
        if len(result[key]) > MAX_VALUE_LENGTH:
            result.setdefault("detail", {})[key.replace("_display", "")] = result[key]
            result[key] = result[key][:MAX_VALUE_LENGTH] + "…"
    return result


def present_all(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [present(event) for event in events]
