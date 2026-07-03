"""Read tool — read a file from the workdir."""
from __future__ import annotations

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _resolve_inside_workdir, READ_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "file path relative to workdir"},
    },
    "required": ["path"],
}


def create_read_capability() -> Capability:
    return Capability(
        name="read",
        description="Read a file from the workdir. Output capped at 64KB.",
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    path = args.get("path") or args.get("file_path") or args.get("filepath")
    if not isinstance(path, str) or not path:
        return ToolResult(tool="read", is_error=True, output="missing 'path'")
    try:
        resolved = _resolve_inside_workdir(path, state.workdir)
    except ValueError as exc:
        return ToolResult(tool="read", is_error=True, output=str(exc))
    if not resolved.exists():
        return ToolResult(tool="read", is_error=True, output=f"not found: {path}")
    if not resolved.is_file():
        return ToolResult(tool="read", is_error=True, output=f"not a regular file: {path}")
    raw = resolved.read_bytes()
    if len(raw) > READ_CAP_BYTES:
        text = raw[:READ_CAP_BYTES].decode("utf-8", errors="replace") + f"\n... [truncated at {READ_CAP_BYTES} bytes]"
    else:
        text = raw.decode("utf-8", errors="replace")
    return ToolResult(tool="read", is_error=False, output=text, detail={"bytes": len(raw)})
