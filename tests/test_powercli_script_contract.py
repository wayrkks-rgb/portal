"""PowerCLI 스크립트가 파이썬이 읽는 컬럼을 그대로 내놓는지 확인한다.

이 저장소에서는 vCenter 에 붙어 스크립트를 돌려볼 수 없다. 컬럼 이름이 한쪽만
바뀌면 수집은 성공하는데 값이 전부 빈다 -- 오류가 없어서 알아채기 어렵다.
그래서 스크립트 본문과 정규화 코드가 같은 이름을 쓰는지 글자로 맞춰 본다.

속도 때문에 고친 부분(Get-View 일괄 조회, 지연 속성 회피)도 되돌아가지 않게 지킨다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "scripts" / "collect_vcenter_inventory.ps1"
RESOURCE = ROOT / "scripts" / "collect_vcenter_resource_usage.ps1"

#: 정규화 코드(snapshot_service.normalize_rvtools)가 읽는 컬럼.
REQUIRED_COLUMNS = {
    "VM", "Powerstate", "Template", "SRM Placeholder", "DNS Name", "Primary IP Address",
    "CPUs", "Memory", "OS according to the configuration file",
    "OS according to the VMware Tools", "Datacenter", "Cluster", "Host",
    "VM ID", "SMBIOS UUID", "VM UUID", "VI SDK Server",
} | {f"Network #{index}" for index in range(1, 9)}


def script_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_only(path: Path) -> str:
    """주석을 뺀 본문. 주석에 적어둔 설명이 검사에 걸리지 않게 한다.

    'Network #$i' 처럼 문자열 안에 # 가 들어가므로, 줄 맨 앞이 # 인 줄만 버린다.
    """
    return "\n".join(
        line for line in script_text(path).splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_inventory_script_emits_every_column_the_normalizer_reads():
    text = script_text(INVENTORY)
    # 'Network #$i' 는 반복문으로 만든다. 그 형태를 인정하고 나머지를 글자로 확인한다.
    produced = set(re.findall(r"^\s*'([^']+)'\s*=", text, flags=re.MULTILINE))
    produced |= {f"Network #{index}" for index in range(1, 9)} if 'Network #$i' in text else set()
    missing = sorted(REQUIRED_COLUMNS - produced)
    assert not missing, f"스크립트가 내보내지 않는 컬럼이 있습니다(값이 빈 채로 수집됩니다): {missing}"


def test_the_normalizer_and_the_script_agree():
    """정규화 코드가 읽는 이름이 스크립트에 실제로 있는지 양방향으로 확인한다."""
    normalizer = (ROOT / "asset_sync" / "services" / "snapshot_service.py").read_text(encoding="utf-8")
    text = script_text(INVENTORY)
    for column in ("VI SDK Server", "Primary IP Address", "Powerstate",
                   "SRM Placeholder", "OS according to the VMware Tools", "Host"):
        assert column in normalizer, f"정규화 코드에서 {column} 을 찾을 수 없습니다"
        assert f"'{column}'" in text, f"스크립트에서 {column} 을 찾을 수 없습니다"


def test_the_inventory_script_fetches_in_bulk():
    """VM 하나당 왕복이 생기면 통합기 1대에 수십 초가 걸린다."""
    text = script_text(INVENTORY)
    assert "Get-View -Server $viServer -ViewType VirtualMachine" in text, (
        "VM 목록을 Get-View 로 한 번에 받아야 한다"
    )
    assert "-Property $properties" in text, "필요한 속성만 지정해야 전송량이 줄어든다"


def test_the_bulk_path_never_touches_the_lazy_host_property():
    """$vm.VMHost 를 읽으면 VM 하나당 별도 호출이 나간다. 예전 스크립트가 느렸던 이유다."""
    text = script_text(INVENTORY)
    code = code_only(INVENTORY)
    bulk = code[code.index("function Collect-Bulk"):code.index("function Collect-Compat")]
    assert ".VMHost" not in bulk, "일괄 조회 경로에서 지연 속성을 건드리면 의미가 없다"
    assert "Runtime.Host" in bulk, "호스트는 미리 받아둔 MoRef 로 이어야 한다"


def test_a_fallback_path_exists_for_a_site_where_bulk_fails():
    """폐쇄망이라 한 번 반입에 오래 걸린다. 새 방식이 막혀도 수집은 되어야 한다."""
    text = script_text(INVENTORY)
    assert "function Collect-Compat" in text
    assert "VCENTER_COLLECT_MODE" in text
    assert "BULK_FALLBACK" in text, "되돌아간 사실을 로그로 남겨야 원인을 알 수 있다"


def test_the_scripts_report_where_time_went():
    """느릴 때 추측하지 않으려면 단계별 시간이 필요하다."""
    for path in (INVENTORY, RESOURCE):
        text = script_text(path)
        assert "TIMING=" in text, f"{path.name} 에 단계별 시간 출력이 없다"


def test_the_resource_script_asks_for_stats_in_one_go():
    """Get-Stat 을 VM 마다 부르면 VM 수 × 지표 수만큼 왕복이 생긴다.

    VM 1,000대면 2,000번이다. 이것이 자원사용률 수집이 느렸던 가장 큰 이유다.
    """
    text = code_only(RESOURCE)
    calls = re.findall(r"Get-Stat[^\n]*", text)
    # 호스트용 1번, VM용 1번. 그 이상이면 어딘가에서 대상마다 부르고 있다.
    assert len(calls) == 2, f"Get-Stat 호출이 {len(calls)}개입니다: {calls}"
    for call in calls:
        compact = call.replace(" ", "")
        assert "-Stat'cpu.usage.average','mem.usage.average'" in compact, (
            f"두 지표를 한 번에 요청해야 한다: {call}"
        )
        # 단수 변수($vm, $vmHost)를 넘기면 반복문 안에서 부르고 있다는 뜻이다.
        assert not re.search(r"-Entity \$(vm|vmHost)\b", call), f"대상을 한꺼번에 넘겨야 한다: {call}"


def test_the_resource_script_does_not_touch_the_lazy_host_property():
    """VM 의 소속 호스트는 미리 받아둔 표에서 꺼내야 한다."""
    text = code_only(RESOURCE)
    assert "$vm.VMHost" not in text, "$vm.VMHost 를 읽으면 VM 하나당 별도 호출이 나간다"
    assert "$vm.ExtensionData" not in text, "VM 마다 원본 뷰를 파헤치지 않는다"
    assert "Get-View -Server $viServer -ViewType VirtualMachine" in text


@pytest.mark.parametrize("path", [INVENTORY, RESOURCE])
def test_the_script_brackets_are_balanced(path: Path):
    """괄호가 안 맞으면 vCenter 에 붙기도 전에 실패한다. 여기서 잡는다."""
    text = script_text(path)
    depth = {"{": 0, "(": 0, "[": 0}
    closing = {"}": "{", ")": "(", "]": "["}
    index = 0
    in_string: str | None = None
    while index < len(text):
        char = text[index]
        if in_string:
            if char == in_string:
                in_string = None
            elif char == "`":
                index += 1
        elif char in "'\"":
            in_string = char
        elif char == "#":
            index = text.find("\n", index)
            if index == -1:
                break
        elif char in depth:
            depth[char] += 1
        elif char in closing:
            depth[closing[char]] -= 1
            assert depth[closing[char]] >= 0, f"{path.name}: '{char}' 가 더 많습니다"
        index += 1
    assert depth == {"{": 0, "(": 0, "[": 0}, f"{path.name}: 괄호가 맞지 않습니다 {depth}"


def test_the_inventory_script_handles_several_vcenters_in_one_process():
    """PowerCLI 모듈 로딩이 6~15초다. 통합기마다 프로세스를 띄우면 그만큼 곱해진다."""
    text = code_only(INVENTORY)
    assert "VCENTER_COUNT" in text, "여러 대를 받는 입구가 있어야 한다"
    assert "$OutputDir" in text, "통합기별 결과를 담을 폴더를 받아야 한다"
    assert "RESULT=" in text, "통합기별 성공·실패를 한 줄씩 알려야 한다"
    # 모듈 로딩은 딱 한 번. 통합기 반복문 안에 있으면 의미가 없다.
    assert text.count("Import-Module VMware.VimAutomation.Core") == 1
    assert "MODULE_SECONDS=" in text, "모듈 로딩에 걸린 시간을 따로 알려야 판단할 수 있다"


def test_one_vcenter_failing_does_not_abort_the_batch():
    """한 대가 안 되면 나머지도 못 받는다면 묶은 의미가 없다."""
    text = code_only(INVENTORY)
    loop = text[text.index("for ($index = 1; $index -le $count"):]
    assert "try {" in loop and "catch {" in loop, "통합기마다 예외를 잡아야 한다"
    assert "FAILED" in loop


def test_the_single_vcenter_path_still_works():
    """연결 테스트는 통합기 1대 방식을 쓴다. 그 길이 막히면 설정 확인이 안 된다."""
    text = code_only(INVENTORY)
    assert "$OutputPath" in text
    assert "Read-Target 'VCENTER_'" in text, "예전 환경변수 이름을 그대로 받아야 한다"
