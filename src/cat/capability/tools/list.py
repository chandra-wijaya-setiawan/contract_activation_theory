"""List tool — list workdir contents."""
from __future__ import annotations

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _resolve_inside_workdir

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "directory path relative to workdir (default '.')"},
    },
}


def create_list_capability() -> Capability:
    return Capability(
        name="list",
        description="List entries (one level) under a directory relative to workdir. Default '.'.",
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    path = args.get("path", ".")
    try:
        resolved = _resolve_inside_workdir(str(path), state.workdir)
    except ValueError as exc:
        return ToolResult(tool="list", is_error=True, output=str(exc))
    if not resolved.exists():
        return ToolResult(tool="list", is_error=True, output=f"not found: {path}")
    if resolved.is_file():
        return ToolResult(tool="list", is_error=False, output=f"file: {resolved.name}", detail={"size": resolved.stat().st_size})
    entries: list[str] = []
    for child in sorted(resolved.iterdir()):
        marker = "/" if child.is_dir() else ""
        size = "" if child.is_dir() else f" {child.stat().st_size}B"
        entries.append(f"{child.name}{marker}{size}")
    return ToolResult(tool="list", is_error=False, output="\n".join(entries) or "(empty)", detail={"count": len(entries)})
