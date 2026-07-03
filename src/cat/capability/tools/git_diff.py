"""git_diff tool — show accumulated diff (docker-only)."""
from __future__ import annotations

import shlex
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, READ_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "workdir": {"type": "string", "description": "directory (default /testbed)"},
        "path":    {"type": "string", "description": "limit diff to a path (optional)"},
        "staged":  {"type": "boolean", "description": "use --staged (default false)"},
    },
}


def create_git_diff_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the git_diff capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        workdir = str(args.get("workdir") or default_workdir)
        path = args.get("path")
        staged = bool(args.get("staged", False))
        parts = ["git diff"]
        if staged:
            parts.append("--staged")
        if isinstance(path, str) and path:
            parts.append("--")
            parts.append(shlex.quote(path))
        sh = f"cd {shlex.quote(workdir)} && {' '.join(parts)}"
        exit_code, output = exec_fn(["bash", "-lc", sh])
        return ToolResult(
            tool="git_diff",
            is_error=(exit_code != 0),
            output=_truncate(output, READ_CAP_BYTES),  # diffs can be large
            detail={"returncode": exit_code, "workdir": workdir, "path": path, "staged": staged},
        )

    return Capability(
        name="git_diff",
        description=(
            f"Show accumulated diff (git diff [--staged] [path]) for the workdir. "
            f"Use to inspect your own edits as a single coherent change. "
            f"Output capped at 64KB."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
