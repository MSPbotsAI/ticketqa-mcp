from typing import Any

import httpx

# The App API is only reachable at this path prefix (see api-qa-ingest.md §4):
# "https://<host>/apps/agent-ticket-qa/api/qa/<endpoint>". Do not hardcode this
# prefix elsewhere — X-MSP-Host only carries the bare host.
_API_PREFIX = "/apps/agent-ticket-qa/api/qa"


class TicketQAError(Exception):
    def __init__(self, status_code: int, code: str | None, message: str):
        self.status_code = status_code
        self.code = code
        super().__init__(f"TicketQA API error {status_code} ({code}): {message}")


class TicketQAClient:
    """Async httpx client wrapping the MSPbots TicketQA Data Store API.

    The platform's routing layer resolves which tenant/app a request belongs
    to via an `X_Tenant_ID` cookie — this is undocumented in the source spec
    (api-qa-ingest.md only mentions the Authorization bearer header) and was
    confirmed empirically: requests with only the Authorization header get
    404 {"error": "App not found"}; adding the X_Tenant_ID cookie makes them
    succeed. A `Host` cookie was also observed in a real browser request but
    tested unnecessary — omitted here.
    """

    def __init__(self, access_token: str, host: str, tenant_id: str):
        self._token = access_token
        self._tenant_id = tenant_id
        self._base_url = host.rstrip("/") + _API_PREFIX

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _cookies(self) -> dict[str, str]:
        return {"X_Tenant_ID": self._tenant_id}

    def _clean_params(self, params: dict | None) -> dict:
        if not params:
            return {}
        return {k: v for k, v in params.items() if v is not None}

    async def get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.get(
                    f"{self._base_url}{path}",
                    headers=self._headers(),
                    cookies=self._cookies(),
                    params=self._clean_params(params),
                )
            except httpx.RequestError as e:
                raise TicketQAError(0, None, f"{e or type(e).__name__} (url={self._base_url}{path})") from e
            return self._handle(resp)

    async def post(self, path: str, json_body: Any) -> Any:
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}{path}",
                    headers=self._headers(),
                    cookies=self._cookies(),
                    json=json_body,
                )
            except httpx.RequestError as e:
                raise TicketQAError(0, None, f"{e or type(e).__name__} (url={self._base_url}{path})") from e
            return self._handle(resp)

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
