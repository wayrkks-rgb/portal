"""Oracle ITSM 수집을 단계별로 점검한다.

CLI(scripts/diagnose_oracle_itsm.py)와 웹(관리 API)이 **같은 코드**를 쓴다.
둘의 결과가 다르면 그 차이 자체가 단서이므로, 점검 로직이 갈라지면 안 된다.

읽기만 한다. 쓰는 문장은 실행하지 않는다.
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from typing import Any, Callable, Mapping

from .oracle_connection import build_connect_kwargs, describe_exception, prepare_driver
from .oracle_itsm_collector import COLLECTOR_BUILD, OracleITSMCollector

LOGGER = logging.getLogger(__name__)

#: 비밀번호로 취급할 설정 키. 값은 절대 응답에 담지 않는다.
SECRET_HINTS = ("password", "pwd", "secret")

#: 응답에 보여줄 접속 설정. 여기 없는 값은 내보내지 않는다.
SHOWN_KEYS = (
    "mode", "host", "port", "service_name", "sid", "dsn", "user", "password",
    "asset_source", "query_file", "fetch_size",
    "connect_timeout_seconds", "query_timeout_seconds",
)


def mask(key: str, value: Any) -> Any:
    if any(hint in key.lower() for hint in SECRET_HINTS):
        return "(설정됨)" if str(value or "") else "(비어 있음)"
    return value


def visible_settings(oracle_cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {key: mask(key, oracle_cfg[key]) for key in SHOWN_KEYS if key in oracle_cfg}


class Steps:
    """단계별 성공 여부와 소요시간을 모은다."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def record(self, name: str, run: Callable[[], str]) -> bool:
        """run() 이 값을 돌려주면 성공, 예외를 내면 실패로 기록한다."""
        started = time.monotonic()
        try:
            detail = run()
        except Exception as exc:  # 진단이므로 어떤 예외든 기록하고 계속 판단한다.
            LOGGER.exception("Oracle 진단 실패: %s", name)
            self.items.append({
                "name": name,
                "status": "FAILED",
                "seconds": round(time.monotonic() - started, 2),
                "error": describe_exception(exc),
            })
            return False
        self.items.append({
            "name": name,
            "status": "SUCCESS",
            "seconds": round(time.monotonic() - started, 2),
            "detail": detail,
        })
        return True

    def skip(self, name: str, reason: str) -> None:
        self.items.append({"name": name, "status": "SKIPPED", "detail": reason})


def run_diagnostics(config: Any, *, max_rows: int | None = None) -> dict[str, Any]:
    """설정부터 실제 수집까지 순서대로 확인하고 결과를 돌려준다.

    앞 단계가 실패하면 뒤는 볼 필요가 없으므로 거기서 멈춘다.
    """
    oracle_cfg = config.oracle
    steps = Steps()
    result: dict[str, Any] = {
        "build": COLLECTOR_BUILD,
        "python": sys.version.split()[0],
        "collection_mode": str(config.itsm.get("collection_mode", "ORACLE")).upper(),
        "settings": visible_settings(oracle_cfg),
        "steps": steps.items,
    }

    try:
        import oracledb
        result["oracledb"] = oracledb.__version__
    except ImportError:
        result["oracledb"] = None
        steps.record("드라이버", lambda: (_ for _ in ()).throw(
            RuntimeError("oracledb 가 설치되지 않았습니다. requirements-oracle.txt 를 설치하세요.")
        ))
        result["status"] = "FAILED"
        return result

    if result["collection_mode"] != "ORACLE":
        steps.skip("수집모드", f"ITSM 수집모드가 {result['collection_mode']} 라 Oracle 직접조회를 쓰지 않습니다.")
        result["status"] = "SKIPPED"
        return result

    collector = OracleITSMCollector(config)
    state: dict[str, Any] = {}

    def prepare() -> str:
        sql, metadata = collector.prepare_query()
        state["sql"] = sql
        return f"{metadata['query_file']} (해시 {metadata['query_hash'][:12]})"

    def driver() -> str:
        state["driver"] = prepare_driver(oracle_cfg)
        state["kwargs"] = build_connect_kwargs(oracle_cfg)
        return f"mode={str(oracle_cfg.get('mode', 'thin')).lower()}"

    def tcp() -> str:
        host = str(oracle_cfg.get("host") or "").strip()
        port = int(oracle_cfg.get("port") or 1521)
        with socket.create_connection((host, port), timeout=10):
            return f"{host}:{port} 연결됨"

    def login() -> str:
        state["connection"] = state["driver"].connect(**state["kwargs"])
        return "로그인 성공"

    def ping() -> str:
        with state["connection"].cursor() as cursor:
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
        return "SELECT 1 FROM DUAL"

    def count() -> str:
        with state["connection"].cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM ({state['sql']})")
            total = int(cursor.fetchone()[0])
        state["total"] = total
        return f"{total:,}건"

    def fetch() -> str:
        rows = collector.collect(max_rows=max_rows)
        state["rows"] = len(rows)
        return f"{len(rows):,}건 · 컬럼 {len(rows[0])}개"

    ordered: list[tuple[str, Callable[[], str]]] = [("조회 SQL", prepare), ("드라이버", driver)]
    if str(oracle_cfg.get("host") or "").strip():
        ordered.append(("TCP 연결", tcp))
    ordered += [("로그인", login), ("기본 조회", ping), ("대상 건수", count)]

    try:
        for name, run in ordered:
            if not steps.record(name, run):
                result["status"] = "FAILED"
                return result
        if not str(oracle_cfg.get("host") or "").strip():
            steps.skip("TCP 연결", "DSN 사용 · 주소를 여기서 알 수 없음")
    finally:
        connection = state.get("connection")
        if connection is not None:
            try:
                connection.close()
            except Exception:  # 진단 결과를 정리 실패로 덮지 않는다.
                LOGGER.warning("Oracle 진단 연결 정리 실패", exc_info=True)

    if not steps.record("실제 수집", fetch):
        result["status"] = "FAILED"
        result["collector_metadata"] = collector.last_metadata
        return result

    result["status"] = "SUCCESS"
    result["rows"] = state.get("rows")
    return result
