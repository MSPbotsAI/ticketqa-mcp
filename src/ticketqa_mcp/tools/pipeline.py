"""The six eval_ref-scoped pipeline endpoints (qa-api-spec.md v1.0 §3.1-3.6).

One ticket evaluation = one eval_ref, orchestrated entirely by the App
through a fixed stage machine (assemble -> judging -> summary -> archived /
failed). The skill only ever acts within one App-initiated turn; it never
advances the stage itself. See server.py's instructions for the full
lifecycle description surfaced to the calling agent.
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import TicketQAClient, TicketQAError
from ._common import NO_TOKEN

_EVAL_REF_DESC = (
    "Required (uuid). The evaluation this call belongs to — copy it verbatim "
    "from the turn message's [qa_ref] marker. Never invent or guess one."
)


def _ticket_pass_mismatch(
    ticket_oml_level: int, ticket_pass: bool, pass_threshold: int
) -> str | None:
    """qa-api-spec.md v1.0 §3.4: the App enforces ticket_pass ==
    (ticket_oml_level >= pass_threshold) as a hard invariant and rejects the
    whole call otherwise. Checking it here catches an obviously-inconsistent
    verdict before spending a round trip on a call the App will refuse.
    """
    expected = ticket_oml_level >= pass_threshold
    if ticket_pass != expected:
        return (
            f"ticket_pass={ticket_pass} is inconsistent with ticket_oml_level="
            f"{ticket_oml_level} and pass_threshold={pass_threshold} (expected "
            f"ticket_pass={expected}, since ticket_pass must equal "
            "ticket_oml_level >= pass_threshold). Fix the value rather than "
            "forwarding a contradictory verdict."
        )
    return None


def register(mcp: FastMCP, client_factory: Callable[[], TicketQAClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def qa_store_ticket_data(
        eval_ref: Annotated[str, Field(description=_EVAL_REF_DESC)],
        ticket_id: Annotated[
            str,
            Field(
                description=(
                    "Required. Must equal the ticket number this eval_ref was "
                    "opened for — a mismatch is rejected."
                )
            ),
        ],
        ticket_data: Annotated[
            dict[str, object],
            Field(
                description=(
                    "Required. Opaque normalized ticket snapshot — the App "
                    "stores and displays it as-is without validating its "
                    "internal shape. For full display/reporting fidelity "
                    "include: summary, description, status, priority, board, "
                    "company/contact, created_time/updated_time, notes[] "
                    "(with internal/external flag and timestamp), "
                    "time_entries[], status_trail[], and owner: {id, name} "
                    "(id = the PSA's stable technician id — required for "
                    "technician-ranking stats to count this ticket; name is "
                    "display-only). Counts toward the 2MB request-body limit."
                )
            ),
        ],
        ticket_content_hash: Annotated[
            str | None, Field(description="Optional change-detection hash of the ticket content.")
        ] = None,
        capture_updated_time: Annotated[
            str | None,
            Field(description="Optional ISO 8601 UTC — the ticket's last-updated time in the PSA."),
        ] = None,
        psa: Annotated[
            str | None,
            Field(
                description=(
                    "Optional, informational only — the App does not read this "
                    "for routing (PSA is fixed per instance and tracked "
                    "separately from the turn message's [qa_psa] marker)."
                )
            ),
        ] = None,
    ) -> str:
        """Store the assembled ticket snapshot for one evaluation (assemble stage).

        Only accepted while the evaluation is in the "assemble" stage.
        Idempotent: re-posting the same eval_ref overwrites it (safe for
        turn retries); a late resubmit after the stage has already advanced
        returns success with duplicate=true and changes nothing.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"eval_ref": eval_ref, "ticket_id": ticket_id, "ticket_data": ticket_data}
        if ticket_content_hash is not None:
            body["ticket_content_hash"] = ticket_content_hash
        if capture_updated_time is not None:
            body["capture_updated_time"] = capture_updated_time
        if psa is not None:
            body["psa"] = psa
        try:
            result = await client.qa_post("/ticket-data", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def qa_get_ticket_data(
        eval_ref: Annotated[str, Field(description=_EVAL_REF_DESC)],
    ) -> str:
        """Re-read the stored ticket snapshot for one evaluation.

        Use this to fetch the authoritative snapshot when judging or
        summarizing rather than relying on conversation memory of it.
        Readable any time after qa_store_ticket_data has been called for
        this eval_ref, including after the evaluation is archived. Returns
        an eval_not_found error if nothing was ever stored for it.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.qa_get("/ticket-data", params={"eval_ref": eval_ref})
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def qa_store_domain_results(
        eval_ref: Annotated[str, Field(description=_EVAL_REF_DESC)],
        domain: Annotated[
            str,
            Field(
                description=(
                    "Required domain code, matching exactly what the current "
                    "turn message dispatched (e.g. \"ticket-hygiene\")."
                )
            ),
        ],
        rule_results: Annotated[
            list[dict],
            Field(
                description=(
                    "Required. One entry per rule in this domain — the set of "
                    "rule_id values must exactly match the domain's dispatched "
                    "rule set (missing, extra, or unknown ids reject the whole "
                    "call). Each entry: rule_id (str, required, from the "
                    "dispatched set), score (\"pass\"|\"fail\", required), "
                    "confidence (int 0-100, optional), findings (str "
                    "<=16000 chars, required — the verdict plus the evidence "
                    "it's based on plus what was missing), corrective_action "
                    "(str <=2048, optional — a one-line fix, worth including "
                    "on a fail). Do NOT include oml_level/alpha/base_severity "
                    "— the App fills those in itself from the frozen rule "
                    "snapshot it dispatched, to avoid the two sides drifting."
                )
            ),
        ],
    ) -> str:
        """Store one domain's rule-by-rule judging results (judging stage, once per domain).

        Only accepted for the domain currently dispatched by the App —
        except a domain already stored for this eval_ref, which can be
        resubmitted at any time as a full replacement (e.g. after fixing a
        validation error, or an App-initiated re-judge), reported as
        replayed=true and without advancing the stage again.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body = {"eval_ref": eval_ref, "domain": domain, "rule_results": rule_results}
        try:
            result = await client.qa_post("/domain-results", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool()
    async def qa_store_summary(
        eval_ref: Annotated[str, Field(description=_EVAL_REF_DESC)],
        ticket_oml_level: Annotated[
            int, Field(description="Required, 1-5 — the compute_oml CLI's output level.")
        ],
        ticket_pass: Annotated[
            bool,
            Field(
                description=(
                    "Required. Must equal (ticket_oml_level >= pass_threshold) "
                    "— checked locally before this call is even sent, and "
                    "enforced again by the App."
                )
            ),
        ],
        pass_threshold: Annotated[
            int,
            Field(
                description=(
                    "Required — must equal the threshold value the App sent in "
                    "the turn message, not a value you choose."
                )
            ),
        ],
        oml_explain: Annotated[
            dict[str, object],
            Field(
                description=(
                    "Required object explaining the level derivation (e.g. "
                    "reason, configured_levels, failed_levels, "
                    "threshold_source) — stored as-is, not validated."
                )
            ),
        ],
        coaching_suggestion: Annotated[
            str, Field(description="Required, <=16000 chars — coaching guidance for the technician.")
        ],
        skill_version: Annotated[
            str | None, Field(description="Optional skill/rubric version identifier, for provenance.")
        ] = None,
        judge_model: Annotated[
            str | None, Field(description="Optional judge-model identifier, for provenance.")
        ] = None,
    ) -> str:
        """Store the overall rating and coaching, and archive the evaluation (summary stage).

        Only accepted once every domain's results are already stored for
        this eval_ref. Archiving happens in the same call, atomically —
        there is no separate archive step. A late resubmit after archiving
        (e.g. a lost response on the original call) returns success with
        duplicate=true and does not create a new version. After this call
        succeeds, the evaluation is closed: wait for the App's own follow-up
        turn before reporting any writeback action.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        mismatch = _ticket_pass_mismatch(ticket_oml_level, ticket_pass, pass_threshold)
        if mismatch is not None:
            return error_envelope("invalid_argument", mismatch, False)
        body = {
            "eval_ref": eval_ref,
            "ticket_oml_level": ticket_oml_level,
            "ticket_pass": ticket_pass,
            "pass_threshold": pass_threshold,
            "oml_explain": oml_explain,
            "coaching_suggestion": coaching_suggestion,
        }
        if skill_version is not None:
            body["skill_version"] = skill_version
        if judge_model is not None:
            body["judge_model"] = judge_model
        try:
            result = await client.qa_post("/summary", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool()
    async def qa_report_writeback(
        eval_ref: Annotated[str, Field(description=_EVAL_REF_DESC)],
        action_type: Annotated[
            str,
            Field(description='Required — one of "write_note", "update_field", "send_alert".'),
        ],
        status: Annotated[
            str, Field(description='Required — one of "success", "failed", "skipped".')
        ],
        action_ref: Annotated[
            str | None,
            Field(
                description=(
                    "Optional idempotency key for this specific action. "
                    "Providing it makes a retry with the same (eval_ref, "
                    "action_ref) overwrite instead of appending a duplicate "
                    "report — use it whenever the same action might be "
                    "reported more than once (e.g. a network retry)."
                )
            ),
        ] = None,
        target: Annotated[
            str | None,
            Field(description="Optional — what the action targeted (ticket number, field name, recipient)."),
        ] = None,
        detail: Annotated[
            str | None,
            Field(description="Optional, <=4000 chars — a summary of what was actually done."),
        ] = None,
        error: Annotated[
            str | None, Field(description="Optional, <=4000 chars — required in spirit when status is failed.")
        ] = None,
        executed_at: Annotated[
            str | None, Field(description="Optional ISO 8601 UTC — when the action actually ran.")
        ] = None,
    ) -> str:
        """Report the outcome of one write-back action taken after an evaluation archived.

        Only accepted once the evaluation is archived. This is the App's
        only visibility into what happened after handoff — report every
        external action taken, and report one with status="skipped" even
        when you deliberately chose not to act, so the write-back step
        shows as closed rather than stuck waiting.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"eval_ref": eval_ref, "action_type": action_type, "status": status}
        if action_ref is not None:
            body["action_ref"] = action_ref
        if target is not None:
            body["target"] = target
        if detail is not None:
            body["detail"] = detail
        if error is not None:
            body["error"] = error
        if executed_at is not None:
            body["executed_at"] = executed_at
        try:
            result = await client.qa_post("/writeback-report", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()

    @mcp.tool()
    async def qa_report_turn_error(
        eval_ref: Annotated[str, Field(description=_EVAL_REF_DESC)],
        failed_stage: Annotated[
            str,
            Field(
                description=(
                    'Required — the stage that failed: "assemble", '
                    '"judging:<domain>" (e.g. "judging:ticket-hygiene"), or '
                    '"summary". Must match the stage this turn was actually '
                    "in."
                )
            ),
        ],
        error_type: Annotated[
            str,
            Field(
                description=(
                    "Required machine-readable category, e.g. "
                    '"ticket_not_found", "data_source_unreachable", '
                    '"empty_ticket_data", "cli_failed".'
                )
            ),
        ],
        error_message: Annotated[
            str, Field(description="Required, <=4000 chars — a human-readable description.")
        ],
        detail: Annotated[
            str | None, Field(description="Optional, <=4000 chars — extra context (e.g. a truncated raw error).")
        ] = None,
    ) -> str:
        """Report an unrecoverable in-turn failure (assemble/judging/summary stages only).

        Use this instead of silently ending the turn when something makes
        the current stage impossible to complete (data source unreachable,
        empty ticket, a CLI call failing) — without it the App's watchdog
        only sees a timeout, with no root cause. End the turn right after
        calling this; do not call any other pipeline tool afterward. Not
        for a failed write-back action after archiving — report that
        through qa_report_writeback's status="failed" instead. Rejected
        with evaluation_closed if the evaluation is already archived or
        failed.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {
            "eval_ref": eval_ref,
            "failed_stage": failed_stage,
            "error_type": error_type,
            "error_message": error_message,
        }
        if detail is not None:
            body["detail"] = detail
        try:
            result = await client.qa_post("/turn-error", json_body=body)
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()
