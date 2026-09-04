import contextvars
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import TicketQAClient
from .config import Settings

# Per-request credential isolation via contextvars.
# GatewayTokenMiddleware sets this before the MCP handler runs.
# Python asyncio copies context per task, so concurrent SSE connections are isolated.
# Value is (access_token, host, tenant_id). tenant_id IS forwarded to the
# downstream App API — as an X_Tenant_ID header. This was confirmed
# empirically: the platform's routing layer 404s ("App not found") without
# it, even with a valid bearer token. Not documented in the App's own spec
# (qa-api-spec.md v1.0 §1.2), which only mentions the Authorization header.
_gateway_creds_var: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "ticketqa_gateway_creds", default=None
)


def get_client_from_context(settings: Settings) -> TicketQAClient | None:
    """Resolve the active TicketQAClient for the current request context."""
    creds = _gateway_creds_var.get()
    if not creds:
        return None
    token, host, tenant_id = creds
    return TicketQAClient(token, host, tenant_id)


class GatewayTokenMiddleware:
    """ASGI middleware.

    Reads X-MSP-Token, X-MSP-Tenant-Id, and X-MSP-Host (all required) from
    request headers and stores them in the contextvar. Returns 401 if any is
    missing on /mcp requests.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = request.headers.get("x-msp-token")
        tenant_id = request.headers.get("x-msp-tenant-id")
        host = request.headers.get("x-msp-host")
        if not token or not tenant_id or not host:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        "This server requires the X-MSP-Token header (Agent Platform "
                        "bearer access credential), the X-MSP-Tenant-Id header, and "
                        "the X-MSP-Host header (TicketQA App API host)"
                    ),
                    "required_headers": ["X-MSP-Token", "X-MSP-Tenant-Id", "X-MSP-Host"],
                    "optional_headers": [],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        ctx_token = _gateway_creds_var.set((token, host, tenant_id))
        try:
            await self.app(scope, receive, send)
        finally:
            _gateway_creds_var.reset(ctx_token)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server instance and register all TicketQA tools."""
    # DNS-rebinding protection is a browser-oriented safeguard that rejects
    # non-localhost Host headers with 421. Disable it so the server works
    # correctly behind a reverse proxy or docker network.
    mcp = FastMCP(
        name="ticketqa-mcp",
        instructions=(
            "MSPbots Agent Ticket QA scores a closed PSA ticket against a "
            "rules rubric. The App orchestrates the whole evaluation as a "
            "stage machine (assemble -> judging, once per domain -> summary "
            "-> archived) and drives it turn by turn; this server is only "
            "ever called from inside one such turn — it never advances the "
            "stage itself, that always happens on the App's own next turn. "
            "eval_ref (from the turn message's [qa_ref] marker) links every "
            "call in one evaluation.\n\n"
            "Turn flow: qa_store_ticket_data once in the assemble turn; "
            "qa_store_domain_results once per domain in each judging turn "
            "(qa_get_ticket_data to re-read the snapshot as evidence); "
            "qa_store_summary once, after every domain is stored, which "
            "also archives the evaluation in the same call. If a turn hits "
            "an unrecoverable failure, call qa_report_turn_error and stop — "
            "never keep going or call another pipeline tool after it. Once "
            "archived, a later turn may ask for external actions (a PSA "
            "note, a field update, an alert) — report every one of them "
            "with qa_report_writeback, including a status=\"skipped\" "
            "report when you deliberately don't act.\n\n"
            "qa_get_ruleset is unrelated to all of the above — it's a "
            "live, uncached rule lookup for conversational/preview scoring "
            "outside a real App-driven evaluation only."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
        json_response=True,
    )

    client_factory: Callable[[], TicketQAClient | None] = lambda: get_client_from_context(settings)

    from .tools import pipeline, ruleset

    pipeline.register(mcp, client_factory)
    ruleset.register(mcp, client_factory)

    return mcp
