from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from ..config import AppConfig
from ..utils.validation import validate_oracle_identifier
from .oracle_connection import (
    OracleConnectionError,
    apply_call_timeout,
    apply_lob_handler,
    build_connect_kwargs,
    describe_exception,
    plain_value,
    prepare_driver,
)

LOGGER = logging.getLogger(__name__)


class OracleCollectionError(RuntimeError):
    pass


#: 실패 단계별로 무엇을 확인해야 하는지. 드라이버 오류 코드만 보면 판단이 어렵다.
_HINTS = {
    "CONNECT": " · 접속 자체가 안 됨: 주소·포트·서비스명·계정 확인",
    "EXECUTE": " · 접속은 됐고 SQL 실행에서 실패: 조회 SQL 과 대상 테이블 권한 확인",
    "DESCRIBE": " · SQL 은 통과했으나 결과 구조를 읽지 못함",
    "FETCH": " · 조회 도중 끊김: 세션 타임아웃·방화벽 idle 차단·건수 과다 가능성",
}

#: 이 파일이 실제로 반영됐는지 화면에서 바로 확인하기 위한 표식.
#: 오류 문구에 같이 나오므로, 표식이 안 보이면 예전 파일이 돌고 있는 것이다.
COLLECTOR_BUILD = "2026-08-oracle-diag"


