"""Gateway credential middleware tests: missing-header 401, and header
values correctly reaching the per-request contextvar (no global-state
leakage across requests).

This server requires 3 headers — X-MSP-Token, X-MSP-Tenant-Id, and
X-MSP-Host — because the platform's routing layer 404s ("App not found")
without the tenant id forwarded downstream as an X_Tenant_ID header (see
api_client.py / server.py docstrings).
"""

from starlette.testclient import TestClient

from ticketqa_mcp.__main__ import _build_http_app
from ticketqa_mcp.config import Settings
from ticketqa_mcp.server import create_mcp_server, get_client_from_context


def _make_app():
    settings = Settings()
    mcp = create_mcp_server(settings)
    return _build_http_app(mcp, settings), settings


def test_health_is_local_and_does_not_require_credentials():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_missing_all_headers_returns_401_with_required_headers_listed():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["required_headers"] == ["X-MSP-Token", "X-MSP-Tenant-Id", "X-MSP-Host"]


def test_missing_one_of_three_headers_still_returns_401():
    # Only 2 of the 3 required headers present — confirms all three are
    # independently enforced, not just "at least one of them".
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "X-MSP-Token": "tok",
                "X-MSP-Host": "https://agentosint.mspbots.ai",
                # X-MSP-Tenant-Id intentionally omitted
            },
        )
        assert resp.status_code == 401
        body = resp.json()
        assert "X-MSP-Tenant-Id" in body["required_headers"]


def test_all_headers_present_reaches_request_context(monkeypatch):
    # Directly exercises the middleware's contextvar plumbing without a full
    # MCP protocol round-trip: confirms the header values that arrive on the
    # request are exactly what get_client_from_context sees, and that they
    # are reset afterward (no leakage to the next request).
    import asyncio

    from ticketqa_mcp.server import GatewayTokenMiddleware, _gateway_creds_var

    settings = Settings()
    seen = {}

    async def fake_app(scope, receive, send):
        seen["creds"] = _gateway_creds_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = GatewayTokenMiddleware(fake_app, settings)

    async def run():
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-msp-token", b"test-token-123"),
                (b"x-msp-host", b"https://agentosint.mspbots.ai"),
                (b"x-msp-tenant-id", b"tenant-abc"),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)

    asyncio.run(run())
    assert seen["creds"] == ("test-token-123", "https://agentosint.mspbots.ai", "tenant-abc")
    # After the request completes, the contextvar must be reset — a fresh
    # get() outside any request context sees no leftover credential.
    assert _gateway_creds_var.get() is None


def test_client_factory_returns_none_without_context():
    settings = Settings()
    assert get_client_from_context(settings) is None


def test_client_factory_builds_client_with_tenant_header(monkeypatch):
    # Confirms the tenant id flows into TicketQAClient in a way that ends up
    # as the X_Tenant_ID header (not silently dropped) — the hard-won
    # gateway requirement this server must never regress.
    from ticketqa_mcp.server import _gateway_creds_var

    settings = Settings()
    token = _gateway_creds_var.set(
        ("tok", "https://agentosint.mspbots.ai", "tenant-xyz")
    )
    try:
        client = get_client_from_context(settings)
        assert client is not None
        assert client._headers()["X_Tenant_ID"] == "tenant-xyz"
        assert client._headers()["Authorization"] == "Bearer tok"
    finally:
        _gateway_creds_var.reset(token)
