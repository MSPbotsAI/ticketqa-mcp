"""The one endpoint unrelated to any eval_ref (qa-api-spec.md v1.0 §3.7).

Lives at a different path prefix (/api/criteria, not /api/qa/...) because it
shares its backing endpoint with the product's own Criteria settings tab —
see api_client.py's _CRITERIA_PREFIX.
"""

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .._json import dump_json_capped
from ..api_client import TicketQAClient, TicketQAError
from ._common import NO_TOKEN


def register(mcp: FastMCP, client_factory: Callable[[], TicketQAClient | None]) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def qa_get_ruleset() -> str:
        """Get the live QA rule set (domains and rules) currently in effect.

        For conversational/preview scoring only — a real evaluation never
        calls this, since the App dispatches each domain's frozen rule
        snapshot in its own turn messages. Never cache: a rule can be
        edited on the settings page at any moment.

        Skip any domain or rule with enabled=false. Judge only against a
        rule's effective_instruction — never the raw instruction/
        descriptions fields, which exist only for the settings page.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.criteria_get()
            return dump_json_capped(result)
        except TicketQAError as e:
            return e.to_envelope()
