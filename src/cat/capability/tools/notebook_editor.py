"""notebook_editor — native CAT tool for notebook cell inspection, editing, and execution.

Uses SQLite-backed persistence (notebook_store) as the single source of truth.
The .ipynb file is only read once on import and written back on sync.

Commands:
  inspect:  list, toc, read, find, stats
  edit:     write, insert, append, delete, update-toc
  answer:   insert a green-box AIAP answer cell
  execute:  extract cell → run as .py → inject output
  store:    load (import .ipynb), sync (export to .ipynb), info (store stats)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _resolve_inside_workdir, _truncate, OUTPUT_CAP_BYTES
from cat.capability.tools.notebook_store import NotebookStore

# ── Green-box answer template ────────────────────────────────────────────────

_GREEN_BOX_PREFIX = (
    '<div style="background-color: #f0fff4; '
    'border-left: 4px solid #4CAF50; padding: 12px; '
    'border-radius: 4px; color: #1a1a1a;">\n\n'
)
_GREEN_BOX_SUFFIX = '\n\n</div>'


def _wrap_green_box(content: str) -> str:
    return _GREEN_BOX_PREFIX + content.strip() + _GREEN_BOX_SUFFIX


# ── Agent store directory ────────────────────────────────────────────────────

def _store_dir(workdir: Path) -> Path:
    """Return the agent's notebook store directory: data/agent/aiap/ under workdir."""
    store_dir = workdir / "data" / "agent" / "aiap"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


def _get_or_create_store(workdir: Path, notebook_name: str) -> NotebookStore:
    """Get or create a notebook store. Auto-imports from .ipynb if new."""
    store_dir = _store_dir(workdir)
    store = NotebookStore(store_dir, notebook_name)

    # If store is empty, try to import from the .ipynb
    if store.cell_count() == 0:
        nb_path = workdir / notebook_name
        if nb_path.exists():
            store.load_from_ipynb(nb_path)

    return store


# ── JSON schema ──────────────────────────────────────────────────────────────

_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "enum": [
                "list", "toc", "read", "find", "stats",   # inspect
                "write", "insert", "append", "delete",     # edit
                "answer",                                   # green-box answer
                "execute",                                  # extract → run → inject
                "store",                                    # load/sync/info
            ],
        },
        "notebook": {
            "type": "string",
            "description": "Path to .ipynb file (relative to workdir)",
        },
        "cell_idx": {
            "type": "integer",
            "description": "0-based cell index (required for read/write/insert/delete/answer/execute)",
        },
        "content": {
            "type": "string",
            "description": "Cell source content (required for write/insert/append/answer)",
        },
        "keyword": {
            "type": "string",
            "description": "Search keyword (required for find)",
        },
        "cell_type": {
            "type": "string",
            "enum": ["code", "markdown"],
            "description": "Cell type for write/insert/append (default: code)",
        },
        "timeout_s": {
            "type": "number",
            "description": "Execution timeout for 'execute' (default: 60)",
        },
        "store_action": {
            "type": "string",
            "enum": ["load", "sync", "info"],
            "description": "Store sub-command (required for 'store' command)",
        },
    },
    "required": ["command", "notebook"],
}


