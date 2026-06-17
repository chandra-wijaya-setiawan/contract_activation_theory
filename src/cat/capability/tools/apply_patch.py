"""apply_patch tool — apply a unified-diff patch via git apply (docker-only)."""
from __future__ import annotations

import base64
import shlex
import uuid
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "patch":   {"type": "string", "description": "unified-diff text (the contents of a .patch file)"},
        "workdir": {"type": "string", "description": "directory to apply in (default /testbed)"},
    },
    "required": ["patch"],
}


def create_apply_patch_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the apply_patch capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        patch = args.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return ToolResult(tool="apply_patch", is_error=True, output="missing 'patch' (unified diff)")
        workdir = str(args.get("workdir") or default_workdir)
        b64 = base64.b64encode(patch.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/cat_patch_{uuid.uuid4().hex[:8]}.diff"
        sh = (
            f"echo {b64} | base64 -d > {tmp} && "
            f"cd {shlex.quote(workdir)} && "
            f"git apply --reject --whitespace=fix {tmp}; rc=$?; "
            f"echo '---REJECTS---'; "
            f"find . -name '*.rej' -newer {tmp} 2>/dev/null | head -20; "
            f"rm -f {tmp}; exit $rc"
        )
        exit_code, output = exec_fn(["bash", "-lc", sh])
        return ToolResult(
            tool="apply_patch",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "workdir": workdir},
        )

    return Capability(
        name="apply_patch",
        description=(
            "Apply a unified-diff patch in the workdir via `git apply --reject --whitespace=fix`. "
            "Use this for multi-hunk edits instead of crafting a heredoc + bash. "
            "Hunks that don't match are written to .rej files (partial apply); "
            "the tool result lists any .rej files so you can fix and retry."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
