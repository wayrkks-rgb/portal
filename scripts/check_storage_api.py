"""스토리지 배열에 REST API 가 열려 있는지 한 번에 확인한다.

배열 세대에 따라 REST API 가 컨트롤러에 들어 있기도 하고, SVP 에만 있기도 하고,
아예 없기도 하다. 문서로 따지는 것보다 직접 물어보는 쪽이 확실하다. 장비 열 대를
한 번에 물어보고 어디가 되는지 표로 알려준다.

이 확인에 필요한 조회는 계정 없이도 답을 준다. 401 이 돌아와도 "REST 는 살아
있다" 는 뜻이므로 판단에는 충분하다.

쓰는 법::

    python scripts\\check_storage_api.py 10.0.0.11 10.0.0.12 svp-vsp5500
    python scripts\\check_storage_api.py --file storages.txt
    python scripts\\check_storage_api.py --file storages.txt --json > result.json

``--file`` 로 주는 파일은 한 줄에 하나씩 적는다. ``#`` 뒤는 설명으로 무시한다::

    10.0.0.11    # VSP 5500 SVP
    10.0.0.21    # E590
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

#: 배열·SVP 의 REST API. 계정 없이도 장비 목록을 돌려주는 조회다.
PATH = "/ConfigurationManager/v1/objects/storages"
DEFAULT_PORT = 443
TIMEOUT = 8


def _context() -> ssl.SSLContext:
    # 배열은 자체 서명 인증서를 쓴다. 살아 있는지만 보는 확인이므로 검증하지 않는다.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _tcp(host: str, port: int) -> str | None:
    """TCP 가 열려 있는지부터 본다. 막혀 있으면 REST 를 물어볼 것도 없다."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return None
    except OSError as exc:
        return f"{exc}"


def probe(target: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
    host, _, given_port = target.partition(":")
    port = int(given_port or port)
    result: dict[str, Any] = {"target": host, "port": port}

    tcp_error = _tcp(host, port)
    if tcp_error:
        result.update(rest="NO", reason="TCP_CLOSED", detail=tcp_error)
        return result

    url = f"https://{host}:{port}{PATH}"
    # 배열에는 직접 붙는다. 프록시 설정이 걸려 있으면 엉뚱한 응답이 돌아온다.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_context()),
    )
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # 인증을 요구한다는 것은 REST 가 살아 있다는 뜻이다.
            result.update(rest="YES", reason=f"HTTP_{exc.code}_AUTH_REQUIRED",
                          detail="계정을 넣으면 조회됩니다.")
        else:
            result.update(rest="NO", reason=f"HTTP_{exc.code}",
                          detail=exc.read().decode("utf-8", "replace")[:200])
        return result
    except urllib.error.URLError as exc:
        result.update(rest="NO", reason="TLS_OR_PROTOCOL", detail=str(exc.reason))
        return result
    except json.JSONDecodeError:
        result.update(rest="NO", reason="NOT_JSON",
                      detail="이 주소가 REST API 가 아닙니다(웹 콘솔일 수 있음).")
        return result

    systems = payload.get("data") or []
    result.update(
        rest="YES", reason="OK", count=len(systems),
        systems=[{
            "storageDeviceId": item.get("storageDeviceId"),
            "model": item.get("model"),
            "serialNumber": item.get("serialNumber"),
            "microcode": item.get("dkcMicroVersion"),
        } for item in systems],
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="스토리지 REST API 사용 가능 여부 확인")
    parser.add_argument("targets", nargs="*", help="배열 또는 SVP 주소. host 또는 host:port")
    parser.add_argument("--file", help="주소를 한 줄에 하나씩 적은 파일")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"기본 {DEFAULT_PORT}")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    args = parser.parse_args()

    targets = list(args.targets)
    if args.file:
        with open(args.file, encoding="utf-8") as stream:
            for line in stream:
                address = line.split("#")[0].strip()
                if address:
                    targets.append(address)
    if not targets:
        parser.error("확인할 주소를 하나 이상 주세요.")

    with ThreadPoolExecutor(max_workers=min(len(targets), 10)) as pool:
        results = list(pool.map(lambda t: probe(t, args.port), targets))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(f"{'주소':<24}{'REST':<7}{'사유':<26}내용")
    print("-" * 100)
    for item in results:
        systems = item.get("systems") or []
        detail = ", ".join(
            f"{s.get('model') or '?'} S/N {s.get('serialNumber') or '?'}"
            f" (ID {s.get('storageDeviceId') or '?'}, 마이크로코드 {s.get('microcode') or '?'})"
            for s in systems
        ) or str(item.get("detail") or "")
        print(f"{item['target']:<24}{item['rest']:<7}{item['reason']:<26}{detail[:60]}")

    usable = [i for i in results if i["rest"] == "YES"]
    print("-" * 100)
    print(f"REST 사용 가능 {len(usable)}대 / 확인 {len(results)}대")
    if len(usable) < len(results):
        print("사용 불가 장비는 CCI(raidcom) 또는 외부 Configuration Manager 로 갑니다.")
        print("장애(디스크 폴트) 는 REST 와 무관하게 모든 장비에서 SNMP trap 으로 받습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
