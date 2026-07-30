"""Oracle 접속과 자산 조회 SQL 을 한 번에 점검한다.

    .venv\\Scripts\\python.exe scripts\\check_oracle_connection.py

성공하면 샘플 건수와 컬럼 목록을, 실패하면 어느 단계에서 막혔는지 출력한다.
실패를 traceback 대신 JSON 으로 알려준다 -- 접속 정보가 아직 없는 상태가 흔한
출발점이고, 그때 파이썬 traceback 을 보여줄 이유가 없다. 실패하면 종료 코드 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asset_sync.collectors.oracle_itsm_collector import OracleITSMCollector
from asset_sync.config import load_config


def main() -> int:
    config = load_config()
    mode = str(config.itsm.get("collection_mode", "ORACLE")).upper()
    if mode != "ORACLE":
        print(
            json.dumps(
                {
                    "status": "SKIPPED",
                    "reason": f"ITSM 수집모드가 {mode} 라서 Oracle 직접조회를 쓰지 않습니다.",
                    "action": "관리 → 연동정보 관리 에서 ITSM 수집모드를 자동수집(ORACLE) 으로 바꾸세요.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        result = OracleITSMCollector(config).test_connection()
    except Exception as exc:
        # 예외 사슬의 가장 안쪽이 실제 이유다(드라이버 미설치, 로그인 실패, ORA-00942 등).
        root = exc
        while root.__cause__ is not None:
            root = root.__cause__
        payload = {"status": "FAILED", "error": str(exc)}
        if str(root) != str(exc):
            payload["root_cause"] = str(root)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
