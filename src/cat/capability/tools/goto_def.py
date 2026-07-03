"""goto_def tool — find where a Python symbol is defined (docker-only)."""
from __future__ import annotations

from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools.outline import _run_symbol_nav

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "symbol name to locate (function, class, variable)"},
        "root": {"type": "string", "description": "project root for the search (default /testbed)"},
    },
    "required": ["name"],
}


def create_goto_def_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the goto_def capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(tool="goto_def", is_error=True, output="missing 'name'")
        return _run_symbol_nav("goto_def", args, exec_fn, default_workdir, "goto_def", state)

    return Capability(
        name="goto_def",
        description=(
            "Find where a Python symbol is defined (class, function, variable). "
            "Returns up to 20 {path, line, col, type, name} hits. "
            "Prefer this over `rg(\"def foo|class foo\")` — works through imports and "
            "respects Python scoping."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
