from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..config import AppConfig
from ..utils.validation import validate_oracle_identifier
from .oracle_connection import (
    OracleConnectionError,
    apply_call_timeout,
    build_connect_kwargs,
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


class OracleITSMCollector:
    """Read-only Oracle collector. Import and connect only when ORACLE mode runs."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.last_metadata: dict[str, Any] = {}

    def collect(self, max_rows: int | None = None) -> list[dict[str, Any]]:
        oracle_cfg = self.config.oracle
        try:
            oracledb = prepare_driver(oracle_cfg)
            kwargs = build_connect_kwargs(oracle_cfg)
        except OracleConnectionError as exc:
            raise OracleCollectionError(str(exc)) from exc
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
        # arraysize 는 한 번에 받아올 행 수다. 컬럼이 많으면 행당 버퍼가 커져서
        # 값을 크게 잡을수록 메모리와 왕복 지연이 함께 늘어난다. 수천 행 규모에서는
        # 수백이면 충분하고, 그 이상은 이득 없이 버퍼만 커진다.
        fetch_size = max(1, min(int(oracle_cfg.get("fetch_size", 500) or 500), 5000))
        self.last_metadata = {
            "asset_source_configured": bool(asset_source) or not requires_asset_source,
            "asset_source": asset_source,
            "query_file": str(query_path),
            "eos_field": eos_field,
            "cpu_field": cpu_field,
            "query_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "mode": mode,
        }

        # 실패했을 때 어느 단계였는지 남긴다. 드라이버 메시지(예: DPY-1001)만으로는
        # 접속에서 끊긴 건지 조회 도중 끊긴 건지 구분할 수 없다.
        stage = "CONNECT"
        fetched = 0
        try:
            with oracledb.connect(**kwargs) as connection:
                query_timeout = apply_call_timeout(connection, oracle_cfg)
                self.last_metadata["query_timeout_seconds"] = query_timeout
                stage = "EXECUTE"
                with connection.cursor() as cursor:
                    cursor.arraysize = fetch_size
                    # prefetchrows 까지 같이 키우면 첫 왕복에서 같은 양을 한 번 더
                    # 버퍼링한다. 컬럼이 많을수록 낭비라 기본값에 맡긴다.
                    cursor.execute(sql)
                    stage = "DESCRIBE"
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
                    stage = "FETCH"
                    while True:
                        rows = cursor.fetchmany(fetch_size)
                        fetched += len(rows)
                        if not rows:
                            break
                        if fetched % (fetch_size * 10) == 0:
                            # 오래 걸릴 때 어디까지 갔는지 로그로 알 수 있어야 한다.
                            LOGGER.info("Oracle 수집 진행: %d건", fetched)
                        for row in rows:
                            records.append(dict(zip(columns, row)))
                            if max_rows is not None and len(records) >= max_rows:
                                break
                        if max_rows is not None and len(records) >= max_rows:
                            break
        except OracleCollectionError:
            raise
        except Exception as exc:
            LOGGER.exception("Oracle collection failed (stage=%s, fetched=%d)", stage, fetched)
            self.last_metadata["failed_stage"] = stage
            self.last_metadata["fetched_before_failure"] = fetched
            raise OracleCollectionError(f"{exc} [단계={stage}{_HINTS.get(stage, '')}]") from exc

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
