"""Capabilities — the executable side of a contract.

A capability is a named, schema-described action the Activator can invoke under
a contract. Capabilities have no state of their own; they read/write the
provided State and return a structured ToolResult that the Activator records
and surfaces to the Criterion predicates.

v0.0.1 capabilities (matched to Pi's baseline for fair comparison):
    bash    — run a shell command in the workdir
    write   — write a file (creates parents)
    read    — read a file (with size cap)
    list    — list workdir contents (one level, with sizes)

Discipline notes:
- Every tool result is captured in State.last_tool_result so Criterion
  predicates see exactly what the LLM saw.
- Bash output is truncated at OUTPUT_CAP_BYTES to keep token budgets honest.
- write/read paths must stay within workdir (no escapes).
"""
from __future__ import annotations
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from cat.contracts import State
from cat.trace import FailureLayer, Recorder, TraceEvent, utcnow_iso


OUTPUT_CAP_BYTES = 8_000
READ_CAP_BYTES = 64_000


@dataclass
class ToolResult:
    tool: str
    is_error: bool
    output: str
    detail: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0


Precondition = Callable[[dict[str, Any], State], Optional[str]]


@dataclass
class Capability:
    name: str
    description: str
    json_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], State], ToolResult]
    preconditions: list[Precondition] = field(default_factory=list)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if cap.name in self._caps:
            raise ValueError(f"capability {cap.name!r} already registered")
        self._caps[cap.name] = cap

    def get(self, name: str) -> Optional[Capability]:
        return self._caps.get(name)

    def names(self) -> list[str]:
        return list(self._caps)

    def tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-compatible tool schemas for chat.completions(tools=...)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": c.name,
                    "description": c.description,
                    "parameters": c.json_schema,
                },
            }
            for c in self._caps.values()
        ]

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        state: State,
        recorder: Optional[Recorder] = None,
        contract_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> ToolResult:
        cap = self._caps.get(name)
        if cap is None:
            return ToolResult(tool=name, is_error=True, output=f"unknown tool: {name}")

        call_id = tool_call_id or f"tc-{uuid.uuid4().hex[:8]}"

        for pre in cap.preconditions:
            violation = pre(args, state)
            if violation is not None:
                if recorder is not None:
                    recorder.append(TraceEvent(
                        event_id=f"fail-{uuid.uuid4().hex[:6]}",
                        parent_id=contract_id,
                        ts=utcnow_iso(),
                        kind="failure_attribution",
                        payload={
                            "layer": "environment_contract",
                            "description": violation,
                            "tool": name,
                            "args": args,
                        },
                    ))
                return ToolResult(
                    tool=name, is_error=True,
                    output=f"[PRECONDITION] {violation}",
                    detail={"precondition_failed": True, "failure_layer": "environment_contract"},
                )

        if recorder is not None:
            recorder.append(TraceEvent(
                event_id=call_id,
                parent_id=contract_id,
                ts=utcnow_iso(),
                kind="tool_call",
                payload={"tool": name, "args": args, "tool_call_id": call_id},
            ))

        t0 = time.time()
        try:
            result = cap.handler(args, state)
        except Exception as exc:  # noqa: BLE001 — capability errors must not crash the run
            result = ToolResult(
                tool=name, is_error=True,
                output=f"{type(exc).__name__}: {exc}"[:OUTPUT_CAP_BYTES],
            )
        result.wall_s = time.time() - t0
        state.last_tool_result = {
            "tool": name,
            "args": args,
            "is_error": result.is_error,
            "output": result.output,
            "detail": result.detail,
        }

        if recorder is not None:
            recorder.append(TraceEvent(
                event_id=f"{call_id}-r",
                parent_id=call_id,
                ts=utcnow_iso(),
                kind="tool_result",
                payload={
                    "tool": name,
                    "tool_call_id": call_id,
                    "output": result.output,
                    "is_error": result.is_error,
                    "detail": result.detail,
                    "wall_s": round(result.wall_s, 3),
                },
            ))
        return result


# ── Built-in capabilities ───────────────────────────────────────────────────


