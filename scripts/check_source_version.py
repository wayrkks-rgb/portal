"""반입한 파일이 실제로 제자리에 들어갔는지 확인한다.

    .venv\\Scripts\\python.exe scripts\\check_source_version.py

압축을 풀었는데도 동작이 그대로라면, 대개 파일이 **덮어써지지 않은** 것이다.
윈도우 탐색기의 [압축 풀기]는 기본값이 *압축파일 이름의 하위 폴더* 라서,
저장소 루트에 푼다고 눌러도 실제로는 아래처럼 들어간다.

    portal\\portal_oracle_diag3\\asset_sync\\...   ← 여기로 들어감
    portal\\asset_sync\\...                        ← 여기가 덮어써져야 함

이 스크립트는 (1) 각 파일에 이번 변경이 들어 있는지, (2) 엉뚱한 하위 폴더에
풀린 사본이 있는지 확인하고, 있으면 옮기는 명령까지 알려준다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (파일, 그 파일에 있어야 하는 문구, 무엇이 들어간 변경인지)
EXPECTED = [
    ("asset_sync/collectors/oracle_itsm_collector.py", "COLLECTOR_BUILD",
     "수집 실패에 단계·경과시간·표식 남기기"),
    ("asset_sync/collectors/oracle_connection.py", "def describe_exception",
     "예외 사슬 펴기(DPY-1001 뒤의 진짜 원인)"),
    ("asset_sync/collectors/oracle_diagnostics.py", "def run_diagnostics",
     "CLI·웹 공용 단계별 점검"),
    ("asset_sync/routes/collection.py", "def _failure",
     "수집 API 오류에 원인 사슬 담기"),
    ("asset_sync/routes/admin.py", "diagnose/oracle",
     "웹 프로세스 안에서 도는 진단 API"),
    ("asset_sync/services/collection_service.py", "conn.commit()",
     "실패 기록이 되돌려지지 않게 확정"),
    ("application/__init__.py", "통합 웹 기동",
     "기동 로그에 소스·설정·표식 남기기"),
    ("scripts/diagnose_oracle_itsm.py", "run_diagnostics",
     "단계별 진단 스크립트"),
]

#: 하위 폴더에 잘못 풀렸는지 판단할 기준 파일.
LANDMARK = Path("asset_sync/collectors/oracle_itsm_collector.py")


def check_files() -> list[str]:
    """빠진 변경의 목록을 돌려준다."""
    missing: list[str] = []
    print("[반입 확인]")
    for relative, marker, purpose in EXPECTED:
        path = ROOT / relative
        if not path.exists():
            print(f"  ✗ {relative:<52} 파일이 없습니다")
            missing.append(relative)
            continue
        if marker not in path.read_text(encoding="utf-8", errors="replace"):
            print(f"  ✗ {relative:<52} 예전 파일 ({purpose})")
            missing.append(relative)
            continue
        print(f"  ✓ {relative:<52} {purpose}")
    return missing


def find_stray_copies() -> list[Path]:
    """저장소 안에 잘못 풀린 사본이 있는지 찾는다."""
    strays: list[Path] = []
    for candidate in ROOT.rglob(LANDMARK.name):
        if candidate == ROOT / LANDMARK:
            continue
        if ".git" in candidate.parts or "__pycache__" in candidate.parts:
            continue
        # <어딘가>/asset_sync/collectors/<파일> 이면 그 <어딘가> 가 잘못 풀린 위치다.
        try:
            base = candidate.parents[2]
        except IndexError:
            continue
        strays.append(base)
    return strays


def main() -> int:
    print(f"저장소 루트  {ROOT}\n")
    missing = check_files()

    strays = find_stray_copies()
    if strays:
        print("\n[잘못 풀린 사본]")
        for base in strays:
            print(f"  ! {base}")
            print(f"    → xcopy \"{base}\\*\" \"{ROOT}\\\" /E /Y")
        print("\n  압축이 하위 폴더에 풀렸습니다. 위 명령으로 옮긴 뒤 다시 확인하세요.")

    if missing:
        print("\n결과: 반입이 안 된 파일이 있습니다.")
        if not strays:
            print("  압축을 저장소 루트에 **덮어쓰기**로 풀었는지 확인하세요.")
            print(f"  기준 경로: {ROOT}")
        return 1

    print("\n결과: 파일은 모두 최신입니다.")
    print("  이래도 화면 동작이 그대로면 웹을 껐다 켜지 않은 것입니다.")
    print("  Flask 창을 닫고 scripts\\run_flask.bat 을 다시 실행한 뒤,")
    print("  logs\\asset_sync.log 에 '통합 웹 기동' 줄이 새로 찍히는지 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
