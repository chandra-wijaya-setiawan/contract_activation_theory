"""Bash tool — run a shell command in the workdir."""
from __future__ import annotations

import subprocess

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "shell command to execute"},
        "timeout_s": {"type": "number", "description": "kill the command after this many seconds (default 60)"},
    },
    "required": ["command"],
}


def create_bash_capability() -> Capability:
    return Capability(
        name="bash",
        description="Run a shell command in the workdir. Returns stdout+stderr, truncated. Returns is_error=true on non-zero exit.",
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    cmd = str(args.get("command", "")).strip()
    timeout_s = float(args.get("timeout_s", 60))
    if not cmd:
        return ToolResult(tool="bash", is_error=True, output="empty command")
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(state.workdir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(tool="bash", is_error=True, output=f"TIMEOUT after {timeout_s}s", detail={"command": cmd})
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return ToolResult(
        tool="bash",
        is_error=(proc.returncode != 0),
        output=_truncate(out, OUTPUT_CAP_BYTES),
        detail={"returncode": proc.returncode, "command": cmd},
    )
