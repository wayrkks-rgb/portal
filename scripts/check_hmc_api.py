"""HMC REST API 연동이 어느 단계에서 막히는지 한 단계씩 확인한다.

    .venv\\Scripts\\python.exe scripts\\check_hmc_api.py                 연계 설정에 등록된 HMC 전부
    .venv\\Scripts\\python.exe scripts\\check_hmc_api.py --id HMC01
    .venv\\Scripts\\python.exe scripts\\check_hmc_api.py --host 10.0.0.30 --user hmcviewer   (비밀번호는 물어봄)

"방화벽 · 계정은 문제없는데 오류" 가 나는 흔한 원인을 차례로 본다.

  1. 프록시   Windows 인터넷 옵션(IE) 프록시가 잡혀 있으면 요청이 프록시로 간다
  2. 이름 · TCP  주소 · 포트(기본 12443)에 닿는가
  3. TLS     오래된 HMC 는 파이썬 3.13(OpenSSL 3)이 막는 TLS 를 쓴다 → legacy_tls
  4. 로그온   계정 · 'Allow remote access via the web' · Accept 헤더
  5. 프레임   /rest/api/uom/ManagedSystem (느리면 시간 초과)
  6. LPAR     프레임마다 /LogicalPartition (LPAR 없는 프레임은 204 빈 응답)
  7. 사용률    PCM(성능 데이터) 이 켜져 있는가 — 켜져 있어야 CPU 사용률을 가져올 수 있다

비밀번호 · 토큰은 화면에 찍지 않는다. 조회만 하고 아무것도 바꾸지 않는다.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, FAIL, WARN = "  [통과]", "  [실패]", "  [주의]"


def step(title: str) -> None:
    print(f"\n- {title}")


def check(entry: dict, settings: dict) -> bool:
    from asset_sync.collectors.hmc_client import HMCClient, HMCError, find_all, find_text
    from asset_sync.collectors.hmc_collector import _entry_uuid

    host, port = entry["host"], int(entry.get("port") or 12443)
    timeout = int(settings.get("timeout_seconds", 30) or 30)
    print(f"\n===== {entry.get('id') or host}  ({host}:{port}) =====")

    step("1) 프록시")
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    if proxies:
        print(f"{WARN} 이 PC 에 프록시가 잡혀 있습니다: {', '.join(f'{k}={v}' for k, v in proxies.items())}")
        print("         통합 웹은 HMC 요청에 프록시를 쓰지 않습니다(hmc.use_proxy: false, 기본).")
    else:
        print(f"{OK} 프록시 없음")

    step("2) 이름 · TCP")
    try:
        address = socket.gethostbyname(host)
        started = time.monotonic()
        with socket.create_connection((address, port), timeout=10):
            print(f"{OK} {host} → {address}:{port} 연결 ({(time.monotonic() - started) * 1000:.0f} ms)")
    except OSError as exc:
        print(f"{FAIL} {host}:{port} 에 닿지 않습니다: {exc}")
        print("         포트가 12443 이 맞는지, HMC 의 원격 웹 접근(Remote Operation)이 켜져 있는지 확인하세요.")
        return False

    step("3) TLS")
    legacy_needed = False
    for legacy in (bool(entry.get("legacy_tls", settings.get("legacy_tls", False))), True):
        context = HMCClient(host, "x", "x", port=port, legacy_tls=legacy).ssl_context()
        try:
            with socket.create_connection((address, port), timeout=10) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    cert = tls.getpeercert(binary_form=False) or {}
                    print(f"{OK} {tls.version()} · {tls.cipher()[0]}"
                          + (" (legacy_tls 로)" if legacy else "")
                          + (f" · 인증서 만료 {cert.get('notAfter')}" if cert.get("notAfter") else ""))
            legacy_needed = legacy and not bool(entry.get("legacy_tls", settings.get("legacy_tls", False)))
            break
        except ssl.SSLError as exc:
            if legacy:
                print(f"{FAIL} TLS 연결 실패(legacy 허용해도): {exc}")
                return False
            print(f"{WARN} 기본 TLS 로는 실패: {exc} → legacy_tls 로 다시 시도합니다")
    if legacy_needed:
        print(f"{WARN} 이 HMC 는 legacy_tls 가 필요합니다. 설정 hmc.legacy_tls: true (또는 이 HMC 항목에 legacy_tls: true)")

    step("4) 로그온")
    client = HMCClient(host, entry["username"], entry["password"], port=port, timeout_seconds=timeout,
                       verify_tls=bool(entry.get("verify_tls", False)),
                       legacy_tls=legacy_needed or bool(entry.get("legacy_tls", settings.get("legacy_tls", False))))
    try:
        started = time.monotonic()
        client.logon()
        print(f"{OK} 토큰 받음 ({time.monotonic() - started:.1f}초)")
    except HMCError as exc:
        print(f"{FAIL} [{exc.stage}] {exc}")
        return False

    ok = True
    try:
        step("5) 프레임 (ManagedSystem)")
        try:
            started = time.monotonic()
            feed = client.get("/rest/api/uom/ManagedSystem")
            frames = [(find_text(e, "SystemName"), _entry_uuid(e), find_text(e, "State")) for e in find_all(feed, "entry")]
            elapsed = time.monotonic() - started
            print(f"{OK} {len(frames)}대 ({elapsed:.1f}초): " + ", ".join(f"{n}({s})" for n, _, s in frames[:10]))
            if elapsed > timeout * 0.7:
                print(f"{WARN} 응답이 느립니다. hmc.timeout_seconds 를 {int(elapsed * 2)} 이상으로 늘리세요.")
        except HMCError as exc:
            print(f"{FAIL} [{exc.stage}] {exc}")
            return False

        step("6) LPAR")
        for name, uuid, _ in frames:
            if not uuid:
                continue
            try:
                started = time.monotonic()
                lpars = find_all(client.get(f"/rest/api/uom/ManagedSystem/{uuid}/LogicalPartition"), "entry")
                print(f"{OK} {name}: LPAR {len(lpars)}개 ({time.monotonic() - started:.1f}초)")
            except HMCError as exc:
                ok = False
                print(f"{FAIL} {name}: [{exc.stage}] {exc}")

        step("7) 사용률(PCM) — 통합서버 자원사용현황에 CPU 사용률을 넣으려면 필요")
        try:
            prefs = client.get("/rest/api/pcm/preferences")
            systems = find_all(prefs, "ManagedSystemPcmPreference")
            if not systems:
                print(f"{WARN} PCM 설정을 읽지 못했습니다(HMC 버전에 따라 경로가 다름). 할당량(구성값)은 그래도 가져옵니다.")
            for node in systems:
                system = find_text(node, "SystemName") or "?"
                ltm = find_text(node, "LongTermMonitorEnabled") or find_text(node, "AggregationEnabled") or "?"
                mark = OK if str(ltm).lower() == "true" else WARN
                print(f"{mark} {system}: 성능 수집 {'켜짐' if str(ltm).lower() == 'true' else '꺼짐(' + str(ltm) + ')'}")
        except HMCError as exc:
            print(f"{WARN} PCM 조회 실패 [{exc.stage}]: {exc}")
            print("         사용률 없이 할당량(구성값)만 쓸 수 있습니다. HMC 에서 '성능 및 용량 모니터링' 을 켜면 됩니다.")
    finally:
        client.logoff()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="HMC REST API 단계별 점검")
    parser.add_argument("--id", help="연계 설정의 HMC id")
    parser.add_argument("--host", help="설정 대신 직접 줄 HMC 주소")
    parser.add_argument("--user", help="--host 와 함께 쓸 계정")
    parser.add_argument("--port", type=int, default=12443)
    args = parser.parse_args()

    from asset_sync.config import load_config

    settings = dict(load_config().hmc or {})
    if args.host:
        entries = [{"id": args.host, "host": args.host, "port": args.port, "username": args.user or input("계정: "),
                    "password": getpass.getpass("비밀번호: ")}]
    else:
        entries = [e for e in settings.get("endpoints") or [] if not args.id or str(e.get("id")) == args.id]
    if not entries:
        print("점검할 HMC 가 없습니다. 연계 설정에 등록하거나 --host 를 주세요.")
        return 1
    results = [check(entry, settings) for entry in entries]
    print(f"\n결과: {sum(results)}/{len(results)} 대 통과. 실패한 단계의 메시지를 그대로 알려 주시면 됩니다.")
    return 0 if all(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
