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
        yield connection
