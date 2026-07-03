"""Write tool — write a file in the workdir."""
from __future__ import annotations

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _resolve_inside_workdir

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "file path relative to workdir"},
        "content": {"type": "string", "description": "full file contents"},
    },
    "required": ["path", "content"],
}


def create_write_capability() -> Capability:
    return Capability(
        name="write",
        description="Write a file in the workdir. Creates parent directories. Overwrites existing files.",
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    path = args.get("path")
    content = args.get("content", "")
    if not isinstance(path, str) or not path:
        return ToolResult(tool="write", is_error=True, output="missing 'path'")
    try:
        resolved = _resolve_inside_workdir(path, state.workdir)
    except ValueError as exc:
        return ToolResult(tool="write", is_error=True, output=str(exc))
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes avoids platform newline translation; agents get exact bytes back.
    data = content.encode("utf-8")
    resolved.write_bytes(data)
    return ToolResult(
        tool="write",
        is_error=False,
        output=f"wrote {len(data)} bytes to {resolved.relative_to(state.workdir.resolve())}",
        detail={"path": str(resolved), "bytes": len(data)},
    )
