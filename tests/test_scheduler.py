"""배치 스케줄 설정과 Windows 작업 등록을 확인한다.

설정에 시각만 적어두면 아무 일도 일어나지 않는다. 그 차이를 눈에 보이게 하는 것이
이 기능의 목적이므로, 값 검증과 '등록 실패가 설정 저장을 막지 않는다' 를 함께 지킨다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asset_sync import scheduler
from asset_sync.settings_store import LocalSettingsStore, SettingsValidationError


# ── 값 검증 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["07:00", "00:00", "23:59", " 09:30 "])
def test_valid_times_are_accepted(value):
    assert scheduler.normalize_time(value) == value.strip()


@pytest.mark.parametrize("value", ["7:00", "24:00", "07:60", "0700", "", None, "아침"])
def test_invalid_times_are_refused(value):
    with pytest.raises(scheduler.ScheduleError):
        scheduler.normalize_time(value)


def test_a_blank_task_name_falls_back_to_the_default():
    assert scheduler.normalize_task_name("") == scheduler.DEFAULT_TASK_NAME
    assert scheduler.normalize_task_name(None) == scheduler.DEFAULT_TASK_NAME


def test_a_korean_task_name_is_fine():
    assert scheduler.normalize_task_name("자산 일일수집") == "자산 일일수집"


@pytest.mark.parametrize("value", ["a/b", "a\\b", 'a"b', "a\nb", "a|b", "a*b"])
def test_a_task_name_cannot_break_the_command_line(value):
    with pytest.raises(scheduler.ScheduleError):
        scheduler.normalize_task_name(value)


def test_settings_default_to_enabled():
    """설정에 없던 기존 서버에서 갑자기 배치가 멈추면 안 된다."""
    assert scheduler.settings({})["enabled"] is True


def test_settings_reads_all_three_values():
    assert scheduler.settings({"enabled": False, "daily_time": "06:30", "task_name": "T"}) == {
        "enabled": False, "daily_time": "06:30", "task_name": "T",
    }


# ── 설정 저장 ──────────────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalSettingsStore:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text("itsm:\n  collection_mode: DEMO\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_daily_batch.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    return LocalSettingsStore(tmp_path)


RUNTIME = {"security": {}, "scheduler": {"enabled": False, "daily_time": "06:30", "task_name": "내자산수집"}}


def test_saving_keeps_the_on_off_switch(store):
    saved = store.save_runtime(RUNTIME)
    assert saved["scheduler"]["enabled"] is False
    assert saved["scheduler"]["daily_time"] == "06:30"
    assert saved["scheduler"]["task_name"] == "내자산수집"
    # 다시 읽어도 같아야 한다.
    assert store.public_settings()["scheduler"]["enabled"] is False


def test_a_bad_time_is_refused_before_anything_is_written(store):
    with pytest.raises(SettingsValidationError):
        store.save_runtime({"security": {}, "scheduler": {"daily_time": "25:00"}})
    assert not store.app_local_path.exists()


def test_the_task_result_comes_back_with_the_settings(store):
    """Windows 작업 갱신이 실패해도 저장 자체는 성공해야 한다."""
    saved = store.save_runtime(RUNTIME)
    task = saved["scheduler"]["task"]
    assert "applied" in task
    if not scheduler.is_windows():
        assert task["applied"] is False
        assert task["error"]
    # 실패했더라도 설정은 파일에 남아 있다.
    assert store.public_settings()["scheduler"]["daily_time"] == "06:30"


def test_saving_does_not_raise_when_the_task_cannot_be_registered(store, monkeypatch):
    def boom(*args, **kwargs):
        raise scheduler.ScheduleError("권한이 없습니다")

    monkeypatch.setattr(scheduler, "is_windows", lambda: True)
    monkeypatch.setattr(scheduler, "describe", lambda name: {"registered": False, "supported": True})
    monkeypatch.setattr(scheduler, "register", boom)

    saved = store.save_runtime(RUNTIME)
    assert saved["scheduler"]["task"] == {
        "applied": False, "action": None, "error": "권한이 없습니다",
    }
    # 등록은 실패했어도 설정은 저장돼 있어야 한다.
    assert saved["scheduler"]["daily_time"] == "06:30"


# ── 배치가 스스로 빠지는지 ─────────────────────────────────────────────────

def test_the_batch_skips_itself_when_disabled(tmp_path, monkeypatch):
    """끄고 켜는 데 Windows 권한이 필요하면 안 된다. 배치가 직접 판단한다."""
    import subprocess
    import sys

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "app_config.yaml").write_text(
        "itsm:\n  collection_mode: DEMO\nscheduler:\n  enabled: false\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, "jobs/daily_batch.py"],
        capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "ASSET_APP_ROOT": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert '"status": "DISABLED"' in proc.stdout, proc.stdout


# ── 작업 스케줄러 '마지막 결과' 를 사람 말로 ─────────────────────────────
# 0x1 · 0x800710E0 같은 숫자만 보고는 무엇을 해야 할지 알 수 없다.


def test_known_result_codes_get_a_meaning_and_an_action() -> None:
    from asset_sync.scheduler import explain_result

    assert explain_result("0")["meaning"] == "정상 종료"
    assert explain_result("1")["hex"] == "0x1"
    assert explain_result("2")["meaning"].startswith("수집이 실패")
    # schtasks 는 0x8... 을 부호 있는 음수로 찍는다.
    refused = explain_result(str(0x800710E0 - (1 << 32)))
    assert refused["hex"] == "0x800710E0"
    assert "암호" in refused["action"]
    assert explain_result("267011")["hex"] == "0x41303"


def test_unknown_or_empty_result_is_not_an_error() -> None:
    from asset_sync.scheduler import explain_result

    assert explain_result("")["meaning"] == "기록 없음"
    assert explain_result("12345")["meaning"] == "알 수 없는 결과 코드"
    assert explain_result("abc")["meaning"] == "읽을 수 없는 값"


def test_describe_reads_the_run_as_account_and_logon_mode(monkeypatch) -> None:
    from asset_sync import scheduler

    output = "\n".join([
        "작업 이름:         \\AssetDailyCollection",
        "다음 실행 시간:    2026-11-03 오전 7:00:00",
        "상태:              준비",
        "로그온 모드:       대화형/백그라운드",
        "마지막 실행 시간:  2026-11-02 오전 7:00:00",
        "마지막 결과:       1",
        "실행 사용자:       SYSTEM",
    ])
    monkeypatch.setattr(scheduler, "is_windows", lambda: True)
    monkeypatch.setattr(scheduler, "_run", lambda argv: (0, output))
    described = scheduler.describe("AssetDailyCollection")
    assert described["run_as"] == "SYSTEM"
    assert described["logon_mode"] == "대화형/백그라운드"
    assert described["last_result"] == "1"
