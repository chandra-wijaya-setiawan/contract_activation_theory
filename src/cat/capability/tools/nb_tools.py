"""nb_tools — native CAT tool for notebook cell inspection and editing.

Wraps .claude/nb_tools.py operations as a first-class tool so the Performer
can read/write/insert/append notebook cells without shelling out.

The tool resolves <notebook> relative to workdir. Cell indices are 0-based.
For write/insert, content is passed directly as a string (not a file path).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _resolve_inside_workdir, _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "enum": ["list", "toc", "read", "write", "insert", "append",
                     "find", "write-output", "update-toc"],
            "description": "nb_tools.py subcommand",
        },
        "notebook": {
            "type": "string",
            "description": "Path to .ipynb file (relative to workdir)",
        },
        "cell_idx": {
            "type": "integer",
            "description": "0-based cell index (required for read/write/insert)",
        },
        "content": {
            "type": "string",
            "description": "Cell source content (required for write/insert/append)",
        },
        "keyword": {
            "type": "string",
            "description": "Search keyword (required for find)",
        },
        "output_text": {
            "type": "string",
            "description": "Output text to inject (required for write-output)",
        },
        "cell_type": {
            "type": "string",
            "enum": ["code", "markdown"],
            "description": "Cell type for append/insert (default: code)",
        },
    },
    "required": ["command", "notebook"],
}


def create_nb_tools_capability() -> Capability:
    return Capability(
        name="nb_tools",
        description=(
            "Inspect and edit Jupyter notebook cells.\n\n"
            "Commands:\n"
            "  list       — list all cells with index, type, preview\n"
            "  toc        — table of contents (markdown cells only)\n"
            "  read       — read cell source (requires cell_idx)\n"
            "  write      — replace cell source (requires cell_idx, content)\n"
            "  insert     — insert new cell before idx (requires cell_idx, content)\n"
            "  append     — add cell at end (requires content, cell_type)\n"
            "  find       — search cells for keyword (requires keyword)\n"
            "  write-output — inject output into code cell (requires cell_idx, output_text)\n"
            "  update-toc — regenerate the TOC markdown cell\n\n"
            "All paths are relative to workdir. Cell indices are 0-based."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _handle(args: dict, state: State) -> ToolResult:
    cmd = args.get("command", "")
    nb_rel = args.get("notebook", "")
    if not nb_rel:
        return ToolResult(tool="nb_tools", is_error=True, output="missing 'notebook' argument")

    try:
        nb_path = _resolve_inside_workdir(nb_rel, state.workdir)
    except ValueError as exc:
        return ToolResult(tool="nb_tools", is_error=True, output=str(exc))

    if not nb_path.exists():
        return ToolResult(tool="nb_tools", is_error=True, output=f"notebook not found: {nb_path}")

    # Find nb_tools.py: same dir as notebook, or workdir
    nb_tools_script = nb_path.parent / ".claude" / "nb_tools.py"
    if not nb_tools_script.exists():
        nb_tools_script = state.workdir / ".claude" / "nb_tools.py"
    if not nb_tools_script.exists():
        return ToolResult(
            tool="nb_tools", is_error=True,
            output="nb_tools.py not found (looked in .claude/ under notebook dir and workdir)",
        )

    # Build the command
    cmd_args = [sys.executable, str(nb_tools_script), cmd, str(nb_path)]

    if cmd in ("read", "write", "insert", "write-output"):
        cell_idx = args.get("cell_idx")
        if cell_idx is None:
            return ToolResult(tool="nb_tools", is_error=True, output=f"command '{cmd}' requires cell_idx")
        cmd_args.append(str(cell_idx))

    if cmd == "write" or cmd == "insert" or cmd == "append":
        content = args.get("content", "")
        if not content and cmd != "append":
            return ToolResult(tool="nb_tools", is_error=True, output=f"command '{cmd}' requires content")
        # Write content to temp file, pass file path
        cell_type = args.get("cell_type", "code")
        suffix = ".md" if cell_type == "markdown" else ".py"
        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            cmd_args.append(tmp.name)

    if cmd == "write-output":
        output_text = args.get("output_text", "")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(output_text)
            tmp.flush()
            cmd_args.append(tmp.name)

    if cmd == "find":
        keyword = args.get("keyword", "")
        if not keyword:
            return ToolResult(tool="nb_tools", is_error=True, output="command 'find' requires keyword")
        cmd_args.append(keyword)

    if cmd == "append":
        cell_type = args.get("cell_type", "code")
        cmd_args.append(cell_type)

    try:
        proc = subprocess.run(
            cmd_args, capture_output=True, text=True, timeout=30,
            cwd=str(state.workdir), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(tool="nb_tools", is_error=True, output="timeout after 30s")
    except Exception as exc:
        return ToolResult(tool="nb_tools", is_error=True, output=f"error: {exc}")

    out = proc.stdout.strip()
    if proc.returncode != 0:
        err = proc.stderr.strip() or out
        return ToolResult(tool="nb_tools", is_error=True,
                          output=_truncate(err, OUTPUT_CAP_BYTES))

    return ToolResult(tool="nb_tools", is_error=False,
                      output=_truncate(out, OUTPUT_CAP_BYTES))
