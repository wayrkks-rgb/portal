"""일일 자동 배치가 실제로 돌았는지, 무엇을 했는지 확인한다.

"배치를 걸었는데 적용이 안 된 것 같다" 를 확인하려면 다섯 군데를 봐야 한다.
한 군데만 보면 잘못 판단한다 -- 예를 들어 Windows 작업은 제 시간에 깨어났는데
배치가 스스로 꺼져 있어 빠졌을 수도 있고, 수집은 됐는데 마지막에 실패해 저장이
안 됐을 수도 있다.

1. Windows 작업 스케줄러에 등록돼 있고, 누구로·어떤 방식으로 돌며, 마지막 결과가 무엇인가
2. 화면에서 배치를 켜 두었는가 (꺼 두면 작업은 깨어나도 배치가 스스로 빠진다)
3. DB 에 배치 기록이 남았는가. 남았으면 상태가 무엇인가
4. 그 배치가 만든 스냅샷에 실제로 몇 건이 들어왔는가
5. 수집 실행 기록과 남아 있는 배치 잠금
6. 콘솔 로그의 마지막 실행 한 벌 (실행 계정 · 사전 점검 · 결과)

맨 끝에 **원인 → 조치** 를 모아 보여준다. 위를 다 읽지 않아도 무엇을 하면 되는지
알 수 있어야 한다.

쓰는 법::

    .venv\\Scripts\\python.exe scripts\\check_daily_batch.py
    .venv\\Scripts\\python.exe scripts\\check_daily_batch.py --days 7
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LINE = "-" * 78

#: (원인, 조치). 점검하면서 모으고 맨 끝에 한꺼번에 보여준다.
FINDINGS: list[tuple[str, str]] = []


def found(cause: str, action: str) -> None:
    FINDINGS.append((cause, action))


def _rows(conn, sql: str, params=None) -> list[dict]:
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except Exception as exc:                        # 표가 아직 없을 수 있다
        print(f"  (조회 실패: {exc})")
        return []


def _text(value) -> str:
    return "-" if value in (None, "") else str(value)


def _when(value) -> str:
    """ISO 시각을 읽기 쉽게. 오늘이면 시간만 보여준다."""
    text = str(value or "")
    if not text:
        return "-"
    return text.replace("T", " ")[:19]


def _elapsed(started, ended) -> str:
    try:
        begin = datetime.fromisoformat(str(started))
        finish = datetime.fromisoformat(str(ended))
    except (TypeError, ValueError):
        return "-"
    return f"{(finish - begin).total_seconds():.0f}초"


def check_windows_task(config) -> None:
    from asset_sync import scheduler

    print("1) Windows 작업 스케줄러")
    settings = scheduler.settings(config.scheduler)
    described = scheduler.describe(settings["task_name"])
    print(f"  작업명       : {settings['task_name']}")
    print(f"  등록 여부    : {'등록됨' if described.get('registered') else '등록 안 됨'}")
    if described.get("supported") and not described.get("registered"):
        print("  → scripts\\register_daily_task.bat 을 관리자 권한으로 한 번 실행하세요.")
        found("Windows 작업이 등록돼 있지 않습니다.",
              "scripts\\register_daily_task.bat 을 관리자 권한으로 실행하세요.")
    for key, label in (("next_run", "다음 실행"), ("last_run", "마지막 실행"),
                       ("state", "상태"), ("run_as", "실행 계정"), ("logon_mode", "로그온 방식")):
        if described.get(key):
            print(f"  {label:<12} : {described[key]}")
    if described.get("last_result"):
        meaning = scheduler.explain_result(described["last_result"])
        print(f"  {'마지막 결과':<12} : {meaning['hex'] or described['last_result']} — {meaning['meaning']}")
        if meaning["action"]:
            print(f"  {'':<12}   → {meaning['action']}")
        if meaning["code"] not in (None, 0x0, 0x41301, 0x41303):
            found(f"작업 스케줄러 마지막 결과가 {meaning['hex']} ({meaning['meaning']}) 입니다.",
                  meaning["action"] or "logs\\daily_batch_console.log 를 보세요.")
    run_as = str(described.get("run_as") or "").upper()
    if run_as.endswith("SYSTEM"):
        print("  ※ SYSTEM 으로 돕니다. PowerCLI 가 C:\\Program Files\\WindowsPowerShell\\Modules 에")
        print("     없고 관리자 프로필(Documents) 아래에만 있으면 vCenter 수집이 실패합니다.")
    logon = str(described.get("logon_mode") or "")
    if "Interactive only" in logon or "대화형" in logon:
        print("  ※ '사용자가 로그온할 때만 실행' 입니다. 07:00 에 아무도 로그온해 있지 않으면 안 돕니다.")
        found("작업이 '사용자가 로그온할 때만 실행' 으로 등록돼 있습니다.",
              "scripts\\register_daily_task.bat 으로 다시 등록하세요(로그온과 무관하게 SYSTEM 으로 돕니다).")
    if described.get("reason"):
        print(f"  비고         : {described['reason']}")
    if described.get("command"):
        print(f"  실행 대상    : {described['command']}")
    print()


def check_switch(config) -> None:
    from asset_sync import scheduler

    print("2) 화면에서 켜 둔 상태")
    settings = scheduler.settings(config.scheduler)
    enabled = settings["enabled"]
    print(f"  일일 배치    : {'켜짐' if enabled else '꺼짐'}")
    print(f"  실행 시각    : {settings['daily_time']}")
    if not enabled:
        print("  → 꺼져 있으면 Windows 작업이 깨어나도 배치가 스스로 빠집니다.")
        print("     관리 → 연계 설정 → 일일 실행 및 로그 에서 켜세요.")
        found("화면에서 일일 배치가 꺼져 있습니다(작업은 깨어나도 배치가 스스로 빠집니다).",
              "관리 → 연계 설정 → 일일 실행 및 로그 에서 [일일 배치]를 켜세요.")
    print()


def _reasons(batch: dict) -> list[dict]:
    """배치 기록에 적힌 '왜 SUCCESS 가 아닌지'."""
    try:
        meta = json.loads(batch.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return []
    found = meta.get("status_reasons") or []
    return [item for item in found if isinstance(item, dict)]


def check_batches(conn, days: int) -> list[dict]:
    print(f"3) DB 에 남은 배치 기록 (최근 {days}일)")
    since = (date.today() - timedelta(days=days)).isoformat()
    batches = _rows(
        conn,
        "SELECT * FROM daily_batch_run WHERE batch_date >= ?"
        " ORDER BY started_at DESC, id DESC LIMIT 50",
        (since,),
    )
    if not batches:
        print("  기록이 없습니다. 배치가 한 번도 끝까지 돌지 않았습니다.")
        print("  → 수동으로 한 번 실행해 보세요: scripts\\run_daily_batch.bat")
        print()
        found(f"최근 {days}일 동안 DB 에 배치 기록이 없습니다(배치가 시작조차 못 했습니다).",
              "6) 로그의 BATCH START 아래를 보세요. 줄이 아예 없으면 작업이 깨어나지 않은 것입니다(1) 참고).")
        return []

    print(f"  {'일자':<12}{'시작':<20}{'상태':<10}{'소요':<8}자원사용률")
    for item in batches:
        print(f"  {_text(item['batch_date']):<12}{_when(item['started_at']):<20}"
              f"{_text(item['status']):<10}{_elapsed(item['started_at'], item['ended_at']):<8}"
              f"{_text(item.get('resource_usage_status'))}")
        # SUCCESS 가 아닌 이유. PARTIAL_SUCCESS 는 "아무것도 안 됐다" 가 아니라
        # "한 군데가 모자라다" 는 뜻이므로 어디가 모자란지 적어 준다.
        for reason in _reasons(item):
            print(f"      · [{reason.get('area')}] {reason.get('message')}")
        errors = item.get("error_json") or "{}"
        if errors not in ("{}", "", None):
            try:
                parsed = json.loads(errors)
            except (TypeError, ValueError):
                parsed = {"raw": errors}
            for key, value in (parsed or {}).items():
                print(f"      ! {key}: {str(value)[:200]}")
        if not item.get("ended_at"):
            print("      ! 끝나지 않은 상태로 남아 있습니다(중간에 프로세스가 죽었을 수 있음).")
            if item is batches[0]:
                found("가장 최근 배치가 끝나지 않은 채 남아 있습니다(중간에 죽었을 수 있음).",
                      "작업 관리자에서 멈춘 python.exe/powershell.exe 를 종료하고 5) 의 잠금을 확인하세요.")
    print()
    return batches


def check_snapshots(conn, batches: list[dict]) -> None:
    print("4) 그 배치가 만든 스냅샷")
    if not batches:
        print("  확인할 배치가 없습니다.\n")
        return
    latest = batches[0]
    found = False
    for column, label in (("itsm_snapshot_id", "ITSM"), ("vcenter_snapshot_id", "vCenter")):
        snapshot_id = latest.get(column)
        if not snapshot_id:
            print(f"  {label:<8}: 스냅샷이 만들어지지 않았습니다(수집 실패 또는 미실행).")
            continue
        rows = _rows(conn, "SELECT * FROM snapshot WHERE id=?", (int(snapshot_id),))
        if not rows:
            print(f"  {label:<8}: 스냅샷 {snapshot_id} 을 찾을 수 없습니다.")
            continue
        found = True
        snapshot = rows[0]
        print(f"  {label:<8}: {_text(snapshot.get('snapshot_date'))} "
              f"· {int(snapshot.get('record_count') or 0):,}건 "
              f"· 상태 {_text(snapshot.get('status'))} "
              f"· 수집 {_when(snapshot.get('collected_at'))}")

    # 화면이 실제로 읽는 것은 '가장 최근 스냅샷' 이다. 배치가 만든 것과 다르면
    # 화면에 반영이 안 된 것처럼 보인다.
    print()
    print("  화면이 읽는 최신 스냅샷(이것이 곧 화면에 보이는 값)")
    for source in ("ITSM", "RVTOOLS"):
        rows = _rows(
            conn,
            "SELECT * FROM snapshot WHERE source=? AND status='SUCCESS'"
            " ORDER BY snapshot_date DESC, id DESC LIMIT 1",
            (source,),
        )
        if not rows:
            print(f"  {source:<8}: 정상 스냅샷이 없습니다. 화면에 아무것도 나오지 않습니다.")
            continue
        snapshot = rows[0]
        age = ""
        try:
            days = (date.today() - date.fromisoformat(str(snapshot["snapshot_date"])[:10])).days
            age = f" ({days}일 전)" if days else " (오늘)"
        except (TypeError, ValueError):
            pass
        print(f"  {source:<8}: {_text(snapshot.get('snapshot_date'))}{age}"
              f" · {int(snapshot.get('record_count') or 0):,}건 · id {snapshot.get('id')}")
    if not found:
        print("  → 스냅샷이 없으면 화면은 예전 값을 계속 보여줍니다.")
    print()


def check_runs(conn, days: int) -> None:
    print(f"5) 수집 실행 기록 (최근 {days}일)")
    since = (datetime.now() - timedelta(days=days)).isoformat()
    runs = _rows(
        conn,
        "SELECT * FROM collection_run WHERE started_at >= ?"
        " ORDER BY started_at DESC, id DESC LIMIT 30",
        (since,),
    )
    if not runs:
        print("  기록이 없습니다.\n")
        return
    print(f"  {'출처':<10}{'시작':<20}{'상태':<10}{'건수':>8}  오류")
    for item in runs:
        print(f"  {_text(item.get('source')):<10}{_when(item.get('started_at')):<20}"
              f"{_text(item.get('status')):<10}{int(item.get('record_count') or 0):>8,}  "
              f"{str(item.get('error_message') or '')[:100]}")
    print()


def check_lock(conn) -> None:
    """남아 있는 배치 잠금. 죽은 배치가 남긴 잠금은 4시간 뒤 저절로 풀린다."""
    print("5-1) 배치 잠금(process_lock)")
    rows = _rows(conn, "SELECT * FROM process_lock WHERE lock_name='daily_batch'")
    if not rows:
        print("  잠금 없음 — 다음 배치가 바로 돌 수 있습니다.\n")
        return
    lock = rows[0]
    expires = str(lock.get("expires_at") or "")
    alive = expires > datetime.now().isoformat()
    print(f"  보유자 {_text(lock.get('owner'))} · 잡은 시각 {_when(lock.get('acquired_at'))}"
          f" · 만료 {_when(expires)} · {'유효' if alive else '만료됨(다음 배치가 회수)'}")
    if alive:
        found(f"배치 잠금이 {_when(expires)} 까지 잡혀 있어 그 전에는 배치가 SKIPPED 로 빠집니다.",
              "돌고 있는 배치가 없다면(작업 관리자에 python.exe 없음) 만료를 기다리거나 "
              "scripts\\release_batch_lock.py --release 로 푸세요.")
    print()


def last_run_block(path: Path) -> list[str]:
    """로그에서 마지막 'BATCH START' 부터 끝까지. 한 번의 실행을 통째로 본다."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    starts = [index for index, line in enumerate(content) if "BATCH START" in line]
    return content[starts[-1]:] if starts else content[-15:]


