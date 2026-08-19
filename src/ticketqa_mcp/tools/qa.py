from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import TicketQAClient, TicketQAError
from ._common import NO_TOKEN, SCHEMA_VERSION

# No documented hard ceiling on page_size was found in the source spec
# (api-qa-ingest.md only fully documents POST /ingest, not GET /results) —
# apply the SOP's conservative default ceiling since no stricter real limit
# is known.
_MAX_PAGE_SIZE = 200

_TRIGGER_DESC = (
    'Optional object — trigger_source ("manual"/"scheduled"/"onboarding", '
    'default "manual"), filter_id, triggered_by, rubric_version, judge_model, '
    'psa ("connectwise"/"autotask"/"default").'
)


def register(mcp: FastMCP, client_factory: Callable[[], TicketQAClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def ticketqa_ingest_result(
        eval_ref: Annotated[
            str,
            Field(
                description="Required non-empty idempotency key for this evaluation, obtained from ticketqa_start_run."
            ),
        ],
        ticket: Annotated[
            dict[str, object],
            Field(
                description=(
                    "Required object — ticket_id (str), ticket_oml_level (int "
                    "1-5), ticket_pass (bool, must equal ticket_oml_level >= "
                    "pass_threshold), pass_threshold (int 2-5), evaluated_at "
                    "(ISO datetime string). Optional: ticket_data, "
                    "oml_explain, coaching_suggestion, ticket_content_hash, "
                    "capture_updated_time."
                )
            ),
        ],
        rule_results: Annotated[
            list[dict],
            Field(
                description=(
                    "Required list of 1-500 objects, each: rule_id (str, "
                    "unique within the list), domain (str), score "
                    '("pass"/"fail"), confidence (int 0-100), oml_level (int '
                    "1-5), findings (str, <=16000 chars). Optional: "
                    'base_severity ("critical"/"major"/"minor"), base_weight '
                    "(int), corrective_action, rule_instruction_snapshot, "
                    "alpha (bool, default false)."
                )
            ),
        ],
        trigger: Annotated[dict[str, object] | None, Field(description=_TRIGGER_DESC)] = None,
    ) -> str:
        """Write a completed QA evaluation for one ticket to the App data store.

        Idempotent on eval_ref: re-posting the same eval_ref replaces that
        evaluation's rule_results in place; a new eval_ref for the same
        ticket_id inserts a new evaluation_version. Atomic and all-or-nothing
        — any validation error rejects the whole envelope and writes nothing.
        Consider calling ticketqa_validate_result first to self-check.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body = {
            "schema_version": SCHEMA_VERSION,
            "eval_ref": eval_ref,
            "ticket": ticket,
            "rule_results": rule_results,
        }
        if trigger is not None:
            body["trigger"] = trigger
        try:
            result = await client.post("/ingest", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ticketqa_validate_result(
        eval_ref: Annotated[
            str,
            Field(
                description="Required non-empty idempotency key for this evaluation, obtained from ticketqa_start_run."
            ),
        ],
        ticket: Annotated[
            dict[str, object],
            Field(description="Required object — same shape as ticketqa_ingest_result's ticket."),
        ],
        rule_results: Annotated[
            list[dict],
            Field(
                description="Required list of 1-500 objects — same shape as ticketqa_ingest_result's rule_results."
            ),
        ],
        trigger: Annotated[dict[str, object] | None, Field(description=_TRIGGER_DESC)] = None,
    ) -> str:
        """Dry-run validate a QA evaluation envelope without writing anything.

        Accepts the exact same envelope shape as ticketqa_ingest_result and
        returns the same success/validation-error response, but never writes
        to the database. Use this to self-check before calling
        ticketqa_ingest_result.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body = {
            "schema_version": SCHEMA_VERSION,
            "eval_ref": eval_ref,
            "ticket": ticket,
            "rule_results": rule_results,
        }
        if trigger is not None:
            body["trigger"] = trigger
        try:
            result = await client.post("/validate", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool()
    async def ticketqa_report_error(
        eval_ref: Annotated[
            str,
            Field(
                description="Required — the same eval_ref obtained from ticketqa_start_run for the run that failed."
            ),
        ],
        failed_stage: Annotated[
            str,
            Field(
                description='Required — which stage failed, e.g. "fetch", "judge", or "compute".'
            ),
        ],
        error_type: Annotated[
            str, Field(description="Required short error category/type string.")
        ],
        error_message: Annotated[
            str, Field(description="Required human-readable error message.")
        ],
        ticket_id: Annotated[
            str | None, Field(description="Optional ticket ID this failure applies to.")
        ] = None,
        trigger: Annotated[dict[str, object] | None, Field(description=_TRIGGER_DESC)] = None,
    ) -> str:
        """Report that a QA run failed, instead of ingesting a result.

        Use this when a run started via ticketqa_start_run could not be
        completed, so the failure is recorded instead of silently losing the
        eval_ref. Field set is not fully confirmed against a live call.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {
            "schema_version": SCHEMA_VERSION,
            "eval_ref": eval_ref,
            "failed_stage": failed_stage,
            "error_type": error_type,
            "error_message": error_message,
        }
        if ticket_id is not None:
            body["ticket_id"] = ticket_id
        if trigger is not None:
            body["trigger"] = trigger
        try:
            result = await client.post("/error", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool()
    async def ticketqa_start_run(
        ticket_id: Annotated[str, Field(description="Required — the ticket to evaluate.")],
        trigger: Annotated[dict[str, object] | None, Field(description=_TRIGGER_DESC)] = None,
    ) -> str:
        """Start a QA evaluation run for a ticket and obtain an eval_ref.

        Triggers a real evaluation (costs an LLM judge call) — this request
        shape is inferred from the spec and not yet confirmed against a live
        call. Use the returned eval_ref with ticketqa_validate_result /
        ticketqa_ingest_result / ticketqa_report_error for the same run.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"ticket_id": ticket_id}
        if trigger is not None:
            body["trigger"] = trigger
        try:
            result = await client.post("/run", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ticketqa_get_result(
        ticket_id: Annotated[
            str | None, Field(description="Optional ticket ID to look up (latest evaluation).")
        ] = None,
        eval_ref: Annotated[
            str | None, Field(description="Optional specific evaluation reference to look up.")
        ] = None,
    ) -> str:
        """Get a single QA result (with its rule_results) by ticket or eval_ref.

        Provide at least one of ticket_id or eval_ref.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        if not ticket_id and not eval_ref:
            return error_envelope(
                "invalid_argument", "Provide at least one of ticket_id or eval_ref", False
            )
        params = {"ticket_id": ticket_id, "eval_ref": eval_ref}
        try:
            result = await client.get("/result", params=params)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ticketqa_get_results(
        page: Annotated[
            int | None, Field(description="Optional page number (1-based).")
        ] = None,
        page_size: Annotated[
            int | None, Field(description=f"Optional page size (server clamps to max {_MAX_PAGE_SIZE}).")
        ] = None,
        status: Annotated[
            str | None,
            Field(description='Optional filter — confirmed working value: "fail".'),
        ] = None,
        sort: Annotated[
            str | None, Field(description='Optional sort order — confirmed working value: "recent".')
        ] = None,
        extra_params: Annotated[
            dict[str, object] | None,
            Field(description="Additional raw query params, for filters not modeled here."),
        ] = None,
    ) -> str:
        """List QA results, optionally filtered and paginated."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        if page_size is not None:
            page_size = min(page_size, _MAX_PAGE_SIZE)
        params = {
            "page": page,
            "page_size": page_size,
            "status": status,
            "sort": sort,
            **(extra_params or {}),
        }
        try:
            result = await client.get("/results", params=params)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()
