"""정리 단계에서 덮어써진 원인을 잃지 않는지 확인한다."""

from __future__ import annotations

from asset_sync.collectors.oracle_connection import describe_exception


def test_single_exception_is_unchanged():
    assert describe_exception(RuntimeError("DPY-1001: not connected to database")) == (
        "DPY-1001: not connected to database"
    )


def test_empty_message_falls_back_to_class_name():
    assert describe_exception(TimeoutError()) == "TimeoutError"


def test_message_is_flattened_to_one_line():
    assert describe_exception(RuntimeError("첫 줄\n  둘째 줄")) == "첫 줄 둘째 줄"


def test_explicit_cause_is_appended():
    try:
        try:
            raise RuntimeError("ORA-00942: table or view does not exist")
        except RuntimeError as inner:
            raise ValueError("수집 실패") from inner
    except ValueError as exc:
        assert describe_exception(exc) == (
            "수집 실패 ← 원인: ORA-00942: table or view does not exist"
        )


def test_implicit_context_is_kept():
    """DPY-1001 은 결과일 뿐이라 __context__ 를 놓치면 이유를 알 수 없다."""
    try:
        try:
            raise RuntimeError("DPY-4011: the database or network closed the connection")
        except RuntimeError:
            raise RuntimeError("DPY-1001: not connected to database")
    except RuntimeError as exc:
        assert describe_exception(exc) == (
            "DPY-1001: not connected to database"
            " ← 원인: DPY-4011: the database or network closed the connection"
        )


def test_chain_is_capped():
    exc: BaseException = RuntimeError("0")
    for index in range(1, 10):
        try:
            raise RuntimeError(str(index)) from exc
        except RuntimeError as raised:
            exc = raised
    assert describe_exception(exc, limit=3).count("←") == 2


def test_repeated_message_is_not_duplicated():
    try:
        try:
            raise RuntimeError("같은 문구")
        except RuntimeError as inner:
            raise RuntimeError("같은 문구") from inner
    except RuntimeError as exc:
        assert describe_exception(exc) == "같은 문구"
