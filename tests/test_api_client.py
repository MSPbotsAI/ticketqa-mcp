"""Unit tests for TicketQAClient._handle — the response-envelope parsing
this fleet's other clients don't need, since qa-api-spec.md v1.0 wraps
every response in {success, data} / {success: false, error: {...}}
instead of using bare HTTP status alone. No network calls: httpx.Response
is constructed directly in-memory.
"""

import httpx
import pytest

from ticketqa_mcp.api_client import TicketQAClient, TicketQAError


def _client():
    return TicketQAClient("tok", "https://example.mspbots.ai", "tenant-1")


def _response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=json_body, request=httpx.Request("GET", "https://x/"))


def test_handle_unwraps_success_envelope():
    resp = _response(200, {"success": True, "data": {"eval_ref": "e1", "stored_bytes": 10}})
    result = _client()._handle(resp)
    assert result == {"eval_ref": "e1", "stored_bytes": 10}


def test_handle_raises_on_success_false_with_details():
    resp = _response(
        400,
        {
            "success": False,
            "error": {
                "code": "validation_failed",
                "message": "2 field error(s)",
                "details": [{"path": "rule_results", "code": "missing_rule_ids"}],
            },
        },
    )
    with pytest.raises(TicketQAError) as exc_info:
        _client()._handle(resp)
    err = exc_info.value
    assert err.code == "validation_failed"
    assert err.status_code == 400
    assert err.details == [{"path": "rule_results", "code": "missing_rule_ids"}]


def test_handle_raises_on_success_true_but_error_status():
    # A response body claiming success:true is never expected to arrive
    # alongside a 4xx/5xx per the spec's own invariant — must not be
    # silently trusted just because success:true is present.
    resp = _response(500, {"success": True, "data": {}})
    with pytest.raises(TicketQAError) as exc_info:
        _client()._handle(resp)
    assert exc_info.value.status_code == 500


def test_handle_falls_back_to_flat_body_when_no_success_field():
    # Defensive path for a non-conforming response (e.g. a gateway error
    # page) that has no `success` key at all.
    resp = _response(404, {"message": "not found"})
    with pytest.raises(TicketQAError) as exc_info:
        _client()._handle(resp)
    assert exc_info.value.status_code == 404
    assert exc_info.value.message == "not found"


def test_handle_extracts_platform_routing_error_shape():
    # Confirmed live 2026-09-04: a routing-layer failure (request never
    # reached the App) comes back as this platform's own flat
    # {"error": "<string>"} — not the App's documented envelope, and not
    # keyed "message" either. Must not collapse to "unknown error".
    resp = _response(404, {"error": "App not found"})
    with pytest.raises(TicketQAError) as exc_info:
        _client()._handle(resp)
    assert exc_info.value.message == "App not found"


def test_handle_passes_through_2xx_without_success_field():
    resp = _response(200, {"raw_response": "ok"})
    result = _client()._handle(resp)
    assert result == {"raw_response": "ok"}