def _resolve_inside_workdir(path_str: str, workdir: Path) -> Path:
    """Resolve `path_str` and ensure it is inside `workdir`. Raises ValueError otherwise."""
    p = Path(path_str)
    if not p.is_absolute():
        p = (workdir / p)
    p_resolved = p.resolve()
    wd_resolved = workdir.resolve()
    try:
        p_resolved.relative_to(wd_resolved)
    except ValueError as exc:
        raise ValueError(f"path {path_str!r} escapes workdir {wd_resolved}") from exc
    return p_resolved


def _truncate(s: str, cap: int) -> str:
    if len(s.encode("utf-8", errors="replace")) <= cap:
        return s
    # Byte-safe truncation
    encoded = s.encode("utf-8", errors="replace")[:cap]
    return encoded.decode("utf-8", errors="replace") + f"\n... [truncated at {cap} bytes]"


def _h_bash(args: dict[str, Any], state: State) -> ToolResult:
    cmd = str(args.get("command", "")).strip()
    timeout_s = float(args.get("timeout_s", 60))
    if not cmd:
        return ToolResult(tool="bash", is_error=True, output="empty command")
    try:
        # On Windows we still want a POSIX-ish shell when available; otherwise
        # fall back to system shell. Most agents will call python/pytest/etc.
        # so the shell choice rarely matters for tool semantics.
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


def _h_write(args: dict[str, Any], state: State) -> ToolResult:
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


def _h_read(args: dict[str, Any], state: State) -> ToolResult:
    path = args.get("path")
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


def _h_list(args: dict[str, Any], state: State) -> ToolResult:
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


def _h_str_replace(args: dict[str, Any], state: State) -> ToolResult:
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
        return ToolResult(tool="str_replace", is_error=True, output="old_str not found in file")
    if count > 1:
        return ToolResult(tool="str_replace", is_error=True,
                          output=f"old_str matches {count} locations — make it more specific")
    new_content = content.replace(old_str, new_str, 1)
    resolved.write_text(new_content, encoding="utf-8")
    return ToolResult(tool="str_replace", is_error=False,
                      output=f"replaced 1 occurrence in {resolved.relative_to(state.workdir.resolve())}")


# ── Preconditions for built-in capabilities ───────────────────────────────


def _pre_read_file_exists(args: dict[str, Any], state: State) -> Optional[str]:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "missing 'path' argument"
    try:
        resolved = _resolve_inside_workdir(path, state.workdir)
    except ValueError:
        return f"path '{path}' is outside workdir"
    if not resolved.exists():
        return f"file '{path}' does not exist; use 'list' to see available files"
    return None


def _pre_bash_non_empty(args: dict[str, Any], state: State) -> Optional[str]:
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "empty command"
    return None


def build_default_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(Capability(
        name="bash",
        description="Run a shell command in the workdir. Returns stdout+stderr, truncated. Returns is_error=true on non-zero exit.",
        json_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "shell command to execute"},
                "timeout_s": {"type": "number", "description": "kill the command after this many seconds (default 60)"},
            },
            "required": ["command"],
        },
        handler=_h_bash,
        preconditions=[_pre_bash_non_empty],
    ))
    reg.register(Capability(
        name="write",
        description="Write a file in the workdir. Creates parent directories. Overwrites existing files.",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "file path relative to workdir"},
                "content": {"type": "string", "description": "full file contents"},
            },
            "required": ["path", "content"],
        },
        handler=_h_write,
    ))
    reg.register(Capability(
        name="read",
        description="Read a file from the workdir. Output capped at 64KB.",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "file path relative to workdir"},
            },
            "required": ["path"],
        },
        handler=_h_read,
        preconditions=[_pre_read_file_exists],
    ))
    reg.register(Capability(
        name="list",
        description="List entries (one level) under a directory relative to workdir. Default '.'.",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "directory path relative to workdir (default '.')"},
            },
        },
        handler=_h_list,
    ))
    reg.register(Capability(
        name="str_replace",
        description=(
            "Edit an existing file by replacing an exact string. "
            "old_str must match exactly once — make it specific enough to be unique. "
            "Prefer this over write for editing existing files; no need to read the whole file first."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "file path relative to workdir"},
                "old_str": {"type": "string", "description": "exact string to find (must appear exactly once)"},
                "new_str": {"type": "string", "description": "replacement string (use empty string to delete)"},
            },
            "required": ["path", "old_str"],
        },
        handler=_h_str_replace,
    ))
    return reg


