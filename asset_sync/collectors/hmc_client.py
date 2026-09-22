"""IBM HMC REST API 를 부르는 얇은 클라이언트.

폐쇄망이므로 새 라이브러리를 들이지 않는다. HMC 가 주고받는 것은 HTTPS 와 XML
뿐이고 둘 다 파이썬 표준 라이브러리에 있다(``urllib.request``, ``ssl``,
``xml.etree``). requests 를 쓰려면 휠을 따로 넣고 관리해야 하는데 그럴 이유가 없다.

HMC 인증은 두 단계다.

1. ``PUT /rest/api/web/Logon`` 에 사용자·비밀번호를 XML 로 보낸다.
2. 응답에 담긴 세션 토큰을 이후 요청의 ``X-API-Session`` 헤더에 넣는다.

토큰은 서버가 들고 있으므로 다 쓰면 ``DELETE /rest/api/web/Logon`` 으로 지운다.
지우지 않으면 HMC 쪽 세션이 남아 동시 세션 수 제한에 걸린다.
"""

from __future__ import annotations

import logging
import ssl
import urllib.error
import urllib.request
from typing import Any
from xml.etree import ElementTree

LOGGER = logging.getLogger(__name__)

#: HMC REST API 기본 포트. 5250(콘솔)·22(SSH) 와 다른 포트다.
DEFAULT_PORT = 12443

#: 로그온 본문. HMC 는 스키마 네임스페이스를 보고 요청 종류를 판단한다.
_LOGON_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<LogonRequest xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"'
    ' schemaVersion="V1_0"><UserID>{user}</UserID><Password>{password}</Password></LogonRequest>'
)
_LOGON_TYPE = "application/vnd.ibm.powervm.web+xml; type=LogonRequest"


class HMCError(RuntimeError):
    """HMC 와 이야기하다 실패했다. ``stage`` 로 어디서 막혔는지 구분한다."""

    def __init__(self, message: str, stage: str = "REQUEST") -> None:
        super().__init__(message)
        self.stage = stage


def local_name(tag: Any) -> str:
    """``{네임스페이스}PartitionName`` 에서 ``PartitionName`` 만 남긴다.

    HMC 는 펌웨어 버전마다 네임스페이스가 다르다. 네임스페이스까지 맞춰 찾으면
    장비를 올릴 때마다 코드가 깨지므로 태그 이름만 본다.
    """
    text = str(tag or "")
    return text.rsplit("}", 1)[-1]


def find_all(element: Any, name: str) -> list[Any]:
    """하위 어디에 있든 이름이 같은 요소를 모두 찾는다."""
    return [node for node in element.iter() if local_name(node.tag) == name]


def find_text(element: Any, name: str, default: Any = None) -> Any:
    for node in element.iter():
        if local_name(node.tag) == name and (node.text or "").strip():
            return node.text.strip()
    return default


class HMCClient:
    """HMC 한 대와의 세션. ``with`` 로 쓰면 끝날 때 로그오프한다."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = DEFAULT_PORT,
        verify_tls: bool = False,
        timeout_seconds: int = 30,
        opener: Any = None,
    ) -> None:
        self.host = str(host or "").strip()
        self.username = str(username or "")
        self.password = str(password or "")
        self.port = int(port or DEFAULT_PORT)
        self.verify_tls = bool(verify_tls)
        self.timeout = int(timeout_seconds or 30)
        self.token: str | None = None
        self._opener = opener or self._build_opener()

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def _build_opener(self) -> Any:
        context = ssl.create_default_context()
        if not self.verify_tls:
            # HMC 는 보통 자체 서명 인증서를 쓴다. 사설 CA 를 신뢰 목록에 넣기
            # 전까지는 검증을 끄고 쓴다 -- 폐쇄망 안이고, 주소는 운영자가 직접 적는다.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def _open(self, request: Any, stage: str) -> Any:
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # pragma: no cover - 본문이 없을 수도 있다
                pass
            if exc.code in (401, 403):
                raise HMCError(f"인증에 실패했습니다(HTTP {exc.code}). 계정과 비밀번호를 확인하세요.", "AUTH") from exc
            raise HMCError(f"HMC 가 HTTP {exc.code} 를 돌려주었습니다. {body}".strip(), stage) from exc
        except urllib.error.URLError as exc:
            raise HMCError(
                f"{self.base_url} 에 연결하지 못했습니다: {exc.reason}."
                f" 방화벽에서 TCP {self.port} 가 열려 있는지 확인하세요.",
                "CONNECT",
            ) from exc

    def logon(self) -> str:
        if not self.host:
            raise HMCError("HMC 주소가 비어 있습니다.", "CONFIG")
        if not self.username or not self.password:
            raise HMCError("HMC 조회 계정과 비밀번호가 필요합니다.", "CONFIG")
        body = _LOGON_BODY.format(user=_escape(self.username), password=_escape(self.password))
        request = urllib.request.Request(
            f"{self.base_url}/rest/api/web/Logon",
            data=body.encode("utf-8"),
            method="PUT",
            headers={"Content-Type": _LOGON_TYPE, "Accept": "application/vnd.ibm.powervm.web+xml"},
        )
        with self._open(request, "LOGON") as response:
            payload = response.read()
        token = self._read_token(payload)
        if not token:
            raise HMCError("로그온 응답에서 세션 토큰을 찾지 못했습니다.", "LOGON")
        self.token = token
        return token

    @staticmethod
    def _read_token(payload: bytes) -> str | None:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise HMCError(f"로그온 응답을 읽지 못했습니다: {exc}", "LOGON") from exc
        return find_text(root, "X-API-Session")

    def get(self, path: str) -> Any:
        """XML 을 받아 파싱한 루트를 돌려준다."""
        if not self.token:
            self.logon()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        request = urllib.request.Request(
            url, method="GET",
            headers={"X-API-Session": str(self.token), "Accept": "application/atom+xml"},
        )
        with self._open(request, "QUERY") as response:
            payload = response.read()
        try:
            return ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise HMCError(f"{path} 응답을 읽지 못했습니다: {exc}", "QUERY") from exc

    def logoff(self) -> None:
        """세션을 지운다. 실패해도 넘어간다 -- 시간이 지나면 HMC 가 알아서 지운다."""
        if not self.token:
            return
        request = urllib.request.Request(
            f"{self.base_url}/rest/api/web/Logon", method="DELETE",
            headers={"X-API-Session": str(self.token)},
        )
        try:
            self._opener.open(request, timeout=self.timeout).close()
        except Exception:
            LOGGER.debug("HMC 로그오프에 실패했습니다.", exc_info=True)
        finally:
            self.token = None

    def __enter__(self) -> "HMCClient":
        self.logon()
        return self

    def __exit__(self, *_: Any) -> None:
        self.logoff()


def _escape(value: str) -> str:
    return (
        str(value).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
