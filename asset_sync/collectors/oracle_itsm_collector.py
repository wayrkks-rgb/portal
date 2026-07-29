from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..config import AppConfig
from ..utils.validation import validate_oracle_identifier

LOGGER = logging.getLogger(__name__)


class OracleCollectionError(RuntimeError):
    pass


class OracleITSMCollector:
    """Read-only Oracle collector. Import and connect only when ORACLE mode runs."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.last_metadata: dict[str, Any] = {}

    def collect(self, max_rows: int | None = None) -> list[dict[str, Any]]:
        try:
            import oracledb  # type: ignore
        except ImportError as exc:
            raise OracleCollectionError("oracledb 패키지가 설치되지 않았습니다. FILE_ONLY/DEMO 모드는 계속 사용할 수 있습니다.") from exc

        oracle_cfg = self.config.oracle
        mode = str(oracle_cfg.get("mode", "thin")).lower()
        if mode not in {"thin", "thick"}:
            raise OracleCollectionError("Oracle mode는 thin 또는 thick이어야 합니다.")
        if mode == "thick":
            client_dir = oracle_cfg.get("client_lib_dir")
            if not client_dir:
                raise OracleCollectionError("Thick mode에는 ORACLE_CLIENT_LIB_DIR가 필요합니다.")
            try:
                oracledb.init_oracle_client(lib_dir=str(client_dir))
            except Exception as exc:
                # The library may already be initialized in the process. Preserve the clear error otherwise.
                if "already" not in str(exc).lower():
                    raise OracleCollectionError(f"Oracle Client 초기화 실패: {exc}") from exc

        user = str(oracle_cfg.get("user") or "").strip()
        password = str(oracle_cfg.get("password") or "")
        dsn = str(oracle_cfg.get("dsn") or "").strip()
        host = str(oracle_cfg.get("host") or "").strip()
        port = int(oracle_cfg.get("port", 1521))
        service_name = str(oracle_cfg.get("service_name") or "").strip()
        sid = str(oracle_cfg.get("sid") or "").strip()
        if not user or not password:
            raise OracleCollectionError("Oracle 설정이 필요합니다: ORACLE_USER/ORACLE_PASSWORD")
        if not dsn and (not host or not (service_name or sid)):
            raise OracleCollectionError("Oracle 설정이 필요합니다: ORACLE_DSN 또는 HOST + SERVICE_NAME/SID")

        query_path = self.config.resolve(oracle_cfg.get("query_file", "config/oracle_query.local.sql"))
        if not query_path.exists():
            raise OracleCollectionError("서버 내부의 Oracle 자산 조회 SQL이 준비되지 않았습니다.")
        sql_template = query_path.read_text(encoding="utf-8")
        source_placeholders = ("${ASSET_SOURCE}", "${TABLE_NAME}")
        requires_asset_source = any(token in sql_template for token in source_placeholders)
        asset_source = ""
        if requires_asset_source:
            raw_asset_source = str(oracle_cfg.get("asset_source", "")).strip()
            if not raw_asset_source:
                raise OracleCollectionError("서버 내부의 Oracle 자산 조회 대상이 준비되지 않았습니다.")
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
        fetch_size = int(oracle_cfg.get("fetch_size", 1000))
        self.last_metadata = {
            "asset_source_configured": bool(asset_source) or not requires_asset_source,
            "eos_field": eos_field,
            "cpu_field": cpu_field,
            "query_hash": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "mode": mode,
        }

        kwargs: dict[str, Any] = {"user": user, "password": password}
        if dsn:
            kwargs["dsn"] = dsn
        else:
            kwargs.update({"host": host, "port": port})
            kwargs["service_name" if service_name else "sid"] = service_name or sid

        try:
            with oracledb.connect(**kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.arraysize = fetch_size
                    cursor.prefetchrows = fetch_size
                    cursor.execute(sql)
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
                    while True:
                        rows = cursor.fetchmany(fetch_size)
                        if not rows:
                            break
                        for row in rows:
                            records.append(dict(zip(columns, row)))
                            if max_rows is not None and len(records) >= max_rows:
                                break
                        if max_rows is not None and len(records) >= max_rows:
                            break
        except OracleCollectionError:
            raise
        except Exception as exc:
            LOGGER.exception("Oracle collection failed")
            raise OracleCollectionError(str(exc)) from exc

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
