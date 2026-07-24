import json
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..api_client import TicketQAClient, TicketQAError
from ._common import NO_TOKEN, SCHEMA_VERSION


def register(mcp: FastMCP, client_factory: Callable[[], TicketQAClient | None]) -> None:

    @mcp.tool()
    async def ticketqa_ingest_result(
        eval_ref: str,
        ticket: dict[str, object],
        rule_results: list[dict],
        trigger: dict[str, object] | None = None,
    ) -> str:
        """Write a completed QA evaluation for one ticket to the App data store.

        API: POST /api/qa/ingest (schema_version 2.0). Idempotent on eval_ref:
        re-posting the same eval_ref replaces that evaluation's rule_results
        in place (evaluation_version unchanged); a new eval_ref for the same
        ticket_id inserts a new evaluation_version. Atomic and all-or-nothing —
        any validation error rejects the whole envelope and writes nothing.

        Args:
            eval_ref: Required non-empty idempotency key for this evaluation
                (obtained from ticketqa_start_run).
            ticket: Required object — ticket_id (str), ticket_oml_level (int
                1..5), ticket_pass (bool, must equal ticket_oml_level >=
                pass_threshold), pass_threshold (int 2..5), evaluated_at (ISO
                datetime string). Optional: ticket_data, oml_explain,
                coaching_suggestion, ticket_content_hash, capture_updated_time.
            rule_results: Required list of 1-500 objects, each: rule_id (str,
                unique within the list), domain (str), score ("pass"/"fail"),
                confidence (int 0..100), oml_level (int 1..5), findings (str,
                <=16000 chars). Optional: base_severity
                ("critical"/"major"/"minor"), base_weight (int),
                corrective_action, rule_instruction_snapshot, alpha (bool,
                default false).
            trigger: Optional object — trigger_source
                ("manual"/"scheduled"/"onboarding", default "manual"),
                filter_id, triggered_by, rubric_version, judge_model, psa
                ("connectwise"/"autotask"/"default").
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
            return json.dumps(result, indent=2)
        except TicketQAError as e:
            return f"Error: {e}"

    @mcp.tool()
    async def ticketqa_validate_result(
        eval_ref: str,
        ticket: dict[str, object],
        rule_results: list[dict],
        trigger: dict[str, object] | None = None,
    ) -> str:
        """Dry-run validate a QA evaluation envelope without writing anything.

        API: POST /api/qa/validate — accepts the exact same envelope shape as
        ticketqa_ingest_result and returns the same success/validation-error
        response, but never writes to the database. Use this to self-check
        before calling ticketqa_ingest_result.

        Args: same as ticketqa_ingest_result (eval_ref, ticket, rule_results,
            trigger).
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
            return json.dumps(result, indent=2)
        except TicketQAError as e:
            return f"Error: {e}"

    @mcp.tool()
    async def ticketqa_report_error(
        eval_ref: str,
        failed_stage: str,
        error_type: str,
        error_message: str,
        ticket_id: str | None = None,
        trigger: dict[str, object] | None = None,
    ) -> str:
        """Report that a QA run failed, instead of ingesting a result.

        ⚠️ INFERRED SCHEMA — the source spec (api-qa-ingest.md) only says this
        endpoint takes "the same envelope header fields, plus failed_stage /
        error_type / error_message"; the exact field set/casing has not been
        confirmed against the actual backend. Verify against a real call
        before relying on this in production.

        API: POST /api/qa/error

        Args:
            eval_ref: Required — the same eval_ref obtained from
                ticketqa_start_run for the run that failed.
            failed_stage: Required — which stage failed, e.g. "fetch",
                "judge", or "compute" (per the source doc's description of
                the run pipeline).
            error_type: Required short error category/type string.
            error_message: Required human-readable error message.
            ticket_id: Optional ticket ID this failure applies to.
            trigger: Optional trigger object, same shape as in
                ticketqa_ingest_result.
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
            return json.dumps(result, indent=2)
        except TicketQAError as e:
            return f"Error: {e}"

    @mcp.tool()
    async def ticketqa_start_run(
        ticket_id: str,
        trigger: dict[str, object] | None = None,
    ) -> str:
        """Start a QA evaluation run for a ticket and obtain an eval_ref.

        ⚠️ INFERRED SCHEMA — the source spec only documents this endpoint's
        response shape ({"eval_ref": ..., "status": "running"}), not its
        request body. This tool assumes ticket_id (required) plus the same
        optional trigger object used elsewhere. Verify against a real call
        before relying on this in production.

        API: POST /api/qa/run

        Args:
            ticket_id: Required — the ticket to evaluate.
            trigger: Optional object — trigger_source
                ("manual"/"scheduled"/"onboarding"), filter_id, triggered_by,
                rubric_version, judge_model, psa
                ("connectwise"/"autotask"/"default").
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"ticket_id": ticket_id}
        if trigger is not None:
            body["trigger"] = trigger
        try:
            result = await client.post("/run", json_body=body)
            return json.dumps(result, indent=2)
        except TicketQAError as e:
            return f"Error: {e}"

    @mcp.tool()
    async def ticketqa_get_result(
        ticket_id: str | None = None,
        eval_ref: str | None = None,
    ) -> str:
        """Get a single QA result (with its rule_results) by ticket or eval_ref.

        ⚠️ INFERRED SCHEMA — the source spec only names this endpoint
        (GET /api/qa/result) without documenting its query parameters. This
        tool assumes lookup by ticket_id and/or eval_ref, matching the fields
        used as lookup keys elsewhere in the spec. Verify against a real call
        before relying on this in production.

        API: GET /api/qa/result

        Args:
            ticket_id: Optional ticket ID to look up (latest evaluation).
            eval_ref: Optional specific evaluation reference to look up.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        if not ticket_id and not eval_ref:
            return "Error: provide at least one of ticket_id or eval_ref"
        params = {"ticket_id": ticket_id, "eval_ref": eval_ref}
        try:
            result = await client.get("/result", params=params)
            return json.dumps(result, indent=2)
        except TicketQAError as e:
            return f"Error: {e}"

    @mcp.tool()
    async def ticketqa_get_results(
        limit: int | None = None,
        offset: int | None = None,
        ticket_id: str | None = None,
        extra_params: dict[str, object] | None = None,
    ) -> str:
        """List QA results, optionally filtered/paginated.

        ⚠️ INFERRED SCHEMA — the source spec only names this endpoint
        (GET /api/qa/results) without documenting its query parameters. This
        tool assumes common pagination (limit/offset) plus a ticket_id
        filter. Verify against a real call before relying on this in
        production.

        API: GET /api/qa/results

        Args:
            limit: Optional max number of results to return.
            offset: Optional pagination offset.
            ticket_id: Optional filter by ticket ID.
            extra_params: Additional raw query params, in case the real API
                supports filters not modeled here (e.g. date range, oml_level,
                ticket_pass).
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        params = {
            "limit": limit,
            "offset": offset,
            "ticket_id": ticket_id,
            **(extra_params or {}),
        }
        try:
            result = await client.get("/results", params=params)
            return json.dumps(result, indent=2)
        except TicketQAError as e:
            return f"Error: {e}"
