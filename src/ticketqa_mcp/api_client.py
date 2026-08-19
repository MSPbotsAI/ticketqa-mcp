import asyncio
from typing import Any

import httpx

from ._json import error_envelope

# The App API is only reachable at this path prefix (see api-qa-ingest.md §4):
# "https://<host>/apps/agent-ticket-qa/api/qa/<endpoint>". Do not hardcode this
# prefix elsewhere — X-MSP-Host only carries the bare host.
_API_PREFIX = "/apps/agent-ticket-qa/api/qa"

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_MAX_BACKOFF_SECONDS = 20.0

# One shared connection pool for the process lifetime. No credentials are
# ever stored on it — token/host/tenant are passed per-request via the
# TicketQAClient instance built fresh from the per-request contextvar (see
# server.py's contextvar-based credential isolation, which is what actually
# keeps tenants apart).
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
    return _http_client


# status_code -> (error code, retryable). status_code 0 means a network/
# connection-level failure (no response at all).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    413: ("invalid_argument", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def _classify(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    return "invalid_argument", False


class TicketQAError(Exception):
    def __init__(self, status_code: int, code: str | None, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"TicketQA API error {status_code} ({code}): {message}")

    def to_envelope(self) -> str:
        envelope_code, retryable = _classify(self.status_code)
        # self.code is the TicketQA API's own domain error code (e.g.
        # "validation_failed", "duplicate_rule", "unsupported_schema_version"),
        # distinct from the SOP's fixed vocabulary in envelope_code — surface
        # both when available.
        message = f"[{self.code}] {self.message}" if self.code else self.message
        return error_envelope(envelope_code, message, retryable)


class TicketQAClient:
    """Async httpx client wrapping the MSPbots TicketQA Data Store API.

    The platform's routing layer resolves which tenant/app a request belongs
    to via an `X_Tenant_ID` HTTP header — this is undocumented in the source
    spec (api-qa-ingest.md only mentions the Authorization bearer header) and
    was confirmed empirically: requests with only the Authorization header
    get 404 {"error": "App not found"}; adding X_Tenant_ID makes them succeed.

    Reuses the module-level connection pool (see _get_http_client) across
    every call made through this instance, rather than opening a new
    connection per request.
    """

    def __init__(self, access_token: str, host: str, tenant_id: str):
        self._token = access_token
        self._tenant_id = tenant_id
        self._base_url = host.rstrip("/") + _API_PREFIX

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X_Tenant_ID": self._tenant_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _clean_params(self, params: dict | None) -> dict:
        if not params:
            return {}
        return {k: v for k, v in params.items() if v is not None}

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=self._clean_params(params))

    async def post(self, path: str, json_body: Any = None) -> Any:
        return await self._request("POST", path, json_body=json_body)

    async def _request(
        self, method: str, path: str, params: dict | None = None, json_body: Any = None
    ) -> Any:
        client = _get_http_client()
        url = f"{self._base_url}{path}"
        headers = self._headers()

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except httpx.RequestError as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
                    continue
                raise TicketQAError(0, None, f"{e or type(e).__name__} (url={url})") from e

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                delay = self._retry_delay(resp, attempt)
                await asyncio.sleep(delay)
                continue

            return self._handle(resp)

        # Unreachable in practice (loop always returns or raises above), but
        # keeps type checkers happy and guards against future edits.
        if last_exc:
            raise TicketQAError(0, None, f"{last_exc}") from last_exc
        raise TicketQAError(0, None, "request failed with no response")

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(2**attempt, _MAX_BACKOFF_SECONDS)

    def _handle(self, resp: httpx.Response) -> Any:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw_response": resp.text}
        if resp.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            message = body.get("message") if isinstance(body, dict) else str(body)
            errors = body.get("errors") if isinstance(body, dict) else None
            if errors:
                message = f"{message} | errors={errors}"
            raise TicketQAError(resp.status_code, code, message or "unknown error")
        return body
