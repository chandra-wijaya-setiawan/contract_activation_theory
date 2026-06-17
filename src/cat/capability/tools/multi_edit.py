"""multi_edit tool — Pi-style atomic multi-block edit (docker-only)."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "path":  {"type": "string", "description": "absolute path inside the container"},
        "edits": {
            "type": "array",
            "description": "list of replacements; each {old_text, new_text}",
            "items": {
                "type": "object",
                "properties": {
                    "old_text": {"type": "string", "description": "exact string to find (unique in original)"},
                    "new_text": {"type": "string", "description": "replacement (empty to delete)"},
                },
                "required": ["old_text", "new_text"],
            },
        },
    },
    "required": ["path", "edits"],
}


def create_multi_edit_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the multi_edit capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        edits = args.get("edits")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="multi_edit", is_error=True, output="missing 'path'")
        if not isinstance(edits, list) or not edits:
            return ToolResult(tool="multi_edit", is_error=True,
                              output="missing 'edits' (list of {old_text, new_text})")
        edits_json = json.dumps(edits)
        edits_b64 = base64.b64encode(edits_json.encode("utf-8")).decode("ascii")
        script_text = (
            "import base64, json, sys\n"
            f"path = {path!r}\n"
            f"edits = json.loads(base64.b64decode('{edits_b64}').decode('utf-8'))\n"
            "try:\n"
            "    original = open(path).read()\n"
            "except FileNotFoundError:\n"
            "    print(f'ERROR: file not found: {path}'); sys.exit(2)\n"
            "# Validate every edit against the ORIGINAL (Pi semantics).\n"
            "applied = []\n"
            "for i, e in enumerate(edits):\n"
            "    old = e.get('old_text', e.get('oldText', ''))\n"
            "    new = e.get('new_text', e.get('newText', ''))\n"
            "    if not isinstance(old, str) or not old:\n"
            "        print(f'ERROR: edit {i}: missing old_text'); sys.exit(3)\n"
            "    n = original.count(old)\n"
            "    if n == 0:\n"
            "        print(f'ERROR: edit {i}: old_text not found'); sys.exit(3)\n"
            "    if n > 1:\n"
            "        print(f'ERROR: edit {i}: old_text matches {n} locations — make it more specific'); sys.exit(3)\n"
            "    applied.append((old, new))\n"
            "# All validated; apply sequentially (each match against current state,\n"
            "# but since each old was unique in the ORIGINAL and edits must not\n"
            "# overlap, this is equivalent to the atomic semantics).\n"
            "content = original\n"
            "for old, new in applied:\n"
            "    if content.count(old) != 1:\n"
            "        print(f'ERROR: edits overlap — old_text appears {content.count(old)} times after prior edits'); sys.exit(4)\n"
            "    content = content.replace(old, new, 1)\n"
            "open(path, 'w').write(content)\n"
            "print(f'OK: applied {len(applied)} edits to {path}')\n"
        )
        script_b64 = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/cat_medit_{uuid.uuid4().hex[:8]}.py"
        stage_and_run = (
            f"echo {script_b64} | base64 -d > {tmp} && python3 {tmp}; rc=$?; rm -f {tmp}; exit $rc"
        )
        exit_code, output = exec_fn(["sh", "-c", stage_and_run])
        return ToolResult(
            tool="multi_edit",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "path": path, "n_edits": len(edits)},
        )

    return Capability(
        name="multi_edit",
        description=(
            "Apply N (old_text → new_text) replacements to one file atomically. "
            "Each old_text matches against the ORIGINAL file (not after prior edits), "
            "so order doesn't matter and line numbers can't drift. "
            "Every old_text must appear exactly once in the original; otherwise the whole call aborts. "
            "Prefer this over multiple `str_replace` calls when fixing several hunks in one file."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
