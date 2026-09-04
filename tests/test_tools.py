"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
TicketQAError, independent of any real HTTP request.
"""

import json

import pytest
from mcp.server.fastmcp import FastMCP

from ticketqa_mcp.api_client import TicketQAError
from ticketqa_mcp.config import Settings
from ticketqa_mcp.server import create_mcp_server

# required params + annotation hints expected for each of the 7 tools
# (qa-api-spec.md v1.0 §3.1-3.7 / §4). Any signature change here is a
# breaking contract change.
EXPECTED_TOOLS = {
    "qa_store_ticket_data": (
        {"eval_ref", "ticket_id", "ticket_data"},
        {"idempotentHint": True},
    ),
    "qa_get_ticket_data": ({"eval_ref"}, {"readOnlyHint": True}),
    "qa_store_domain_results": (
        {"eval_ref", "domain", "rule_results"},
        {"idempotentHint": True},
    ),
    "qa_store_summary": (
        {
            "eval_ref",
            "ticket_oml_level",
            "ticket_pass",
            "pass_threshold",
            "oml_explain",
            "coaching_suggestion",
        },
        {},
    ),
    "qa_report_writeback": ({"eval_ref", "action_type", "status"}, {}),
    "qa_report_turn_error": (
        {"eval_ref", "failed_stage", "error_type", "error_message"},
        {},
    ),
    "qa_get_ruleset": (set(), {"readOnlyHint": True}),
}


# qa_store_summary's docstring deliberately exceeds the SOP's 500-char
# guideline (a "should" not a hard rule): it's the atomic archive step —
# duplicate-replay semantics, and "wait for the App's own next turn"
# afterward — are load-bearing correctness/safety information, not filler.
_LONG_DESCRIPTION_EXCEPTIONS = {"qa_store_summary", "qa_report_turn_error"}


@pytest.mark.asyncio
async def test_tools_list_snapshot():
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOLS), f"unexpected tool set: {names}"
    assert len(tools) == 7

    by_name = {t.name: t for t in tools}
    for name, (expected_required, expected_annotations) in EXPECTED_TOOLS.items():
        tool = by_name[name]
        required = set(tool.inputSchema.get("required", []))
        assert required == expected_required, f"{name}: required={required}"

        ann = tool.annotations
        for hint, value in expected_annotations.items():
            actual = getattr(ann, hint, None) if ann is not None else None
            assert actual == value, f"{name}: {hint}={actual}, expected {value}"
        if not expected_annotations and ann is not None:
            # No semantic hint expected — but still confirm none of the
            # hints were set by accident.
            assert not ann.readOnlyHint
            assert not ann.destructiveHint
            assert not ann.idempotentHint

        description = tool.description or ""
        if name not in _LONG_DESCRIPTION_EXCEPTIONS:
            assert len(description) <= 500, f"{name}: description too long ({len(description)})"
        first_line = description.strip().splitlines()[0] if description.strip() else ""
        assert len(first_line) <= 100, f"{name}: first line too long: {first_line!r}"
        assert "POST /" not in description and "GET /" not in description, (
            f"{name}: leaked implementation detail"
        )


@pytest.mark.asyncio
async def test_service_instructions_present_and_bounded():
    mcp = create_mcp_server(Settings())
    assert mcp.instructions
    assert len(mcp.instructions) <= 1500


@pytest.mark.parametrize(
    "status_code,expected_code,expected_retryable",
    [
        (0, "upstream_error", True),
        (400, "invalid_argument", False),
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (409, "invalid_argument", False),
        (413, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (503, "upstream_error", True),
    ],
)
def test_error_envelope_mapping(status_code, expected_code, expected_retryable):
    err = TicketQAError(status_code, "some_domain_code", "boom")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retryable"] is expected_retryable
    assert "boom" in envelope["error"]["message"]


def test_error_envelope_without_domain_code():
    err = TicketQAError(404, None, "not there")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == "not_found"
    assert envelope["error"]["message"] == "not there"


def test_error_envelope_passes_through_validation_details():
    # qa-api-spec.md v1.0 §1.4/§4: `error.details` must reach the model
    # verbatim — the skill depends on it to self-correct in the same turn.
    details = [{"path": "rule_results[1].score", "code": "invalid_enum", "expected": "pass|fail", "got": "passed"}]
    err = TicketQAError(400, "validation_failed", "2 field error(s)", details)
    envelope = json.loads(err.to_envelope())
    assert "invalid_enum" in envelope["error"]["message"]
    assert "rule_results[1].score" in envelope["error"]["message"]


@pytest.mark.asyncio
async def test_store_summary_rejects_ticket_pass_mismatch_before_calling_api():
    captured = {}

    class _StubClient:
        async def qa_post(self, path, json_body=None):
            captured["called"] = path
            return {"should": "not happen"}

    from ticketqa_mcp.tools import pipeline

    mcp = FastMCP(name="test")
    pipeline.register(mcp, lambda: _StubClient())
    result = await mcp.call_tool(
        "qa_store_summary",
        {
            "eval_ref": "e1",
            "ticket_oml_level": 4,
            "ticket_pass": False,  # inconsistent: 4 >= pass_threshold(2) should be True
            "pass_threshold": 2,
            "oml_explain": {},
            "coaching_suggestion": "x",
        },
    )
    text = result[0][0].text if isinstance(result, tuple) else str(result)
    assert "invalid_argument" in text
    assert "called" not in captured, "must reject before ever calling the API"


@pytest.mark.asyncio
async def test_no_credentials_returns_not_configured_without_calling_api():
    from ticketqa_mcp.tools import ruleset

    mcp = FastMCP(name="test")
    ruleset.register(mcp, lambda: None)
    result = await mcp.call_tool("qa_get_ruleset", {})
    text = result[0][0].text if isinstance(result, tuple) else str(result)
    assert "not_configured" in text
