"""give_up tool — Performer inner-loop exit signal: cannot complete step."""
from __future__ import annotations

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State

# Marker string detected by the perform loop
PERFORMER_GIVE_UP_MARKER = "[PERFORMER_GIVE_UP]"

_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "The specific blocker preventing completion.",
        },
    },
    "required": ["reason"],
}


def create_give_up_capability() -> Capability:
    return Capability(
        name="give_up",
        description=(
            "Signal that you CANNOT complete this step after trying. Call this "
            "only when you have hit a fundamental blocker — the file doesn't "
            "exist, the task is impossible as stated, or the environment is "
            "broken. DO NOT call this just because one attempt failed — try "
            "at least 2 different approaches first. "
            "MANDATORY: provide a `reason` explaining the specific blocker."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    reason = str(args.get("reason", "") or "").strip()
    return ToolResult(tool="give_up", is_error=False,
                      output=f"{PERFORMER_GIVE_UP_MARKER} {reason}",
                      detail={"reason": reason})
