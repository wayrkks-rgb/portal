"""일일 배치 .bat 이 흔적 없이 빠지지 않는지 지킨다.

작업 스케줄러가 돌릴 때는 콘솔이 없다. 예전 .bat 은 사전 점검이 하나라도
걸리면 로그에 아무것도 안 남기고 끝났다 -- "배치가 안 돈다" 는데 남은 것이
없어 원인을 못 찾았다. 그 상태로 돌아가지 않도록 문자열로 지킨다.
"""

from __future__ import annotations

from application.settings import BASE_DIR

BAT = (BASE_DIR / "scripts" / "run_daily_batch.bat").read_text(encoding="utf-8")
LINES = [line.strip() for line in BAT.splitlines()]


def test_the_batch_file_is_plain_ascii() -> None:
    """cmd.exe 는 .bat 을 OEM 코드페이지로 읽는다. 한글이 섞이면 로그가 깨진다."""
    BAT.encode("ascii")


def test_every_step_writes_to_the_console_log() -> None:
    assert 'set "LOG=logs\\daily_batch_console.log"' in BAT
    for step in ("check_python_313.bat", "check_auto_configuration.py", "jobs\\daily_batch.py"):
        line = next(line for line in LINES if step in line)
        assert '>> "%LOG%" 2>&1' in line, f"{step} 출력이 로그로 가지 않습니다: {line}"


def test_start_and_end_are_stamped_with_the_account() -> None:
    """누구로 돌았는지가 SYSTEM 계정 문제를 가려내는 첫 단서다."""
    start = next(line for line in LINES if "BATCH START" in line)
    assert "%USERNAME%" in start and "%date%" in start
    assert any("BATCH END" in line and "%RC%" in line for line in LINES)


def test_a_failed_pre_check_does_not_stop_the_batch() -> None:
    """통합기 하나의 설정 문제로 ITSM 수집까지 통째로 건너뛰면 안 된다."""
    check = LINES.index(next(line for line in LINES if "check_auto_configuration.py" in line))
    after = LINES[check + 1]
    assert after.startswith("if errorlevel 1") and "exit" not in after, after
    assert any("jobs\\daily_batch.py" in line for line in LINES[check + 1:])


def test_the_batch_exit_code_reaches_the_scheduler() -> None:
    """FAILED(2) 가 작업 스케줄러 '마지막 결과' 에 그대로 보여야 한다."""
    assert LINES[-1] == "exit /b %RC%"
