from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

LOGGER = logging.getLogger(__name__)

#: 접속 단계 제한(초). 폐쇄망에서 경로가 막히면 여기서 걸린다.
DEFAULT_CONNECT_TIMEOUT = 15
#: 조회 한 건의 제한(초). 컬럼이 많고 행이 수천이어도 이 안에 끝나야 정상이다.
DEFAULT_QUERY_TIMEOUT = 300


class OracleConnectionError(RuntimeError):
    pass


def describe_exception(exc: BaseException, limit: int = 5) -> str:
    """예외 사슬을 한 줄로 편다.

    드라이버가 실패한 뒤 정리 단계에서 다시 실패하면, 마지막 오류만 남고 원래
    이유는 __context__ 로 밀려난다. DPY-1001("not connected to database")이 대표적인
    예다. 이 값은 "연결이 이미 끊긴 객체를 썼다"는 결과일 뿐이라, 그것만 봐서는
    왜 끊겼는지 알 수 없다. 원인 사슬을 같이 남겨야 판단할 수 있다.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(parts) < limit:
        if id(current) in seen:
            break
        seen.add(id(current))
        text = " ".join(str(current).split()) or current.__class__.__name__
        if not parts or text != parts[-1]:
            parts.append(text)
        current = current.__cause__ or current.__context__
    return " ← 원인: ".join(parts)


def import_oracledb() -> Any:
    """Import the driver only when an Oracle feature actually runs."""
    try:
        import oracledb  # type: ignore
    except ImportError as exc:
        raise OracleConnectionError(
            "oracledb 패키지가 설치되지 않았습니다. FILE_ONLY/DEMO 모드는 계속 사용할 수 있습니다."
        ) from exc
    return oracledb


def prepare_driver(oracle_cfg: Mapping[str, Any]) -> Any:
    """Return the driver module with thick mode initialized when requested."""
    mode = str(oracle_cfg.get("mode", "thin")).lower()
    if mode not in {"thin", "thick"}:
        raise OracleConnectionError("Oracle mode는 thin 또는 thick이어야 합니다.")
    oracledb = import_oracledb()
    if mode == "thick":
        client_dir = str(oracle_cfg.get("client_lib_dir") or "").strip()
        if not client_dir:
            raise OracleConnectionError("Thick mode에는 ORACLE_CLIENT_LIB_DIR가 필요합니다.")
        try:
            oracledb.init_oracle_client(lib_dir=client_dir)
        except Exception as exc:
            # The library may already be initialized in the process. Preserve the clear error otherwise.
            if "already" not in str(exc).lower():
                raise OracleConnectionError(f"Oracle Client 초기화 실패: {exc}") from exc
    return oracledb


def build_connect_kwargs(oracle_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the connection values and build oracledb.connect keyword arguments."""
    user = str(oracle_cfg.get("user") or "").strip()
    password = str(oracle_cfg.get("password") or "")
    dsn = str(oracle_cfg.get("dsn") or "").strip()
    host = str(oracle_cfg.get("host") or "").strip()
    try:
        port = int(oracle_cfg.get("port") or 1521)
    except (TypeError, ValueError) as exc:
        raise OracleConnectionError("Oracle Port는 숫자여야 합니다.") from exc
    service_name = str(oracle_cfg.get("service_name") or "").strip()
    sid = str(oracle_cfg.get("sid") or "").strip()
    if not user or not password:
        raise OracleConnectionError("Oracle 설정이 필요합니다: ORACLE_USER/ORACLE_PASSWORD")
    if not dsn and (not host or not (service_name or sid)):
        raise OracleConnectionError("Oracle 설정이 필요합니다: ORACLE_DSN 또는 HOST + SERVICE_NAME/SID")

    kwargs: dict[str, Any] = {"user": user, "password": password}
    if dsn:
        kwargs["dsn"] = dsn
    else:
        kwargs.update({"host": host, "port": port})
        kwargs["service_name" if service_name else "sid"] = service_name or sid

    # 타임아웃이 없으면 방화벽이 조용히 끊었을 때 무한정 기다리거나, 한참 뒤에
    # 엉뚱한 오류로 나타난다. 어디서 막혔는지 알 수 있게 접속 단계에 제한을 둔다.
    connect_timeout = _positive_int(oracle_cfg.get("connect_timeout_seconds"), DEFAULT_CONNECT_TIMEOUT)
    if connect_timeout:
        kwargs["tcp_connect_timeout"] = connect_timeout
    return kwargs


def _positive_int(value: Any, default: int) -> int:
    """0 이나 빈 값은 '제한 없음' 으로 본다."""
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def plain_value(value: Any) -> Any:
    """LOB 손잡이를 값으로 바꾼다.

    LOB 은 '나중에 읽는 손잡이'라서, 연결이 닫힌 뒤에 읽으면 DPY-1001 이 난다.
    수집 결과는 연결보다 오래 살아남으므로 받는 즉시 값으로 만들어야 한다.
    """
    read = getattr(value, "read", None)
    return read() if callable(read) else value


def apply_lob_handler(connection: Any, oracledb: Any) -> None:
    """CLOB/BLOB 을 손잡이가 아니라 값으로 받게 한다.

    행마다 따로 읽으면 왕복이 행 수만큼 늘어난다. 드라이버가 조회 결과와 함께
    실어 보내도록 바꾸면 왕복도 없고, 연결이 닫힌 뒤에 읽을 일도 없다.
    """
    replacement = {
        oracledb.DB_TYPE_CLOB: oracledb.DB_TYPE_LONG,
        oracledb.DB_TYPE_NCLOB: oracledb.DB_TYPE_LONG,
        oracledb.DB_TYPE_BLOB: oracledb.DB_TYPE_LONG_RAW,
    }

    def handler(cursor: Any, *args: Any) -> Any:
        # 드라이버 버전에 따라 (cursor, metadata) 또는
        # (cursor, name, default_type, ...) 로 불린다. 둘 다 받는다.
        type_code = getattr(args[0], "type_code", None)
        if type_code is None and len(args) >= 2:
            type_code = args[1]
        target = replacement.get(type_code)
        if target is None:
            return None
        return cursor.var(target, arraysize=cursor.arraysize)

    try:
        connection.outputtypehandler = handler
    except Exception:  # 지원하지 않는 드라이버면 plain_value 가 받아낸다.
        LOGGER.warning("LOB 출력 핸들러를 설정하지 못했습니다. 행마다 읽습니다.", exc_info=True)


def apply_call_timeout(connection: Any, oracle_cfg: Mapping[str, Any]) -> int:
    """조회 한 건이 이 시간을 넘기면 드라이버가 끊는다.

    접속은 됐는데 조회가 끝나지 않는 경우(잠금 대기, 과도한 전체 스캔)를 잡는다.
    0 이면 제한하지 않는다.
    """
    seconds = _positive_int(oracle_cfg.get("query_timeout_seconds"), DEFAULT_QUERY_TIMEOUT)
    if seconds:
        # call_timeout 은 밀리초 단위다.
        connection.call_timeout = seconds * 1000
    return seconds


@contextmanager
def oracle_connection(oracle_cfg: Mapping[str, Any]) -> Iterator[Any]:
    """Open a read-only Oracle session from the given configuration section."""
    oracledb = prepare_driver(oracle_cfg)
    kwargs = build_connect_kwargs(oracle_cfg)
    with oracledb.connect(**kwargs) as connection:
        apply_call_timeout(connection, oracle_cfg)
        apply_lob_handler(connection, oracledb)
        yield connection
