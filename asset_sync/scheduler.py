"""Windows 작업 스케줄러 등록과 배치 on/off.

설정에 `daily_time` 을 적어두는 것만으로는 아무 일도 일어나지 않는다. 실제로 매일
돌게 하려면 Windows 작업 스케줄러에 등록해야 하고, 그건 관리자 권한이 필요하다.

그래서 두 가지를 나눈다.

* **등록(한 번)** — `scripts/register_daily_task.bat` 을 관리자로 실행한다.
  시간이 바뀌면 화면 저장이 `schtasks /Change` 로 갱신을 시도하고, 권한이 없어
  실패하면 안내한다.
* **on/off(언제든)** — `scheduler.enabled` 를 배치가 직접 본다. 작업은 그대로 두고
  배치가 스스로 빠지므로 Windows 권한이 필요 없다.

읽기(`describe`)는 어디서든 안전하다. 쓰기(`create`/`change`/`delete`)는 권한이
없으면 실패를 그대로 알려준다.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger(__name__)

DEFAULT_TASK_NAME = "AssetDailyCollection"
DEFAULT_DAILY_TIME = "07:00"

#: schtasks 호출 제한(초). 응답이 없으면 화면이 멈추는 것보다 실패가 낫다.
COMMAND_TIMEOUT = 20

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: 작업 이름에 공백·한글을 허용하되, 명령행을 깨뜨리는 문자는 막는다.
_TASK_NAME_FORBIDDEN = re.compile(r'[\\/:*?"<>|\r\n\t]')


class ScheduleError(RuntimeError):
    pass


def normalize_time(value: Any) -> str:
    text = str(value or "").strip()
    if not _TIME_PATTERN.match(text):
        raise ScheduleError(f"배치 시각은 HH:MM(24시간) 형식이어야 합니다: {value!r}")
    return text


def normalize_task_name(value: Any) -> str:
    text = str(value or "").strip() or DEFAULT_TASK_NAME
    if _TASK_NAME_FORBIDDEN.search(text):
        raise ScheduleError(r'작업 이름에 \ / : * ? " < > | 와 줄바꿈은 쓸 수 없습니다.')
    if len(text) > 200:
        raise ScheduleError("작업 이름이 너무 깁니다(200자 이내).")
    return text


def settings(scheduler_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """설정에서 스케줄 관련 값만 꺼낸다. 기본은 '켜짐' 이다."""
    return {
        "enabled": bool(scheduler_cfg.get("enabled", True)),
        "daily_time": normalize_time(scheduler_cfg.get("daily_time") or DEFAULT_DAILY_TIME),
        "task_name": normalize_task_name(scheduler_cfg.get("task_name")),
    }


def is_windows() -> bool:
    return sys.platform.startswith("win")


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
            encoding="cp949" if is_windows() else "utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise ScheduleError("schtasks 명령을 찾을 수 없습니다. Windows 에서만 동작합니다.") from None
    except subprocess.TimeoutExpired:
        raise ScheduleError(f"{COMMAND_TIMEOUT}초 안에 응답이 없습니다.") from None
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def batch_path(root: Path) -> Path:
    return root / "scripts" / "run_daily_batch.bat"


# ── 수시 수집 (업무 시간 중 30분 · 1시간 간격) ────────────────────────────
DEFAULT_INTRADAY_TASK = "AssetIntradayCollection"
INTRADAY_INTERVALS = (15, 30, 60, 120)


def intraday_path(root: Path) -> Path:
    return root / "scripts" / "run_intraday_collect.bat"


def intraday_settings(scheduler_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """수시 수집 설정. 기본은 꺼짐이다(켜기 전에 DB 크기 · 소스 부하를 확인하라는 뜻)."""
    section = dict((scheduler_cfg or {}).get("intraday") or {})
    interval = int(section.get("interval_minutes") or 60)
    if interval not in INTRADAY_INTERVALS:
        raise ScheduleError(f"수시 수집 간격은 {', '.join(map(str, INTRADAY_INTERVALS))}분 중 하나여야 합니다: {interval}")
    start = normalize_time(section.get("start_time") or "08:00")
    end = normalize_time(section.get("end_time") or "20:00")
    if end <= start:
        raise ScheduleError(f"수시 수집 끝 시각({end})이 시작 시각({start})보다 늦어야 합니다.")
    return {
        "enabled": bool(section.get("enabled", False)),
        "interval_minutes": interval,
        "start_time": start,
        "end_time": end,
        "task_name": normalize_task_name(section.get("task_name") or DEFAULT_INTRADAY_TASK),
    }


def in_window(settings_: Mapping[str, Any], now: Any) -> bool:
    """지금이 수시 수집 시간대인가. 작업이 잘못 등록돼도 배치가 스스로 판단한다."""
    hhmm = now.strftime("%H:%M")
    return str(settings_["start_time"]) <= hhmm <= str(settings_["end_time"])


def apply_intraday(root: Path, wanted: Mapping[str, Any]) -> dict[str, Any]:
    """화면에서 저장한 수시 수집 설정을 Windows 작업에 반영한다. 실패해도 예외를 올리지 않는다."""
    result: dict[str, Any] = {"applied": False, "action": None, "error": None}
    if not is_windows():
        result["error"] = "Windows 가 아니어서 작업 스케줄러를 갱신하지 않았습니다."
        return result
    try:
        if wanted.get("enabled"):
            register_intraday(root, wanted)
            result.update(applied=True, action="REGISTERED")
        elif describe(str(wanted["task_name"])).get("registered"):
            unregister(str(wanted["task_name"]))
            result.update(applied=True, action="REMOVED")
    except ScheduleError as exc:
        result["error"] = str(exc)
    return result


def register_intraday(root: Path, wanted: Mapping[str, Any], *, force: bool = True) -> dict[str, Any]:
    """매일 시작 시각부터 끝 시각까지 N분마다 도는 작업을 만든다(schtasks /RI /DU)."""
    target = intraday_path(root)
    if not target.is_file():
        raise ScheduleError(f"배치 파일이 없습니다: {target}")
    start_h, start_m = map(int, str(wanted["start_time"]).split(":"))
    end_h, end_m = map(int, str(wanted["end_time"]).split(":"))
    minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    argv = [
        "schtasks", "/Create", "/TN", str(wanted["task_name"]),
        "/TR", f'"{target}"',
        "/SC", "DAILY", "/ST", str(wanted["start_time"]),
        "/RI", str(int(wanted["interval_minutes"])), "/DU", f"{minutes // 60:02d}:{minutes % 60:02d}",
        "/RU", "SYSTEM", "/RL", "HIGHEST",
    ]
    if force:
        argv.append("/F")
    code, output = _run(argv)
    if code != 0:
        raise ScheduleError(f"수시 수집 작업 등록 실패: {output or '권한을 확인하세요(관리자로 실행).'}")
    return {"task_name": wanted["task_name"], "argv": argv, "output": output}


def describe(task_name: str) -> dict[str, Any]:
    """등록된 작업의 상태를 읽는다. 없으면 registered=False 다."""
    task_name = normalize_task_name(task_name)
    if not is_windows():
        return {
            "registered": False,
            "supported": False,
            "reason": "Windows 가 아니어서 작업 스케줄러를 확인할 수 없습니다.",
        }
    code, output = _run(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"])
    if code != 0:
        return {"registered": False, "supported": True, "reason": output or "등록된 작업이 없습니다."}

    fields: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    def pick(*names: str) -> str:
        for name in names:
            if fields.get(name):
                return fields[name]
        return ""

    return {
        "registered": True,
        "supported": True,
        "task_name": task_name,
        # 한국어/영어 Windows 양쪽의 라벨을 본다.
        "state": pick("상태", "Status", "Scheduled Task State"),
        "next_run": pick("다음 실행 시간", "Next Run Time"),
        "last_run": pick("마지막 실행 시간", "Last Run Time"),
        "last_result": pick("마지막 결과", "Last Result"),
        "command": pick("실행할 작업", "Task To Run"),
        # 누구로 도는가. SYSTEM 이면 사용자 프로필에 깐 PowerCLI 를 못 본다.
        "run_as": pick("실행 사용자", "사용자로 실행", "Run As User"),
        # '사용자가 로그온할 때만 실행' 이면 아무도 로그온 안 한 07시에는 안 돈다.
        "logon_mode": pick("로그온 모드", "Logon Mode"),
    }


#: 작업 스케줄러 '마지막 결과' 값 → 무슨 뜻이고 무엇을 하면 되는가.
RESULT_CODES: dict[int, tuple[str, str]] = {
    0x0: ("정상 종료", ""),
    0x1: ("배치 파일이 오류로 끝났습니다(파이썬 가상환경 문제 등)",
          "logs\\daily_batch_console.log 의 마지막 BATCH START 아래를 보세요."),
    0x2: ("수집이 실패로 끝났습니다(FAILED)",
          "check_daily_batch.py 의 3) 배치 기록에 적힌 사유를 보세요."),
    0x41300: ("작업을 실행할 준비가 됐습니다", ""),
    0x41301: ("지금 실행 중입니다", "끝날 때까지 기다리세요. 4시간이 넘으면 멈춘 것입니다."),
    0x41302: ("작업이 꺼져(사용 안 함) 있습니다",
              "작업 스케줄러에서 작업을 [사용]으로 바꾸세요."),
    0x41303: ("아직 한 번도 실행되지 않았습니다",
              "예정 시각을 기다리거나 작업 스케줄러에서 [실행]을 눌러 보세요."),
    0x41306: ("작업이 중간에 종료됐습니다(시간 초과 또는 사용자가 종료)",
              "4시간 제한에 걸렸는지 로그를 보세요."),
    0x8004131F: ("이전 실행이 아직 돌고 있어 이번 실행을 건너뛰었습니다",
                 "멈춘 python.exe 가 있으면 종료하세요."),
    0x800710E0: ("실행 계정이 거부됐습니다(암호 만료·권한 부족)",
                 "실행 계정 암호를 갱신하거나 작업을 다시 등록하세요."),
    0x80070002: ("실행할 파일을 찾지 못했습니다",
                 "소스 폴더를 옮겼다면 작업을 다시 등록하세요."),
    0x8007010B: ("작업 폴더 경로가 잘못됐습니다",
                 "소스 폴더를 옮겼다면 작업을 다시 등록하세요."),
    0x80070005: ("접근이 거부됐습니다(권한 부족)",
                 "관리자 권한으로 작업을 다시 등록하세요."),
    0xC000013A: ("강제로 종료됐습니다(Ctrl+C 또는 종료 신호)", ""),
}


def explain_result(value: Any) -> dict[str, Any]:
    """'마지막 결과' 를 숫자로 읽어 뜻과 조치를 붙인다. 음수(부호 있는 표기)도 받는다."""
    text = str(value or "").strip()
    if not text:
        return {"code": None, "hex": "", "meaning": "기록 없음", "action": ""}
    try:
        code = int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return {"code": None, "hex": text, "meaning": "읽을 수 없는 값", "action": ""}
    if code < 0:
        code += 1 << 32
    meaning, action = RESULT_CODES.get(
        code, ("알 수 없는 결과 코드", "logs\\daily_batch_console.log 를 보세요.")
    )
    return {"code": code, "hex": f"0x{code:X}", "meaning": meaning, "action": action}


def register(root: Path, task_name: str, daily_time: str, *, force: bool = True) -> dict[str, Any]:
    """작업을 만든다(이미 있으면 덮어쓴다). 관리자 권한이 필요하다."""
    task_name = normalize_task_name(task_name)
    daily_time = normalize_time(daily_time)
    target = batch_path(root)
    if not target.is_file():
        raise ScheduleError(f"배치 파일이 없습니다: {target}")

    argv = [
        "schtasks", "/Create", "/TN", task_name,
        "/TR", f'"{target}"',
        "/SC", "DAILY", "/ST", daily_time,
        # 수집은 사용자가 로그온하지 않아도 돌아야 하고, DB·로그 기록 권한이 필요하다.
        "/RU", "SYSTEM", "/RL", "HIGHEST",
    ]
    if force:
        argv.append("/F")
    code, output = _run(argv)
    if code != 0:
        raise ScheduleError(f"작업 등록 실패: {output or '권한을 확인하세요(관리자로 실행).'}")
    return {"task_name": task_name, "daily_time": daily_time, "output": output}


def change_time(task_name: str, daily_time: str) -> dict[str, Any]:
    """이미 등록된 작업의 시각만 바꾼다."""
    task_name = normalize_task_name(task_name)
    daily_time = normalize_time(daily_time)
    code, output = _run(["schtasks", "/Change", "/TN", task_name, "/ST", daily_time])
    if code != 0:
        raise ScheduleError(f"작업 시각 변경 실패: {output or '등록 여부와 권한을 확인하세요.'}")
    return {"task_name": task_name, "daily_time": daily_time, "output": output}


def unregister(task_name: str) -> dict[str, Any]:
    task_name = normalize_task_name(task_name)
    code, output = _run(["schtasks", "/Delete", "/TN", task_name, "/F"])
    if code != 0:
        raise ScheduleError(f"작업 삭제 실패: {output}")
    return {"task_name": task_name, "output": output}


def apply_time(root: Path, task_name: str, daily_time: str) -> dict[str, Any]:
    """저장된 시각을 Windows 작업에 반영한다.

    화면에서 시각을 바꿨을 때 쓴다. 작업이 없으면 만들고, 있으면 시각만 바꾼다.
    실패해도 예외를 올리지 않는다 -- 설정 저장 자체는 성공했고, 권한이 없는
    환경에서 저장이 막히면 안 된다. 결과를 그대로 돌려주어 화면이 안내한다.
    """
    result: dict[str, Any] = {"applied": False, "action": None, "error": None}
    if not is_windows():
        result["error"] = "Windows 가 아니어서 작업 스케줄러를 갱신하지 않았습니다."
        return result
    try:
        current = describe(task_name)
        if current.get("registered"):
            change_time(task_name, daily_time)
            result.update({"applied": True, "action": "CHANGED"})
        else:
            register(root, task_name, daily_time)
            result.update({"applied": True, "action": "CREATED"})
    except ScheduleError as exc:
        LOGGER.warning("작업 스케줄러 갱신 실패: %s", exc)
        result["error"] = str(exc)
    return result