def build_tmux_registry(session: object) -> "CapabilityRegistry":
    """Build a CapabilityRegistry that routes all tool calls through a TmuxSession.

    `session` must have `send_keys(keys, block, max_timeout_sec)` and
    `capture_pane() -> str`. Passing the terminal_bench TmuxSession object
    satisfies this interface.

    All bash/read/list calls use a file-based capture pattern to avoid the
    stale-viewport bug: command output is redirected to a temp file, then the
    viewport is reset with `clear` before reading the file, guaranteeing
    `capture_pane()` sees only the current command's output.
    """
    import base64
    import re as _re

    def _file_capture(cmd: str, timeout: float, tool: str, state: State) -> ToolResult:
        """Run cmd, capture its output via temp file + clear, return ToolResult.

        Output display strategy (avoids viewport overflow):
        - Short output (≤ 38 lines): show all lines with cat.
        - Long output (> 38 lines): show first 18 + '...' + last 19 lines.
          The last line is always the CAT_EXIT:N sentinel, so the exit code
          is always visible regardless of total output length.
        This guarantees at most ~40 visible lines, fitting any reasonable
        tmux pane height, so capture_pane() sees only the current output.
        """
        tmp = f"/tmp/cat_tmux_{uuid.uuid4().hex[:8]}.txt"
        run_cmd = f'({cmd}) > {tmp} 2>&1; echo "CAT_EXIT:$?" >> {tmp}'
        try:
            session.send_keys([run_cmd, "Enter"], block=True, max_timeout_sec=timeout)
        except TimeoutError:
            return ToolResult(tool=tool, is_error=True, output=f"TIMEOUT after {timeout}s")
        # Reset viewport, then show short outputs in full or long ones head+tail
        # threshold=48: a 50-row pane can display 48 content lines + prompt cleanly.
        # For longer output, head-20 + tail-25 keeps total ≤47 lines, always visible.
        read_cmd = (
            f"N=$(wc -l < {tmp}); clear; "
            f"if [ \"$N\" -le 48 ]; then cat {tmp}; "
            f"else echo \"[out: $N lines - first/last shown]\"; "
            f"head -20 {tmp}; echo '...'; tail -25 {tmp}; fi; "
            f"rm -f {tmp}"
        )
        try:
            session.send_keys([read_cmd, "Enter"], block=True, max_timeout_sec=15)
        except TimeoutError:
            return ToolResult(tool=tool, is_error=True, output="TIMEOUT reading output file")
        raw = session.capture_pane()
        state.kv["terminal_output"] = raw
        m = _re.search(r"CAT_EXIT:(\d+)", raw)
        exit_code = int(m.group(1)) if m else 0
        clean = _re.sub(r"\nCAT_EXIT:\d+", "", raw).strip()
        return ToolResult(
            tool=tool,
            is_error=(exit_code != 0),
            output=_truncate(clean, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code},
        )

    def _bash(args: dict, state: State) -> ToolResult:
        cmd = str(args.get("command", "")).strip()
        timeout = float(args.get("timeout_s", 60.0))
        if not cmd:
            return ToolResult(tool="bash", is_error=True, output="empty command")
        return _file_capture(cmd, timeout, "bash", state)

    def _write(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        content = args.get("content", "")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="write", is_error=True, output="missing 'path'")
        # mkdir -p parent, then write via base64 pipeline (handles any content)
        parent = str(Path(path).parent)
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        try:
            session.send_keys([f"mkdir -p {parent}", "Enter"], block=True, max_timeout_sec=10)
            session.send_keys([f"echo '{b64}' | base64 -d > {path}", "Enter"],
                              block=True, max_timeout_sec=15)
        except TimeoutError:
            return ToolResult(tool="write", is_error=True, output="TIMEOUT writing file")
        return ToolResult(tool="write", is_error=False,
                          output=f"wrote {len(content)} chars to {path}")

    def _read(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="read", is_error=True, output="missing 'path'")
        return _file_capture(f"cat {path}", 15.0, "read", state)

    def _list(args: dict, state: State) -> ToolResult:
        path = args.get("path", ".")
        return _file_capture(f"ls -la {path}", 10.0, "list", state)

    def _str_replace(args: dict, state: State) -> ToolResult:
        path = args.get("path", "")
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        if not path:
            return ToolResult(tool="str_replace", is_error=True, output="missing 'path'")
        if not old_str:
            return ToolResult(tool="str_replace", is_error=True, output="missing 'old_str'")
        # Pass strings via base64 to avoid shell quoting issues
        old_b64 = base64.b64encode(old_str.encode("utf-8")).decode("ascii")
        new_b64 = base64.b64encode(new_str.encode("utf-8")).decode("ascii")
        # Write a temp Python script to disk, then execute it
        script = (
            "import base64, sys\n"
            f"old = base64.b64decode('{old_b64}').decode('utf-8')\n"
            f"new = base64.b64decode('{new_b64}').decode('utf-8')\n"
            f"path = {repr(path)}\n"
            "try:\n"
            "    content = open(path).read()\n"
            "except FileNotFoundError:\n"
            "    print(f'ERROR: file not found: {path}'); sys.exit(1)\n"
            "n = content.count(old)\n"
            "if n == 0: print('ERROR: old_str not found in file'); sys.exit(1)\n"
            "if n > 1: print(f'ERROR: {n} matches — make old_str more specific'); sys.exit(1)\n"
            "open(path, 'w').write(content.replace(old, new, 1))\n"
            f"print(f'OK: replaced 1 occurrence in {path}')\n"
        )
        script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/cat_edit_{uuid.uuid4().hex[:8]}.py"
        try:
            session.send_keys([f"echo '{script_b64}' | base64 -d > {tmp}", "Enter"],
                              block=True, max_timeout_sec=10)
        except TimeoutError:
            return ToolResult(tool="str_replace", is_error=True, output="TIMEOUT writing edit script")
        return _file_capture(f"python3 {tmp}; rm -f {tmp}", 15.0, "str_replace", state)

    reg = CapabilityRegistry()
    reg.register(Capability(
        name="bash",
        description=(
            "Run a shell command in the container terminal. "
            "Output ≤48 lines is shown in full; longer output shows first 20 + last 25 lines. "
            "IMPORTANT: for reading code sections use ranges of ≤45 lines: "
            "e.g. 'sed -n \\'104,148p\\' file.py' — this fits entirely and nothing is truncated. "
            "Use grep -n to locate line numbers first, then sed -n with a ≤45-line window."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "shell command to execute"},
                "timeout_s": {"type": "number", "description": "seconds before timeout (default 60)"},
            },
            "required": ["command"],
        },
        handler=_bash,
    ))
    reg.register(Capability(
        name="write",
        description="Write content to a file in the container. Creates parent dirs. Overwrites existing.",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute file path inside the container"},
                "content": {"type": "string", "description": "full file contents (UTF-8)"},
            },
            "required": ["path", "content"],
        },
        handler=_write,
    ))
    reg.register(Capability(
        name="read",
        description=(
            "Read a file from the container. Shows full content for short files; "
            "first 18 + last 19 lines for large files. "
            "For specific sections of large files, use bash with sed -n 'X,Yp' instead."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute file path inside the container"},
            },
            "required": ["path"],
        },
        handler=_read,
    ))
    reg.register(Capability(
        name="list",
        description="List directory contents in the container via ls -la.",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "path to list (default '.')"},
            },
        },
        handler=_list,
    ))
    reg.register(Capability(
        name="str_replace",
        description=(
            "Edit an existing file by replacing an exact string. "
            "old_str must match exactly once — make it specific enough (include surrounding lines). "
            "Prefer this over write for patching existing files; no need to read the whole file first."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute file path inside the container"},
                "old_str": {"type": "string", "description": "exact string to find (must appear exactly once)"},
                "new_str": {"type": "string", "description": "replacement string (empty string to delete)"},
            },
            "required": ["path", "old_str"],
        },
        handler=_str_replace,
    ))
    return reg


def workdir_snapshot(workdir: Path, max_entries: int = 30) -> str:
    """Compact textual snapshot of the workdir for the judge's STATE SUMMARY."""
    lines: list[str] = []
    try:
        wd = workdir.resolve()
        for i, p in enumerate(sorted(wd.rglob("*"))):
            if i >= max_entries:
                lines.append(f"... [{max_entries}+ entries; truncated]")
                break
            rel = p.relative_to(wd)
            if p.is_dir():
                lines.append(f"  {rel}/")
            else:
                lines.append(f"  {rel} ({p.stat().st_size}B)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(snapshot error: {exc})")
    return "\n".join(lines) or "(empty workdir)"
