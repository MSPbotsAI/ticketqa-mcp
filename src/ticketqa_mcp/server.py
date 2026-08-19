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
# it, even with a valid bearer token. Not documented in the source spec
# (api-qa-ingest.md), which only mentions the Authorization header.
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
            "MSPbots TicketQA is the platform's automated PSA-ticket "
            "quality-assurance scoring feature (\"Agent Ticket QA\"). An AI QA "
            "agent evaluates a closed support ticket against a rules rubric "
            "(per-domain checks such as responsiveness, documentation, "
            "communication) and this server writes that evaluation back to "
            "the platform's qa_results/rule_results tables, or reads it back "
            "later for reporting and coaching.\n\n"
            "Core concepts: eval_ref (idempotency key linking one "
            "evaluation's run -> ingest/error -> result lookup), "
            "ticket_oml_level (1-5 Operational Maturity Level) plus "
            "pass_threshold and ticket_pass (the overall verdict), "
            "rule_results (per-rule pass/fail findings, 1-500 entries per "
            "evaluation).\n\n"
            "Typical flow: ticketqa_start_run(ticket_id) to kick off scoring "
            "and obtain an eval_ref; ticketqa_validate_result to dry-run "
            "check an envelope before writing anything; "
            "ticketqa_ingest_result to persist the finished evaluation "
            "(idempotent on eval_ref); ticketqa_report_error if scoring "
            "failed partway. Use ticketqa_get_result / ticketqa_get_results "
            "to read evaluations back. PSA sources: ConnectWise, Autotask."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    client_factory: Callable[[], TicketQAClient | None] = lambda: get_client_from_context(settings)

    from .tools import qa

    qa.register(mcp, client_factory)

    return mcp
