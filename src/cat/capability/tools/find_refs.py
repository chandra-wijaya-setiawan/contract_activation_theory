"""find_refs tool — find all references to a Python symbol (docker-only)."""
from __future__ import annotations

from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools.outline import _run_symbol_nav

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "symbol name to find references of"},
        "root": {"type": "string", "description": "project root (default /testbed)"},
    },
    "required": ["name"],
}


def create_find_refs_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the find_refs capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(tool="find_refs", is_error=True, output="missing 'name'")
        return _run_symbol_nav("find_refs", args, exec_fn, default_workdir, "find_refs", state)

    return Capability(
        name="find_refs",
        description=(
            "Find all references to a Python symbol across the project. "
            "Returns up to 50 {path, line, col, snippet} hits. "
            "Prefer this over `rg(name)` for 'who calls X' / 'where is X used' questions — "
            "filters out string matches and respects Python scoping."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
