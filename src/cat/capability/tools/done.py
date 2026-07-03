"""done tool — Performer inner-loop exit signal: task completed."""
from __future__ import annotations

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State

# Marker strings detected by the perform loop
PERFORMER_DONE_MARKER = "[PERFORMER_DONE]"

_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "Why you believe this step is complete. "
                           "Reference specific tool output as evidence.",
        },
    },
    "required": ["reason"],
}


def create_done_capability() -> Capability:
    return Capability(
        name="done",
        description=(
            "Signal that you have COMPLETED this step. Call this when you are "
            "confident the step's expected_result is satisfied. "
            "MANDATORY: provide a `reason` explaining WHY you believe the step "
            "is done — what specific evidence from your tool results confirms it."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    reason = str(args.get("reason", "") or "").strip()
    # Accept all done() calls — reason quality is validated by the Attestor,
    # not the handler. Rejecting empty reasons caused deepseek-v4-flash to
    # burn all 5 inner attempts calling done() repeatedly without adapting.
    return ToolResult(tool="done", is_error=False,
                      output=f"{PERFORMER_DONE_MARKER} {reason}",
                      detail={"reason": reason})
