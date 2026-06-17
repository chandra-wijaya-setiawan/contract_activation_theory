"""str_replace tool — replace an exact string in a file."""
from __future__ import annotations

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _resolve_inside_workdir

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "file path relative to workdir"},
        "old_str": {"type": "string", "description": "exact string to find (must appear exactly once)"},
        "new_str": {"type": "string", "description": "replacement string (use empty string to delete)"},
    },
    "required": ["path", "old_str"],
}


def create_str_replace_capability() -> Capability:
    return Capability(
        name="str_replace",
        description=(
            "Edit an existing file by replacing an exact string. "
            "old_str must match exactly once — make it specific enough to be unique. "
            "Prefer this over write for editing existing files; no need to read the whole file first."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    path = args.get("path")
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    if not isinstance(path, str) or not path:
        return ToolResult(tool="str_replace", is_error=True, output="missing 'path'")
    if not old_str:
        return ToolResult(tool="str_replace", is_error=True, output="missing 'old_str'")
    try:
        resolved = _resolve_inside_workdir(path, state.workdir)
    except ValueError as exc:
        return ToolResult(tool="str_replace", is_error=True, output=str(exc))
    if not resolved.exists():
        return ToolResult(tool="str_replace", is_error=True, output=f"not found: {path}")
    content = resolved.read_text(encoding="utf-8", errors="replace")
    count = content.count(old_str)
    if count == 0:
        # Widen search: find the most specific line of old_str that actually exists in the file.
        # Skip pure decorators/brackets (too generic); prefer lines with identifiers.
        _GENERIC = {"@staticmethod", "@classmethod", "@property", "pass", "return", "{", "}", "(", ")"}
        candidates = [
            l.strip() for l in old_str.splitlines()
            if l.strip() and not l.strip().startswith("#") and l.strip() not in _GENERIC
        ]
        # Sort by specificity: longer lines with word chars score higher; skip if >5 matches (too common)
        hint_lines = []
        for probe in sorted(candidates, key=lambda s: -len(s))[:5]:
            if len(probe) < 8:
                continue
            matches = [(i + 1, l) for i, l in enumerate(content.splitlines()) if probe in l]
            if not matches or len(matches) > 5:
                continue  # not found or too generic
            snip = "\n".join(f"  line {ln}: {txt[:120]}" for ln, txt in matches[:3])
            hint_lines.append(f"searched {probe!r:.60}:\n{snip}")
            break
        if not hint_lines:
            hint_lines.append("(no specific lines matched — file may have changed)")
        hint = "\n".join(hint_lines)
        msg = f"old_str not found in file. Widened search:\n{hint}\nRead the file to get exact text before retrying."
        return ToolResult(tool="str_replace", is_error=True, output=msg)
    if count > 1:
        return ToolResult(tool="str_replace", is_error=True,
                          output=f"old_str matches {count} locations — make it more specific")
    new_content = content.replace(old_str, new_str, 1)
    resolved.write_text(new_content, encoding="utf-8")
    return ToolResult(tool="str_replace", is_error=False,
                      output=f"replaced 1 occurrence in {resolved.relative_to(state.workdir.resolve())}")
