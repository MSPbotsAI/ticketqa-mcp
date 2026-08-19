"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
TicketQAError, independent of any real HTTP request.
"""

import json

import pytest

from ticketqa_mcp.api_client import TicketQAError
from ticketqa_mcp.config import Settings
from ticketqa_mcp.server import create_mcp_server

# required params + annotation hints expected for each of the 6 tools.
# Any signature change here is a breaking contract change (SOP §13).
EXPECTED_TOOLS = {
    "ticketqa_start_run": ({"ticket_id"}, {}),
    "ticketqa_ingest_result": (
        {"eval_ref", "ticket", "rule_results"},
        {"idempotentHint": True},
    ),
    "ticketqa_report_error": (
        {"eval_ref", "failed_stage", "error_type", "error_message"},
        {},
    ),
    "ticketqa_validate_result": (
        {"eval_ref", "ticket", "rule_results"},
        {"readOnlyHint": True},
    ),
    "ticketqa_get_result": (set(), {"readOnlyHint": True}),
    "ticketqa_get_results": (set(), {"readOnlyHint": True}),
}


@pytest.mark.asyncio
async def test_tools_list_snapshot():
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOLS), f"unexpected tool set: {names}"
    assert len(tools) == 6

    by_name = {t.name: t for t in tools}
    for name, (expected_required, expected_annotations) in EXPECTED_TOOLS.items():
        tool = by_name[name]
        required = set(tool.inputSchema.get("required", []))
        assert required == expected_required, f"{name}: required={required}"

        ann = tool.annotations
        for hint, value in expected_annotations.items():
            actual = getattr(ann, hint, None) if ann is not None else None
            assert actual == value, f"{name}: {hint}={actual}, expected {value}"
        if not expected_annotations:
            # No semantic hint expected — but still confirm none of the
            # hints were set by accident.
            if ann is not None:
                assert not ann.readOnlyHint
                assert not ann.destructiveHint
                assert not ann.idempotentHint

        first_line = (tool.description or "").strip().splitlines()[0]
        assert len(first_line) <= 100, f"{name}: first line too long: {first_line!r}"
        assert len(tool.description or "") <= 500, f"{name}: description too long"


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
        (413, "invalid_argument", False),
        (422, "invalid_argument", False),
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
