"""도메인 WAS 호출 클라이언트 (BFF).

브라우저가 각 WAS 를 직접 호출하지 않고 통합 웹 서버가 대신 호출한다. 그래서
WAS 를 사설망에 둘 수 있고, 인증 경계가 통합 웹 한 곳으로 모이고, CORS 설정이
필요 없다.

모든 실패는 예외가 아니라 ``ModuleResponse`` 로 돌려준다. 통합 대시보드는 모듈
하나가 죽어도 나머지를 그려야 하므로 호출자가 상태를 보고 판단해야 한다.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import requests
from requests.adapters import HTTPAdapter

from .registry import ModuleConfigError, ModuleDefinition, ModuleRegistry
from .tokens import issue_token, mask_token

LOGGER = logging.getLogger(__name__)

# 상태값은 기존 수집 파이프라인(PARTIAL_SUCCESS/COLLECTION_GAP)과 같은 결로 맞춘다.
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_UNREACHABLE = "UNREACHABLE"
STATUS_SKIPPED = "SKIPPED"

# 프록시 시 전달하지 않는 헤더. 홉 단위 헤더와 통합 웹의 인증 정보는 넘기지 않는다.
_BLOCKED_REQUEST_HEADERS = {
    "host", "cookie", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "content-length", "authorization",
}
_BLOCKED_RESPONSE_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "content-encoding",
    "content-length", "set-cookie", "proxy-authenticate",
}
ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE")


@dataclass(slots=True)
class ModuleResponse:
    module_id: str
    status: str
    http_status: int | None = None
    data: Any = None
    error: str | None = None
    elapsed_ms: int = 0
    content_type: str = "application/json"
    raw: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


class ModuleClient:
    """레지스트리에 등록된 모듈을 호출한다. 커넥션은 Session 으로 재사용한다."""

    def __init__(self, registry: ModuleRegistry, session: requests.Session | None = None) -> None:
        self.registry = registry
        self.session = session or self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        # 대시보드가 모듈마다 반복 호출하므로 커넥션 재사용이 지연에 크게 영향을 준다.
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        # 사내 서비스 호출이므로 프록시 환경변수를 타지 않게 한다.
        session.trust_env = False
        return session

    def close(self) -> None:
        self.session.close()

    def _auth_headers(self, module: ModuleDefinition, user: Mapping[str, Any] | None) -> tuple[dict[str, str], str]:
        auth = self.registry.auth
        secret = str(auth.get("shared_secret") or "")
        header_name = str(auth.get("header") or "X-Portal-Token")
        if not secret or not user:
            # 시크릿이 없으면 토큰 없이 호출한다. 초기 도입 단계에서 WAS 쪽 검증이
            # 준비되기 전에도 연동을 시험할 수 있어야 한다.
            return {}, ""
        token = issue_token(
            secret,
            user=user,
            module_id=module.id,
            ttl_seconds=int(auth.get("token_ttl_seconds") or 60),
        )
        return {header_name: token}, token

    def call(
        self,
        module_id: str,
        path: str,
        *,
        method: str = "GET",
        user: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        body: bytes | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        parse_json: bool = True,
    ) -> ModuleResponse:
        started = time.monotonic()
        verb = str(method or "GET").upper()
        if verb not in ALLOWED_METHODS:
            return ModuleResponse(module_id, STATUS_FAILED, error=f"허용되지 않는 메서드입니다: {verb}")
        try:
            module = self.registry.require(module_id)
            if module.is_local:
                raise ModuleConfigError(f"내부 모듈은 프록시 대상이 아닙니다: {module_id}")
            url = module.resolve(path)
        except ModuleConfigError as exc:
            return ModuleResponse(module_id, STATUS_FAILED, error=str(exc))

        headers, token = self._auth_headers(module, user)
        headers["Accept"] = "application/json"
        if extra_headers:
            headers.update({k: v for k, v in extra_headers.items() if k.lower() not in _BLOCKED_REQUEST_HEADERS})
        effective_timeout = float(timeout or module.timeout_seconds or self.registry.request_timeout)
        attempts = 1 + (self.registry.retry_count if verb == "GET" else 0)

        last_error: str | None = None
        last_status = STATUS_FAILED
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    verb,
                    url,
                    params=dict(params or {}),
                    json=json_body,
                    data=body,
                    headers=headers,
                    timeout=effective_timeout,
                    verify=self.registry.verify_tls,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout:
                last_status, last_error = STATUS_TIMEOUT, f"{effective_timeout:.1f}초 안에 응답하지 않았습니다."
            except requests.RequestException as exc:
                # 드라이버 예외 원문은 화면에 담기에 너무 길다. 상세는 로그에만 남긴다.
                LOGGER.warning("모듈 연결 실패 %s: %s", module.id, exc)
                last_status = STATUS_UNREACHABLE
                last_error = f"모듈에 연결할 수 없습니다: {module.base_url}"
            else:
                with response:
                    return self._read(module, response, started, parse_json=parse_json, token=token)
            LOGGER.warning(
                "모듈 호출 실패 %s %s (%d/%d) token=%s: %s",
                module.id, path, attempt + 1, attempts, mask_token(token), last_error,
            )
            # GET 만 재시도한다. 쓰기 요청을 재시도하면 중복 처리 위험이 있다.
        return ModuleResponse(
            module_id, last_status, error=last_error, elapsed_ms=int((time.monotonic() - started) * 1000)
        )

    def _read(
        self,
        module: ModuleDefinition,
        response: requests.Response,
        started: float,
        *,
        parse_json: bool,
        token: str,
    ) -> ModuleResponse:
        limit = self.registry.max_response_bytes
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(64 * 1024):
            size += len(chunk)
            if size > limit:
                return ModuleResponse(
                    module.id,
                    STATUS_FAILED,
                    http_status=response.status_code,
                    error=f"응답이 허용 크기({limit} bytes)를 넘었습니다.",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        content_type = str(response.headers.get("Content-Type") or "application/json")
        passthrough = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _BLOCKED_RESPONSE_HEADERS
        }

        data: Any = None
        if parse_json and "json" in content_type.lower() and raw:
            # 본문을 이미 스트리밍으로 읽었으므로 response.json() 은 쓸 수 없다.
            try:
                data = json.loads(raw.decode(response.encoding or "utf-8"))
            except (ValueError, LookupError, UnicodeDecodeError):
                return ModuleResponse(
                    module.id, STATUS_FAILED, http_status=response.status_code,
                    error="JSON 응답을 해석할 수 없습니다.", elapsed_ms=elapsed_ms,
                    content_type=content_type, raw=raw, headers=passthrough,
                )

        if response.status_code >= 400:
            detail = ""
            if isinstance(data, Mapping):
                detail = str(data.get("error") or data.get("message") or "")
            return ModuleResponse(
                module.id, STATUS_FAILED, http_status=response.status_code, data=data,
                error=detail or f"모듈이 HTTP {response.status_code} 를 반환했습니다.",
                elapsed_ms=elapsed_ms, content_type=content_type, raw=raw, headers=passthrough,
            )

        LOGGER.info("모듈 호출 성공 %s %s %dms token=%s", module.id, response.url, elapsed_ms, mask_token(token))
        return ModuleResponse(
            module.id, STATUS_SUCCESS, http_status=response.status_code, data=data,
            elapsed_ms=elapsed_ms, content_type=content_type, raw=raw, headers=passthrough,
        )

    def health(self, module_id: str, user: Mapping[str, Any] | None = None) -> ModuleResponse:
        module = self.registry.get(module_id)
        if module is None:
            return ModuleResponse(module_id, STATUS_FAILED, error="등록되지 않은 모듈입니다.")
        if not module.enabled:
            return ModuleResponse(module_id, STATUS_SKIPPED, error="사용 중지된 모듈입니다.")
        if module.is_local:
            return ModuleResponse(module_id, STATUS_SUCCESS, data={"status": "UP", "location": "LOCAL"})
        return self.call(module_id, module.health_path, user=user)


def filter_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """프록시할 때 그대로 넘겨도 되는 헤더만 남긴다."""
    return {key: value for key, value in headers.items() if key.lower() not in _BLOCKED_REQUEST_HEADERS}