class OracleITSMCollector:
    """Read-only Oracle collector. Import and connect only when ORACLE mode runs."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.last_metadata: dict[str, Any] = {}

    def prepare_query(self) -> tuple[str, dict[str, Any]]:
        """조회 SQL 을 완성하고 그 근거를 함께 돌려준다.

        접속 없이도 확인할 수 있어야 진단 스크립트가 같은 SQL 을 쓸 수 있다.
        """
        oracle_cfg = self.config.oracle
        mode = str(oracle_cfg.get("mode", "thin")).lower()
        query_path = self.config.resolve(oracle_cfg.get("query_file", "config/oracle_query.local.sql"))
        if not query_path.exists():
            raise OracleCollectionError(
                "서버 내부의 Oracle 자산 조회 SQL이 준비되지 않았습니다. "
                "관리 화면의 [자산 테이블 찾기]에서 대상 테이블을 선택하면 자동 생성됩니다."
            )
        sql_template = query_path.read_text(encoding="utf-8")
        source_placeholders = ("${ASSET_SOURCE}", "${TABLE_NAME}")
        requires_asset_source = any(token in sql_template for token in source_placeholders)
        asset_source = ""
        if requires_asset_source:
            raw_asset_source = str(oracle_cfg.get("asset_source", "")).strip()
            if not raw_asset_source:
                raise OracleCollectionError(
                    "서버 내부의 Oracle 자산 조회 대상이 준비되지 않았습니다. "
                    "관리 화면의 [자산 테이블 찾기]에서 대상 테이블을 선택하세요."
                )
            asset_source = validate_oracle_identifier(raw_asset_source, "asset_source")
        eos_field = validate_oracle_identifier(str(self.config.itsm.get("os_eos_field", "")), "os_eos_field")
        cpu_field = validate_oracle_identifier(str(self.config.itsm.get("cpu_compare_field", "CM_CPU_CORE_CNT")), "cpu_compare_field")
        sql = (
            sql_template.replace("${ASSET_SOURCE}", asset_source)
            .replace("${TABLE_NAME}", asset_source)
            .replace("${EOS_FIELD}", eos_field)
            .replace("${CPU_FIELD}", cpu_field)
            .strip()
            .rstrip(";")
        )
        return sql, {
            "asset_source_configured": bool(asset_source) or not requires_asset_source,
            "asset_source": asset_source,
            "query_file": str(query_path),
            "eos_field": eos_field,
            "cpu_field": cpu_field,
            "query_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "mode": mode,
        }

    def collect(self, max_rows: int | None = None) -> list[dict[str, Any]]:
        oracle_cfg = self.config.oracle
        try:
            oracledb = prepare_driver(oracle_cfg)
            kwargs = build_connect_kwargs(oracle_cfg)
        except OracleConnectionError as exc:
            raise OracleCollectionError(str(exc)) from exc

        sql, metadata = self.prepare_query()
        cpu_field = str(metadata["cpu_field"])
        eos_field = str(metadata["eos_field"])
        # arraysize 는 한 번에 받아올 행 수다. 컬럼이 많으면 행당 버퍼가 커져서
        # 값을 크게 잡을수록 메모리와 왕복 지연이 함께 늘어난다. 수천 행 규모에서는
        # 수백이면 충분하고, 그 이상은 이득 없이 버퍼만 커진다.
        fetch_size = max(1, min(int(oracle_cfg.get("fetch_size", 500) or 500), 5000))
        self.last_metadata = dict(metadata)

        # 실패했을 때 어느 단계였는지 남긴다. 드라이버 메시지(예: DPY-1001)만으로는
        # 접속에서 끊긴 건지 조회 도중 끊긴 건지 구분할 수 없다.
        stage = "CONNECT"
        fetched = 0
        started_at = time.monotonic()
        stage_at = started_at
        stage_seconds: dict[str, float] = {}

        def enter(next_stage: str) -> str:
            """단계 전환. 각 단계에 몇 초가 걸렸는지 남긴다."""
            nonlocal stage_at
            now = time.monotonic()
            stage_seconds[stage] = round(now - stage_at, 1)
            stage_at = now
            return next_stage

        try:
            with oracledb.connect(**kwargs) as connection:
                query_timeout = apply_call_timeout(connection, oracle_cfg)
                # CM_DESCR 같은 CLOB 컬럼은 손잡이로 온다. 여기서 값으로 바꿔두지
                # 않으면 연결이 닫힌 뒤 정규화 단계에서 읽다가 DPY-1001 이 난다.
                apply_lob_handler(connection, oracledb)
                self.last_metadata["query_timeout_seconds"] = query_timeout
                stage = enter("EXECUTE")
                with connection.cursor() as cursor:
                    cursor.arraysize = fetch_size
                    # prefetchrows 까지 같이 키우면 첫 왕복에서 같은 양을 한 번 더
                    # 버퍼링한다. 컬럼이 많을수록 낭비라 기본값에 맡긴다.
                    cursor.execute(sql)
                    stage = enter("DESCRIBE")
                    columns = [str(column[0]).upper() for column in cursor.description]
                    memory_field = str(self.config.itsm.get("memory_field", "CM_MEMORY")).upper()
                    required = {
                        "CM_ID", "CM_HOSTNAME", "CM_IP", "CM_SUB_IP", "CM_OS", "CM_OS_VERSION",
                        "CM_SVR_CAT_CD", "CM_STA_CD", cpu_field, memory_field, eos_field,
                    }
                    missing = sorted(required - set(columns))
                    if missing:
                        raise OracleCollectionError(f"Oracle 조회 컬럼 누락: {', '.join(missing)}")
                    self.last_metadata["columns"] = columns
                    self.last_metadata["required_columns"] = sorted(required)
                    records: list[dict[str, Any]] = []
                    stage = enter("FETCH")
                    while True:
                        rows = cursor.fetchmany(fetch_size)
                        fetched += len(rows)
                        if not rows:
                            break
                        if fetched % (fetch_size * 10) == 0:
                            # 오래 걸릴 때 어디까지 갔는지 로그로 알 수 있어야 한다.
                            LOGGER.info("Oracle 수집 진행: %d건", fetched)
                        for row in rows:
                            # 핸들러가 안 걸린 드라이버를 대비한 안전망. 연결이 살아
                            # 있는 지금 읽어야 한다.
                            records.append({
                                column: plain_value(value)
                                for column, value in zip(columns, row)
                            })
                            if max_rows is not None and len(records) >= max_rows:
                                break
                        if max_rows is not None and len(records) >= max_rows:
                            break
        except OracleCollectionError:
            raise
        except Exception as exc:
            elapsed = round(time.monotonic() - started_at, 1)
            stage_seconds[stage] = round(time.monotonic() - stage_at, 1)
            LOGGER.exception(
                "Oracle collection failed (build=%s, stage=%s, fetched=%d, elapsed=%.1fs, stages=%s)",
                COLLECTOR_BUILD, stage, fetched, elapsed, stage_seconds,
            )
            self.last_metadata["failed_stage"] = stage
            self.last_metadata["fetched_before_failure"] = fetched
            self.last_metadata["stage_seconds"] = stage_seconds
            self.last_metadata["elapsed_seconds"] = elapsed
            # 마지막 오류만 보여주면 원인을 잃는다. 사슬과 경과시간을 함께 남긴다.
            raise OracleCollectionError(
                f"{describe_exception(exc)} "
                f"[단계={stage} 경과={elapsed}초 수집={fetched}건 "
                f"단계별초={stage_seconds} build={COLLECTOR_BUILD}{_HINTS.get(stage, '')}]"
            ) from exc

        if not records:
            raise OracleCollectionError("Oracle 조회 결과가 0건입니다. 정상 스냅샷으로 저장하지 않습니다.")
        self.last_metadata["rows"] = len(records)
        return records

    def test_connection(self) -> dict[str, Any]:
        rows = self.collect(max_rows=5)
        return {
            "status": "SUCCESS",
            "stage": "QUERY_VALIDATED",
            "sample_count": len(rows),
            "columns": sorted(rows[0].keys()) if rows else [],
            "required_columns": self.last_metadata.get("required_columns", []),
            "metadata": self.last_metadata,
        }