def create_notebook_editor_capability() -> Capability:
    return Capability(
        name="notebook_editor",
        description=(
            "Inspect, edit, and execute Jupyter notebook cells with SQLite persistence.\n\n"
            "All cell data is stored in data/agent/aiap/<notebook>.sqlite — the single "
            "source of truth. The .ipynb is only read on import and written on sync.\n\n"
            "INSPECT:\n"
            "  list                     — list all cells: idx, type, preview\n"
            "  toc                      — table of contents (markdown headings)\n"
            "  read <idx>               — read cell source\n"
            "  find <keyword>           — search cells\n"
            "  stats                    — notebook statistics (counts, empty cells, etc.)\n\n"
            "EDIT:\n"
            "  write <idx> <content> [type]  — replace cell source\n"
            "  insert <idx> <content> [type] — insert new cell before idx\n"
            "  append <content> [type]       — add cell at end\n"
            "  delete <idx>                  — remove cell, shift subsequent down\n\n"
            "ANSWER (AIAP green-box):\n"
            "  answer <idx> <content>  — insert green-box HTML answer before idx.\n"
            "    Content is wrapped in a styled div that scores well in AIAP evaluations.\n"
            "    Include a 'My understanding' section for best results.\n\n"
            "EXECUTE:\n"
            "  execute <idx> [timeout_s] — extract cell → run as .py → inject output\n\n"
            "STORE (persistence):\n"
            "  store load   — import .ipynb into SQLite store (one-time)\n"
            "  store sync   — write store back to .ipynb file\n"
            "  store info   — show store metadata and file paths"
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cell_preview(source: str, max_len: int = 80) -> str:
    """One-line preview of cell source."""
    flat = source.replace("\n", " ").strip()
    if len(flat) > max_len:
        return flat[:max_len] + "…"
    return flat or "(empty)"


def _row_to_display(row: dict) -> str:
    """Format a cell row for display."""
    idx = row["idx"]
    ctype = row["cell_type"]
    src = row.get("source", "")
    preview = _cell_preview(src)
    exec_info = ""
    if ctype == "code" and row.get("exec_info"):
        exec_info = f" [exec:{row['exec_info']}]"
    return f"[{idx:3d}] {ctype:8s}  {preview}{exec_info}"


# ── Handler ──────────────────────────────────────────────────────────────────

def _handle(args: dict, state: State) -> ToolResult:
    cmd = args.get("command", "")
    nb_rel = args.get("notebook", "")
    if not nb_rel:
        return ToolResult(tool="notebook_editor", is_error=True, output="missing 'notebook'")

    try:
        nb_path = _resolve_inside_workdir(nb_rel, state.workdir)
    except ValueError as exc:
        return ToolResult(tool="notebook_editor", is_error=True, output=str(exc))

    # Get or create the store (auto-imports from .ipynb if needed)
    try:
        store = _get_or_create_store(state.workdir, nb_rel)
    except Exception as exc:
        return ToolResult(tool="notebook_editor", is_error=True,
                          output=f"store error: {exc}")

    # ── STORE commands ──────────────────────────────────────────────────

    # Handle compound store commands: "store load", "store sync", "store info"
    if cmd.startswith("store"):
        action = cmd.replace("store", "").strip() or args.get("store_action", "info")
        if action == "load":
            if not nb_path.exists():
                return ToolResult(tool="notebook_editor", is_error=True,
                                  output=f".ipynb not found: {nb_path}")
            try:
                count = store.load_from_ipynb(nb_path)
                return ToolResult(tool="notebook_editor", is_error=False,
                                  output=f"Imported {count} cells from {nb_path}")
            except Exception as exc:
                return ToolResult(tool="notebook_editor", is_error=True,
                                  output=f"import failed: {exc}")

        if action == "sync":
            try:
                count = store.sync_to_ipynb(nb_path)
                return ToolResult(tool="notebook_editor", is_error=False,
                                  output=f"Synced {count} cells to {nb_path}")
            except Exception as exc:
                return ToolResult(tool="notebook_editor", is_error=True,
                                  output=f"sync failed: {exc}")

        # info
        stats = store.get_stats()
        source_file = store.get_meta("source_file") or str(nb_path)
        lines = [
            f"Store: {store.db_path}",
            f"Source: {source_file}",
            f"Cells: {stats['total_cells']} total "
            f"({stats['code_cells']} code, {stats['markdown_cells']} markdown)",
            f"Empty code cells: {stats['empty_code_cells']}",
            f"Code cells without output: {stats['code_without_output']}",
            f"Personal voice cells: {stats['personal_voice_cells']}",
        ]
        if stats['empty_code_cells'] > 0:
            empty = store.find_empty_code_cells()
            lines.append(f"  Empty at: {empty[:10]}{'...' if len(empty) > 10 else ''}")
        if stats['personal_voice_cells'] > 0:
            pv = store.find_cells_with_personal_voice()
            lines.append(f"  Personal voice at: {pv[:10]}{'...' if len(pv) > 10 else ''}")
        return ToolResult(tool="notebook_editor", is_error=False,
                          output="\n".join(lines))

    # ── INSPECT commands ────────────────────────────────────────────────

    if cmd == "list":
        cells = store.list_cells()
        if not cells:
            return ToolResult(tool="notebook_editor", is_error=False,
                              output="(empty notebook — run 'store load' first)")
        lines = [f"[{c['idx']:3d}] {c['cell_type']:8s}  {_cell_preview(c.get('source', ''))}"
                 for c in cells]
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=_truncate("\n".join(lines), OUTPUT_CAP_BYTES))

    if cmd == "toc":
        md_cells = store.list_cells(cell_type="markdown")
        lines = []
        for c in md_cells:
            src = c.get("source", "")
            for line in src.splitlines():
                if line.startswith("#"):
                    lines.append(f"  [{c['idx']}] {line}")
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=_truncate("\n".join(lines) or "(no headings)",
                                           OUTPUT_CAP_BYTES))

    if cmd == "read":
        cell_idx = args.get("cell_idx")
        if cell_idx is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'read' requires cell_idx")
        cell = store.read_cell(cell_idx)
        if cell is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output=f"Cell {cell_idx} not found (max: {store.max_idx()})")
        lines = [
            f"── Cell {cell_idx} ({cell['cell_type']}) ──────────────────",
            "",
            cell.get("source", ""),
        ]
        if cell.get("outputs"):
            lines.append("")
            lines.append(f"[outputs: {len(cell['outputs'])}]")
        return ToolResult(tool="notebook_editor", is_error=False, output="\n".join(lines))

    if cmd == "find":
        keyword = args.get("keyword", "")
        if not keyword:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'find' requires keyword")
        cells = store.find_cells(keyword)
        if not cells:
            return ToolResult(tool="notebook_editor", is_error=False,
                              output=f"No cells found containing: {keyword!r}")
        lines = [f"{len(cells)} cell(s) matching {keyword!r}:"]
        for c in cells:
            lines.append(f"  [{c['idx']:3d}] {c['cell_type']:8s}  {_cell_preview(c.get('source', ''))}")
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=_truncate("\n".join(lines), OUTPUT_CAP_BYTES))

    if cmd == "stats":
        stats = store.get_stats()
        lines = [
            f"Cells: {stats['total_cells']} total",
            f"  Code: {stats['code_cells']}  |  Markdown: {stats['markdown_cells']}",
            f"Empty code cells: {stats['empty_code_cells']} ⚠️" if stats['empty_code_cells'] > 0
            else f"Empty code cells: 0 ✓",
            f"Code without output: {stats['code_without_output']}",
            f"Personal voice cells: {stats['personal_voice_cells']}",
        ]
        if stats['empty_code_cells'] > 0:
            empty = store.find_empty_code_cells()
            lines.append(f"  Empty indices: {empty}")
        return ToolResult(tool="notebook_editor", is_error=False, output="\n".join(lines))

    # ── EDIT commands ───────────────────────────────────────────────────

    if cmd == "write":
        cell_idx = args.get("cell_idx")
        content = args.get("content", "")
        cell_type = args.get("cell_type", "code")
        if cell_idx is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'write' requires cell_idx")
        if not store.write_cell(cell_idx, content, cell_type):
            return ToolResult(tool="notebook_editor", is_error=True,
                              output=f"Cell {cell_idx} not found")
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=f"Cell {cell_idx} updated ({cell_type}, {len(content)} chars)")

    if cmd == "insert":
        cell_idx = args.get("cell_idx")
        content = args.get("content", "")
        cell_type = args.get("cell_type", "code")
        if cell_idx is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'insert' requires cell_idx")
        total = store.insert_cell(cell_idx, content, cell_type)
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=f"Cell inserted at {cell_idx} ({cell_type}). Total: {total}")

    if cmd == "append":
        content = args.get("content", "")
        cell_type = args.get("cell_type", "code")
        if not content:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'append' requires content")
        idx = store.append_cell(content, cell_type)
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=f"Cell appended at index {idx} ({cell_type})")

    if cmd == "delete":
        cell_idx = args.get("cell_idx")
        if cell_idx is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'delete' requires cell_idx")
        if not store.delete_cell(cell_idx):
            return ToolResult(tool="notebook_editor", is_error=True,
                              output=f"Cell {cell_idx} not found")
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=f"Cell {cell_idx} deleted. Total: {store.cell_count()}")

    # ── ANSWER command (green-box) ───────────────────────────────────────

    if cmd == "answer":
        cell_idx = args.get("cell_idx")
        content = args.get("content", "")
        if cell_idx is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'answer' requires cell_idx")
        if not content:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'answer' requires content")
        wrapped = _wrap_green_box(content)
        total = store.insert_cell(cell_idx, wrapped, "markdown")
        return ToolResult(tool="notebook_editor", is_error=False,
                          output=f"Answer cell inserted at {cell_idx} (green-box markdown). Total: {total}")

    # ── EXECUTE command ──────────────────────────────────────────────────

    if cmd == "execute":
        cell_idx = args.get("cell_idx")
        if cell_idx is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output="command 'execute' requires cell_idx")
        timeout_s = float(args.get("timeout_s", 60))

        cell = store.read_cell(cell_idx)
        if cell is None:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output=f"Cell {cell_idx} not found")
        if cell["cell_type"] != "code":
            return ToolResult(tool="notebook_editor", is_error=True,
                              output=f"Cell {cell_idx} is {cell['cell_type']}, not code")

        source = cell.get("source", "").strip()
        if not source:
            return ToolResult(tool="notebook_editor", is_error=True,
                              output=f"Cell {cell_idx} is empty — nothing to execute")

        # Write to temp and run
        py_path = Path(tempfile.mktemp(suffix=".py", dir="/tmp"))
        out_path = Path(tempfile.mktemp(suffix=".txt", dir="/tmp"))
        py_path.write_text(source, encoding="utf-8")

        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(py_path)],
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(state.workdir), encoding="utf-8", errors="replace",
            )
            elapsed = time.time() - t0
            output = proc.stdout
            if proc.stderr:
                output += "\n[STDERR]\n" + proc.stderr
            out_path.write_text(output, encoding="utf-8")
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            output = f"TIMEOUT after {timeout_s}s"
            out_path.write_text(output, encoding="utf-8")
        except Exception as exc:
            elapsed = time.time() - t0
            output = f"ERROR: {exc}"
            out_path.write_text(output, encoding="utf-8")
        finally:
            try:
                py_path.unlink()
            except OSError:
                pass

        # Update store with output
        outputs = [{"output_type": "stream", "name": "stdout",
                    "text": output.splitlines(True)}]
        store.write_cell(cell_idx, source, "code",
                         outputs=outputs, exec_count=proc.returncode)

        # Also inject into the .ipynb if it exists
        if nb_path.exists():
            try:
                nb = json.load(open(nb_path, encoding="utf-8"))
                cells = nb.get("cells", [])
                if cell_idx < len(cells):
                    cells[cell_idx]["outputs"] = outputs
                    cells[cell_idx]["execution_count"] = proc.returncode
                    with open(nb_path, "w", encoding="utf-8") as f:
                        json.dump(nb, f, indent=1, ensure_ascii=False)
            except Exception:
                pass  # Don't fail execution if .ipynb write fails

        try:
            out_path.unlink()
        except OSError:
            pass

        is_error = "TIMEOUT" in output or output.startswith("ERROR:")
        summary = (
            f"Cell {cell_idx} executed in {elapsed:.1f}s | "
            f"returncode={proc.returncode} | "
            f"output_lines={len(output.splitlines())}"
        )
        return ToolResult(
            tool="notebook_editor",
            is_error=is_error,
            output=_truncate(f"{summary}\n\n{output}", OUTPUT_CAP_BYTES),
            detail={"cell_idx": cell_idx, "elapsed_s": round(elapsed, 2),
                    "returncode": proc.returncode},
        )

    return ToolResult(tool="notebook_editor", is_error=True, output=f"unknown command: {cmd}")
