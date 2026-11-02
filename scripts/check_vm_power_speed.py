"""[현재 VM 상태 보기] 가 이 환경에서 몇 초 걸리는지 미리 잰다.

    python scripts\\check_vm_power_speed.py              REST · PowerCLI 둘 다 잰다
    python scripts\\check_vm_power_speed.py --vms 20     통합기마다 VM 20대로 잰다
    python scripts\\check_vm_power_speed.py --method POWERCLI

마지막 vCenter 스냅샷에서 통합기마다 VM 몇 대를 골라, 화면 버튼과 똑같은 코드로
지금 전원을 읽고 단계별 시간(모듈 로딩 · 접속 · 조회)을 찍는다. 아무것도 바꾸지 않는다.

판단 기준(제안): 10초 안이면 버튼으로 쓸 만하고, 30초를 넘으면 의미가 적다.
PowerCLI 가 느린 이유는 대개 '모듈 로딩'이다. 그때는 설정
``vcenter.live_power_method`` 를 AUTO(기본) 나 REST 로 둔다.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="현재 VM 전원 조회 속도 점검")
    parser.add_argument("--vms", type=int, default=10, help="통합기마다 고를 VM 수 (기본 10)")
    parser.add_argument("--method", choices=["BOTH", "REST", "POWERCLI"], default="BOTH")
    args = parser.parse_args()

    from asset_sync.config import load_config
    from asset_sync.db.manager import create_manager
    from asset_sync.repositories import AssetRepository
    from asset_sync.services.live_power_service import LivePowerService

    config = load_config()
    with create_manager(config).connect() as conn:
        repo = AssetRepository(conn)
        snapshot = repo.latest_snapshot("RVTOOLS")
        records = repo.load_rv_records(int(snapshot["id"])) if snapshot else {}
    if not records:
        print("vCenter 스냅샷이 없습니다. 배치를 한 번 돌린 뒤 다시 실행하세요.")
        return 1

    picked: dict[str, list[dict]] = {}
    for key, record in records.items():
        group = picked.setdefault(str(record.get("vcenter") or ""), [])
        if len(group) < args.vms:
            group.append(dict(record, asset_key=key))
    vms = [vm for group in picked.values() for vm in group]
    print(f"스냅샷 {snapshot['collected_at']} · 통합기 {len(picked)}곳 · VM {len(vms)}대로 잽니다.\n")

    methods = ["REST", "POWERCLI"] if args.method == "BOTH" else [args.method]
    worst = 0.0
    for method in methods:
        config.rvtools["live_power_method"] = method
        started = time.monotonic()
        result = LivePowerService(config).lookup(vms)
        print(f"== {method} · 전체 {time.monotonic() - started:.1f}초 · {result['status']}")
        for vc in result["vcenters"]:
            attempt = (vc.get("attempts") or [{}])[-1]
            steps = " · ".join(f"{k} {v}s" for k, v in (attempt.get("timings") or {}).items())
            line = f"   {vc.get('vcenter_label') or vc['vcenter_id']}: {vc['seconds']}초"
            if vc["status"] == "SUCCESS":
                line += f" [{steps}]"
                worst = max(worst, float(vc["seconds"]))
            else:
                line += f" 실패 — {vc.get('error')}"
            print(line)
        found = sum(1 for row in result["results"] if row.get("found"))
        print(f"   찾은 VM {found}/{len(vms)}\n")

    if worst:
        verdict = "버튼으로 쓸 만합니다" if worst <= 10 else ("쓸 수는 있으나 느립니다" if worst <= 30 else "너무 느립니다")
        print(f"판단: 가장 느린 통합기 {worst:.1f}초 → {verdict}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
