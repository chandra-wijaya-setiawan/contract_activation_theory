"""git_status tool — show repo status (docker-only)."""
from __future__ import annotations

import shlex
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "workdir": {"type": "string", "description": "directory (default /testbed)"},
    },
}


def create_git_status_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the git_status capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        workdir = str(args.get("workdir") or default_workdir)
        sh = f"cd {shlex.quote(workdir)} && git status -sb"
        exit_code, output = exec_fn(["bash", "-lc", sh])
        return ToolResult(
            tool="git_status",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "workdir": workdir},
        )

    return Capability(
        name="git_status",
        description=(
            f"Show repo status (git status -sb) for the workdir. "
            f"Use between iterations to see which files you've touched."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
