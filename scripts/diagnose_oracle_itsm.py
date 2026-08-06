"""ITSM 수집이 어디서 막히는지 웹 없이 단계별로 확인한다.

    .venv\\Scripts\\python.exe scripts\\diagnose_oracle_itsm.py
    .venv\\Scripts\\python.exe scripts\\diagnose_oracle_itsm.py --rows 100
    .venv\\Scripts\\python.exe scripts\\diagnose_oracle_itsm.py --full > diag.txt

[ITSM 수집] 버튼과 같은 코드를 그대로 태우되, 각 단계의 소요시간과 실패한 예외
사슬을 모두 보여준다. DPY-1001("not connected to database")처럼 **결과만 알려주는**
오류는 그것만 봐서는 원인을 알 수 없다. 실제 이유는 그 앞 예외에 들어 있다.

단계
    0 환경      실행 중인 파일 경로와 표식 (예전 파일이 도는지 확인)
    1 설정      주소·계정·타임아웃·조회 SQL (비밀번호는 가림)
    2 TCP       host:port 소켓 연결
    3 로그인    oracledb.connect
    4 기본조회  SELECT 1 FROM DUAL
    5 건수      조회 SQL 을 감싼 COUNT(*)
    6 수집      실제 ITSM 수집

읽기만 한다. 쓰는 문장은 실행하지 않는다.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors import oracle_itsm_collector as collector_module
from asset_sync.collectors.oracle_connection import (
    build_connect_kwargs,
    describe_exception,
    prepare_driver,
)
from asset_sync.collectors.oracle_itsm_collector import OracleITSMCollector
from asset_sync.config import load_config

SECRET_KEYS = {"password", "pwd", "secret"}


def mask(key: str, value: object) -> object:
    if any(token in key.lower() for token in SECRET_KEYS):
        return "(설정됨)" if str(value or "") else "(비어 있음)"
    return value


def head(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")
    print("-" * 68)


class Timer:
    """단계마다 몇 초 걸렸는지 보여준다. 즉시 실패와 타임아웃은 대응이 다르다."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def lap(self) -> float:
        now = time.monotonic()
        elapsed = now - self.started
        self.started = now
        return round(elapsed, 2)


def fail(timer: Timer, exc: BaseException, full: bool) -> int:
    print(f"  ✗ 실패 ({timer.lap()}초)")
    print(f"  {describe_exception(exc)}")
    if full:
        print()
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    else:
        print("  (전체 스택은 --full 로 볼 수 있습니다)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ITSM Oracle 수집 단계별 진단")
    parser.add_argument("--rows", type=int, default=0,
                        help="이 건수까지만 수집해 본다 (0 이면 전체)")
    parser.add_argument("--full", action="store_true", help="실패 시 전체 스택 출력")
    parser.add_argument("--show-sql", action="store_true", help="완성된 조회 SQL 출력")
    args = parser.parse_args()

    config = load_config()
    oracle_cfg = config.oracle
    timer = Timer()

    head(0, "환경")
    print(f"  python           {sys.version.split()[0]}")
    print(f"  수집기 파일      {collector_module.__file__}")
    print(f"  수집기 표식      {getattr(collector_module, 'COLLECTOR_BUILD', '(없음 · 예전 파일입니다)')}")
    try:
        import oracledb
        print(f"  oracledb         {oracledb.__version__}")
    except ImportError:
        print("  oracledb         설치되지 않음 → requirements-oracle.txt 설치 필요")
        return 1

    head(1, "설정")
    mode = str(config.itsm.get("collection_mode", "ORACLE")).upper()
    print(f"  ITSM 수집모드    {mode}")
    if mode != "ORACLE":
        print("  → ORACLE 이 아니면 이 진단은 의미가 없습니다.")
        return 0
    for key in ("mode", "host", "port", "service_name", "sid", "dsn", "user", "password",
                "asset_source", "query_file", "fetch_size",
                "connect_timeout_seconds", "query_timeout_seconds"):
        if key in oracle_cfg:
            print(f"  {key:<18} {mask(key, oracle_cfg.get(key))}")

    itsm_collector = OracleITSMCollector(config)
    try:
        sql, metadata = itsm_collector.prepare_query()
    except Exception as exc:
        return fail(timer, exc, args.full)
    print(f"  조회 SQL         {metadata['query_file']} (해시 {metadata['query_hash'][:12]})")
    if args.show_sql:
        print("\n" + sql + "\n")

    try:
        driver = prepare_driver(oracle_cfg)
        kwargs = build_connect_kwargs(oracle_cfg)
    except Exception as exc:
        return fail(timer, exc, args.full)

    head(2, "TCP 연결")
    host = str(oracle_cfg.get("host") or "").strip()
    port = int(oracle_cfg.get("port") or 1521)
    if kwargs.get("dsn") and not host:
        print("  건너뜀 (DSN 사용 · 주소를 여기서 알 수 없음)")
    else:
        timer.lap()
        try:
            with socket.create_connection((host, port), timeout=10):
                print(f"  ✓ {host}:{port} 연결됨 ({timer.lap()}초)")
        except OSError as exc:
            print(f"  ✗ {host}:{port} 실패 ({timer.lap()}초) — {exc}")
            print("  → 방화벽·Listener·주소를 먼저 확인하세요. 이 아래는 볼 필요가 없습니다.")
            return 1

    head(3, "로그인")
    timer.lap()
    try:
        connection = driver.connect(**kwargs)
    except Exception as exc:
        return fail(timer, exc, args.full)
    print(f"  ✓ 로그인 성공 ({timer.lap()}초)")

    try:
        head(4, "기본 조회")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
        print(f"  ✓ SELECT 1 FROM DUAL ({timer.lap()}초)")

        head(5, "대상 건수")
        # 조회 SQL 자체가 무거운지, 전송이 무거운지를 가른다. 여기서 오래 걸리면
        # SQL 문제고, 여기는 빠른데 6단계가 느리면 전송/네트워크 문제다.
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM ({sql})")
                total = cursor.fetchone()[0]
            print(f"  ✓ {int(total):,}건 ({timer.lap()}초)")
        except Exception as exc:
            print(f"  ✗ 건수 확인 실패 ({timer.lap()}초) — {describe_exception(exc)}")
            print("  → 조회 SQL 자체에 문제가 있습니다(테이블 권한·컬럼명·문법).")
            if args.full:
                traceback.print_exception(type(exc), exc, exc.__traceback__)
            return 1
    finally:
        connection.close()

    head(6, "실제 수집")
    timer.lap()
    try:
        rows = itsm_collector.collect(max_rows=args.rows or None)
    except Exception as exc:
        print(f"  ✗ 실패 ({timer.lap()}초)")
        print(f"  {describe_exception(exc)}")
        meta = itsm_collector.last_metadata
        if meta.get("failed_stage"):
            print(f"  단계={meta['failed_stage']} 수집={meta.get('fetched_before_failure')}건 "
                  f"단계별초={meta.get('stage_seconds')}")
        if args.full:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        return 1
    print(f"  ✓ {len(rows):,}건 ({timer.lap()}초)")
    print(f"  컬럼 {len(rows[0])}개: {', '.join(sorted(rows[0])[:12])} ...")
    print("\n모든 단계 통과. [ITSM 수집] 버튼도 같은 경로로 동작합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
