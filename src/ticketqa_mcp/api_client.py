import asyncio
from typing import Any

import httpx

from ._json import error_envelope

# Six of the seven endpoints live under .../api/qa/<endpoint>; the seventh
# (read-only ruleset) deliberately lives under .../api/criteria instead — it
# shares that path with the page's own Criteria tab, not the qa/* family. See
# qa-api-spec.md v1.0 §3.7: "路径前缀是 /api/criteria 而不是 /api/qa". Do not
# hardcode either prefix elsewhere — X-MSP-Host only carries the bare host.
_QA_PREFIX = "/apps/agent-ticket-qa/api/qa"
_CRITERIA_PREFIX = "/apps/agent-ticket-qa/api/criteria"

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
# connection-level failure (no response at all). Table matches qa-api-spec.md
# v1.0 §2 exactly: 401 unauthorized, 404 eval_not_found, 409 invalid_stage /
# evaluation_closed, 400 validation_failed, 413 payload_too_large, 500
# internal_error. The API's own `error.code` string (e.g. "invalid_stage") is
# carried separately on TicketQAError.code — this table only maps the HTTP
# status to this fleet's fixed envelope vocabulary.
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("invalid_argument", False),
    413: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def _classify(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    return "invalid_argument", False


class TicketQAError(Exception):
    def __init__(
        self, status_code: int, code: str | None, message: str, details: list | None = None
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"TicketQA API error {status_code} ({code}): {message}")

    def to_envelope(self) -> str:
        envelope_code, retryable = _classify(self.status_code)
        # self.code is the App's own domain error code (e.g. "invalid_stage",
        # "eval_not_found"), distinct from the SOP's fixed vocabulary in
        # envelope_code — surface both. `details` is the field-level error
        # list qa-api-spec.md v1.0 §1.4/§4 says must be passed through
        # verbatim ("skill 依赖它在同一回合内自纠重试" — swallowing it means
        # the model can't self-correct), so it's appended rather than dropped.
        message = f"[{self.code}] {self.message}" if self.code else self.message
        if self.details:
            message = f"{message} | details={self.details}"
        return error_envelope(envelope_code, message, retryable)


class TicketQAClient:
    """Async httpx client wrapping the agent-ticketqa App's QA data API
    (qa-api-spec.md v1.0).

    The platform's routing layer resolves which tenant/app instance a
    request belongs to via an `X_Tenant_ID` HTTP header. This is separate
    from — and not mentioned by — the App's own documented auth (v1.0 §1.2
    says plainly "no special auth: no MCP token, no write token, no
    signature header, just the same bearer JWT as the webpage"); it's this
    platform's own gateway-routing concern, confirmed empirically on the
    prior build of this server (requests with only the Authorization header
    got 404 {"error": "App not found"}; adding X_Tenant_ID fixed it) and
    carried forward here since it's the same App/platform, not re-verified
    against these specific new endpoints — see README Known Gaps.

    Reuses the module-level connection pool (see _get_http_client) across
    every call made through this instance, rather than opening a new
    connection per request.
    """

    def __init__(self, access_token: str, host: str, tenant_id: str):
        self._token = access_token
        self._tenant_id = tenant_id
        self._host = host.rstrip("/")

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

    async def qa_get(self, path: str, params: dict | None = None) -> Any:
        return await self._request(
            "GET", f"{self._host}{_QA_PREFIX}{path}", params=self._clean_params(params)
        )

    async def qa_post(self, path: str, json_body: Any = None) -> Any:
        return await self._request("POST", f"{self._host}{_QA_PREFIX}{path}", json_body=json_body)

    async def criteria_get(self) -> Any:
        return await self._request("GET", f"{self._host}{_CRITERIA_PREFIX}")

    async def _request(
        self, method: str, url: str, params: dict | None = None, json_body: Any = None
    ) -> Any:
        client = _get_http_client()
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

        # qa-api-spec.md v1.0 §1.4: success/failure is judged by the
        # `success` field, and HTTP status always agrees with it — but check
        # both, since a raw_response fallback (non-JSON body) has neither.
        if isinstance(body, dict) and "success" in body:
            if body.get("success") is False:
                err = body.get("error") or {}
                raise TicketQAError(
                    resp.status_code,
                    err.get("code"),
                    err.get("message") or "unknown error",
                    err.get("details"),
                )
            if resp.status_code >= 400:
                # success:true but a 4xx/5xx status would itself violate the
                # spec's own invariant — surface it rather than silently
                # trusting the body.
                raise TicketQAError(resp.status_code, None, "response body claims success but HTTP status is an error")
            return body.get("data")

        if resp.status_code >= 400:
            # Confirmed live (2026-09-04, dummy tenant against a real INT
            # host): a routing-layer failure — the request never reached the
            # App at all — comes back as this platform gateway's own flat
            # {"error": "<string>"} shape, not qa-api-spec.md's envelope
            # (that spec only describes what the App itself returns once
            # routing succeeds). Check both keys rather than only the
            # documented one, or a real "App not found" collapses to a
            # useless "unknown error".
            if isinstance(body, dict):
                message = body.get("message") or body.get("error")
            else:
                message = str(body)
            raise TicketQAError(resp.status_code, None, message or "unknown error")
        return body