def check_logs(config, lines: int = 15) -> None:
    print("6) 콘솔 로그 — 마지막 실행 한 벌(BATCH START 부터)")
    candidates = [
        config.resolve("logs") / "daily_batch_console.log",
        config.resolve("logs") / "asset_sync.log",
    ]
    for path in candidates:
        if not path.exists():
            continue
        print(f"  {path}")
        block = last_run_block(path)
        if not block:
            print("  (읽지 못했습니다)")
            continue
        for line in block[-80:]:
            print(f"    {line}")
        text = "\n".join(block)
        if "VMware.VimAutomation.Core" in text and ("찾지 못했습니다" in text or "not found" in text.lower()):
            found("실행 계정에서 PowerCLI 모듈을 찾지 못했습니다(로그의 사전 점검).",
                  "관리자 PowerShell 로 PowerCLI 모듈 폴더를 C:\\Program Files\\WindowsPowerShell\\Modules 에 복사하세요.")
        if "[STOP]" in text:
            found("배치가 파이썬 가상환경 문제로 시작 전에 멈췄습니다.",
                  "scripts\\install_offline.bat 을 다시 실행하세요.")
        print()
        return
    print(f"  로그 파일이 없습니다. 찾아본 곳: {', '.join(str(p) for p in candidates)}")
    print("  → 배치가 한 번도 실행되지 않았을 수 있습니다.")
    print()
    found("배치 콘솔 로그가 없습니다(작업이 한 번도 배치 파일을 실행하지 않았습니다).",
          "1) 의 등록 여부·실행 계정·마지막 결과를 확인하고, scripts\\run_daily_batch.bat 을 직접 한 번 실행해 보세요.")


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 자동 배치 점검")
    parser.add_argument("--days", type=int, default=3, help="며칠치를 볼지(기본 3)")
    args = parser.parse_args()

    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager

    config = load_config()
    print(LINE)
    print(f"일일 자동 배치 점검 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"설정 위치: {config.root_dir}")
    print(LINE)

    check_windows_task(config)
    check_switch(config)

    manager = create_manager(config)
    manager.initialize()
    with manager.connect() as conn:
        batches = check_batches(conn, args.days)
        check_snapshots(conn, batches)
        check_runs(conn, args.days)
        check_lock(conn)
    check_logs(config)

    print(LINE)
    if batches and str(batches[0].get("status")) == "SUCCESS":
        print("가장 최근 배치는 SUCCESS 입니다. 화면에 안 보이면 위 4) 의 '화면이 읽는")
        print("최신 스냅샷' 날짜를 확인하세요. 그 날짜가 곧 화면에 보이는 값입니다.")
    elif batches:
        status = batches[0].get("status")
        reasons = _reasons(batches[0])
        if status == "PARTIAL_SUCCESS" and reasons:
            print("가장 최근 배치는 PARTIAL_SUCCESS 입니다. 수집은 됐고, 아래가 모자랍니다.")
            for reason in reasons:
                print(f"  · [{reason.get('area')}] {reason.get('message')}")
            print()
            print("※ AIX(HMC) 는 아직 일일 배치에 들어 있지 않습니다. PARTIAL_SUCCESS 의")
            print("   원인이 될 수 없습니다.")
        elif status == "PARTIAL_SUCCESS":
            print("가장 최근 배치는 PARTIAL_SUCCESS 인데 사유가 기록돼 있지 않습니다")
            print("(사유 기록 전 버전에서 돈 배치입니다). 다음 배치부터 사유가 남습니다.")
            print("지금 바로 보려면: scripts\\run_daily_batch.bat 을 한 번 실행하세요.")
        else:
            print(f"가장 최근 배치 상태가 {status} 입니다. 위 3) 의 오류 줄을 보세요.")
    else:
        print("배치 기록이 없습니다. 2) 의 켜짐 여부와 1) 의 등록 여부를 먼저 확인하세요.")
    print(LINE)
    print("진단 요약 — 원인 → 조치")
    if FINDINGS:
        seen = set()
        for number, (cause, action) in enumerate(
            [item for item in FINDINGS if not (item in seen or seen.add(item))], start=1
        ):
            print(f"  {number}. {cause}")
            print(f"     → {action}")
    else:
        print("  찾은 문제가 없습니다. 그래도 화면에 안 보이면 4) 의 '화면이 읽는 최신 스냅샷' 날짜를 보세요.")
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
