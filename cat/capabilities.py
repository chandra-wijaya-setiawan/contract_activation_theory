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
from cat.trace import Recorder, TraceEvent, utcnow_iso
import base64


OUTPUT_CAP_BYTES = 8_000
READ_CAP_BYTES = 64_000


@dataclass
class ToolResult:
    tool: str
    is_error: bool
    output: str
    detail: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0


@dataclass
class Capability:
    name: str
    description: str
    json_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], State], ToolResult]


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

    def tool_schemas(self, only: Optional[set[str]] = None) -> list[dict[str, Any]]:
        """OpenAI-compatible tool schemas. If `only` is given, restrict to that grant."""
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
            if only is None or c.name in only
        ]

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        state: State,
        recorder: Optional[Recorder] = None,
        contract_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
    ) -> ToolResult:
        cap = self._caps.get(name)
        if cap is None:
            return ToolResult(tool=name, is_error=True, output=f"unknown tool: {name}")

        call_id = tool_call_id or f"tc-{uuid.uuid4().hex[:8]}"

        # parent_event_id chains the tool_call to its triggering event (the
        # capability_invoke that issued it, or the REPRESENT probe write).
        # Falls back to contract_id only for legacy / test callers; new code
        # should always pass parent_event_id.
        tc_parent = parent_event_id if parent_event_id is not None else contract_id

        if recorder is not None:
            recorder.append(TraceEvent(
                event_id=call_id,
                parent_id=tc_parent,
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


# ── Docker exec registry (PRD 1.6.6.1) ─────────────────────────────────────
#
# Replaces tmux-based execution for non-interactive benchmarks (primarily
# SWE-bench). Each tool dispatches via docker SDK's `container.exec_run()` —
# a direct subprocess in the container. No tmux capture, no sentinel
# scraping, no pane viewport limits.
#
# This is the "right execution backend per domain" choice (see DESIGN.md
# "Benchmark-adapter pattern"). The tmux registry stays available for
# benchmarks whose tasks genuinely need an interactive shell.


def build_docker_registry(container: "object", default_workdir: str = "/testbed") -> "CapabilityRegistry":
    """Build a CapabilityRegistry that routes all tool calls through `docker exec`.

    `container` must be a docker SDK `Container` (e.g., from a
    `terminal_bench.terminal.tmux_session.TmuxSession.container`).

    Tools provided (parallel to the tmux registry):
      bash         — `docker exec` of a shell command
      read         — `docker exec cat <path>`
      list         — `docker exec ls -la <path>`
      write        — base64-encoded `echo | base64 -d > <path>` in-container
      str_replace  — temp Python script via base64, exec'd in-container

    Reliability win vs tmux: no `clear` + `capture_pane` + sentinel-regex
    round-trip per call; exit codes come directly from `exec_run`.
    """

    def _exec(cmd_argv: list[str], workdir: str = default_workdir) -> tuple[int, str]:
        """Run a docker exec; return (exit_code, combined_output_str)."""
        try:
            res = container.exec_run(
                cmd=cmd_argv,
                workdir=workdir,
                demux=False,
                stdout=True,
                stderr=True,
            )
            output = ""
            if res.output is not None:
                output = res.output.decode("utf-8", errors="replace") \
                    if isinstance(res.output, (bytes, bytearray)) else str(res.output)
            return int(res.exit_code or 0), output
        except Exception as exc:  # noqa: BLE001
            return -1, f"docker exec error: {type(exc).__name__}: {exc}"

    def _bash(args: dict, state: State) -> ToolResult:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return ToolResult(tool="bash", is_error=True, output="empty command")
        timeout_s = float(args.get("timeout_s", 60.0))
        # bash -lc = login shell → sources /etc/profile + ~/.bashrc so PATH
        # includes the container's conda env (testbed) and pytest, python, etc.
        # are findable. Without this, docker exec inherits only ENV vars set
        # at container build time, which can miss interpreter PATHs (sphinx,
        # some sympy envs). See PRD 1.6.6 follow-up note.
        wrapped = f"timeout {int(timeout_s)} bash -lc {shlex.quote(cmd)}"
        exit_code, output = _exec(["sh", "-c", wrapped])
        return ToolResult(
            tool="bash",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "command": cmd},
        )

    def _read(args: dict, state: State) -> ToolResult:
        """Read a file with offset/limit + Pi-style truncation message.

        1.6.6.14 — added offset/limit support and a truncation notice that
        tells the LLM both how much it got and how to continue (Pi pattern:
        "showing X of Y lines; use offset to continue"). Addresses the
        cost-per-PASS finding that bare reads dominate output bytes (95% of
        tool output volume in v1.7-tools).
        """
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="read", is_error=True, output="missing 'path'")
        offset = args.get("offset")  # 1-indexed line
        limit = args.get("limit")    # max lines to return
        # Default cap: 2000 lines OR 50KB, whichever hits first (Pi defaults)
        MAX_LINES = 2000
        MAX_BYTES = 50 * 1024

        exit_code, output = _exec(["cat", path])
        if exit_code != 0:
            return ToolResult(tool="read", is_error=True, output=output[:OUTPUT_CAP_BYTES])

        lines = output.split("\n")
        # cat appends a trailing newline; drop the empty tail if so
        if lines and lines[-1] == "":
            lines.pop()
        total_lines = len(lines)

        try:
            start = max(0, (int(offset) - 1)) if offset is not None else 0
        except (TypeError, ValueError):
            start = 0
        try:
            n_lim = int(limit) if limit is not None else MAX_LINES
        except (TypeError, ValueError):
            n_lim = MAX_LINES
        n_lim = max(0, min(n_lim, MAX_LINES))
        end_req = min(total_lines, start + n_lim)

        selected = lines[start:end_req]
        sliced = "\n".join(selected)

        # Byte cap can fire before line cap on dense lines
        truncated_by_bytes = False
        if len(sliced.encode("utf-8", errors="replace")) > MAX_BYTES:
            # Walk back from MAX_BYTES to the previous newline
            head = sliced.encode("utf-8", errors="replace")[:MAX_BYTES]
            sliced = head.decode("utf-8", errors="replace")
            cut = sliced.rfind("\n")
            if cut > 0:
                sliced = sliced[:cut]
            selected = sliced.split("\n")
            truncated_by_bytes = True

        n_shown = len(selected)
        end_shown = start + n_shown

        # Build the truncation/range notice — designed to nudge narrow reads.
        notice = ""
        if total_lines == 0:
            notice = ""  # empty file
        elif start == 0 and end_shown == total_lines and not truncated_by_bytes:
            notice = ""  # full file fit
        elif truncated_by_bytes:
            notice = (
                f"\n[Truncated: lines {start + 1}-{end_shown} of {total_lines} "
                f"({MAX_BYTES // 1024}KB byte limit hit). "
                f"Pass offset={end_shown + 1} to continue, or use limit to read a smaller range.]"
            )
        elif n_shown >= MAX_LINES and total_lines > end_shown:
            notice = (
                f"\n[Truncated: showing lines {start + 1}-{end_shown} of {total_lines} "
                f"({MAX_LINES} line limit). Pass offset={end_shown + 1} to continue.]"
            )
        elif start > 0 or end_shown < total_lines:
            notice = (
                f"\n[Showing lines {start + 1}-{end_shown} of {total_lines}. "
                f"Use offset/limit to view other ranges.]"
            )

        return ToolResult(
            tool="read",
            is_error=False,
            output=sliced + notice,
            detail={
                "total_lines": total_lines,
                "shown_lines": n_shown,
                "offset": start + 1,
                "truncated_by_bytes": truncated_by_bytes,
            },
        )

    def _list(args: dict, state: State) -> ToolResult:
        path = str(args.get("path", "."))
        exit_code, output = _exec(["ls", "-la", path])
        return ToolResult(
            tool="list",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
        )

    def _write(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        content = args.get("content", "")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="write", is_error=True, output="missing 'path'")
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = str(Path(path).parent) or "."
        # mkdir then write — single shell command to keep it atomic.
        cmd = f"mkdir -p {shlex.quote(parent)} && echo {b64} | base64 -d > {shlex.quote(path)}"
        exit_code, output = _exec(["sh", "-c", cmd])
        if exit_code != 0:
            return ToolResult(tool="write", is_error=True, output=output[:OUTPUT_CAP_BYTES])
        return ToolResult(
            tool="write",
            is_error=False,
            output=f"wrote {len(content)} chars to {path}",
            detail={"path": path, "bytes": len(content.encode('utf-8'))},
        )

    def _str_replace(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="str_replace", is_error=True, output="missing 'path'")
        if not old_str:
            return ToolResult(tool="str_replace", is_error=True, output="missing 'old_str'")
        # Write a small Python script into /tmp via base64, then exec it.
        # Avoids fragile shell quoting on arbitrary old_str / new_str.
        old_b64 = base64.b64encode(old_str.encode("utf-8")).decode("ascii")
        new_b64 = base64.b64encode(new_str.encode("utf-8")).decode("ascii")
        script_text = (
            "import base64, sys\n"
            f"old = base64.b64decode('{old_b64}').decode('utf-8')\n"
            f"new = base64.b64decode('{new_b64}').decode('utf-8')\n"
            f"path = {path!r}\n"
            "try:\n"
            "    content = open(path).read()\n"
            "except FileNotFoundError:\n"
            "    print(f'ERROR: file not found: {path}'); sys.exit(2)\n"
            "n = content.count(old)\n"
            "if n == 0:\n"
            "    print('ERROR: old_str not found in file'); sys.exit(2)\n"
            "if n > 1:\n"
            "    print(f'ERROR: {n} matches — make old_str more specific'); sys.exit(3)\n"
            "open(path, 'w').write(content.replace(old, new, 1))\n"
            f"print(f'OK: replaced 1 occurrence in {path}')\n"
        )
        script_b64 = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/cat_edit_{uuid.uuid4().hex[:8]}.py"
        # Stage the script then execute, all in one shell call.
        stage_and_run = (
            f"echo {script_b64} | base64 -d > {tmp} && python3 {tmp}; rc=$?; rm -f {tmp}; exit $rc"
        )
        exit_code, output = _exec(["sh", "-c", stage_and_run])
        return ToolResult(
            tool="str_replace",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "path": path},
        )

    def _rg(args: dict, state: State) -> ToolResult:
        """ripgrep-flavored search; falls back to `grep -rn` when rg unavailable.

        1.6.6.2 — sharper search primitive than `bash("grep ...")`. Same
        interface whether rg is installed or not; the wrapper picks at
        invoke-time. On large codebases (Django, sympy) ripgrep is 10-100×
        faster than grep.

        1.6.6.10 — `context: int` adds -C N (both rg and grep), so callers can
        ask for N lines of surrounding context in one call instead of grep
        then read.
        """
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(tool="rg", is_error=True, output="missing 'pattern'")
        paths = args.get("paths") or "."
        if isinstance(paths, list):
            paths_str = " ".join(shlex.quote(str(p)) for p in paths)
        else:
            paths_str = shlex.quote(str(paths))
        flags = str(args.get("flags") or "")
        ctx = args.get("context")
        if isinstance(ctx, (int, float)) and int(ctx) > 0:
            # -C is supported by both rg and POSIX grep, so a single flag works
            # for the fallback path too.
            flags = (flags + f" -C {int(ctx)}").strip()
        rg_cmd = f"rg {flags} -- {shlex.quote(pattern)} {paths_str}"
        grep_cmd = f"grep -rn {flags} -- {shlex.quote(pattern)} {paths_str}"
        sh = f"if command -v rg >/dev/null 2>&1; then {rg_cmd}; else {grep_cmd}; fi"
        exit_code, output = _exec(["bash", "-lc", sh])
        # grep returns 1 on "no matches"; treat that as not-an-error
        is_err = (exit_code not in (0, 1))
        return ToolResult(
            tool="rg",
            is_error=is_err,
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "pattern": pattern, "paths": paths},
        )

    def _apply_patch(args: dict, state: State) -> ToolResult:
        """Apply a unified-diff patch via `git apply --reject`.

        1.6.6.7 — closes the django-13315-style gap where the agent's plan said
        "apply this patch" but it had to hand-craft a heredoc + git apply call.
        Hermes-style: patch is a first-class tool, takes a unified-diff blob.

        --reject keeps partial application: hunks that don't match get written
        to .rej files instead of aborting the whole patch. The agent can then
        read the .rej and fix the surviving hunks. --whitespace=fix
        absorbs trailing-whitespace mismatches which kill ~10% of diffs.
        """
        patch = args.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return ToolResult(tool="apply_patch", is_error=True, output="missing 'patch' (unified diff)")
        workdir = str(args.get("workdir") or default_workdir)
        # Stage the diff via base64 to avoid quoting/heredoc fragility.
        b64 = base64.b64encode(patch.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/cat_patch_{uuid.uuid4().hex[:8]}.diff"
        # Write patch → git apply --reject → capture .rej files → exit
        sh = (
            f"echo {b64} | base64 -d > {tmp} && "
            f"cd {shlex.quote(workdir)} && "
            f"git apply --reject --whitespace=fix {tmp}; rc=$?; "
            # List any .rej files so the agent can see partial application.
            f"echo '---REJECTS---'; "
            f"find . -name '*.rej' -newer {tmp} 2>/dev/null | head -20; "
            f"rm -f {tmp}; exit $rc"
        )
        exit_code, output = _exec(["bash", "-lc", sh])
        # exit 0 = all hunks applied; nonzero = some rejected (still useful)
        return ToolResult(
            tool="apply_patch",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "workdir": workdir},
        )

    def _multi_edit(args: dict, state: State) -> ToolResult:
        """Apply N (old → new) replacements atomically against the original file.

        1.6.6.8 — Pi-style multi-block edit. Each {old_text, new_text} matches
        against the ORIGINAL file contents, not incrementally. This means:
          • edits can be in any order
          • line numbers don't drift between hunks
          • each old_text must be unique in the original (or replace_all)
        If any edit fails to match (0 or >1 hits), the WHOLE call is aborted —
        no partial writes. Cuts N-call cost of N-hunk fixes to 1 call.
        """
        path = args.get("path")
        edits = args.get("edits")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="multi_edit", is_error=True, output="missing 'path'")
        if not isinstance(edits, list) or not edits:
            return ToolResult(tool="multi_edit", is_error=True,
                              output="missing 'edits' (list of {old_text, new_text})")
        # Encode the whole edits list once via base64-JSON, applied by a small
        # Python script in-container. Avoids per-edit shell quoting.
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
        exit_code, output = _exec(["sh", "-c", stage_and_run])
        return ToolResult(
            tool="multi_edit",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "path": path, "n_edits": len(edits)},
        )

    def _git_status(args: dict, state: State) -> ToolResult:
        """`git status -sb` inside the workdir. 1.6.6.9 — first-class git
        orientation so the agent doesn't have to craft bash for it."""
        workdir = str(args.get("workdir") or default_workdir)
        sh = f"cd {shlex.quote(workdir)} && git status -sb"
        exit_code, output = _exec(["bash", "-lc", sh])
        return ToolResult(
            tool="git_status",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "workdir": workdir},
        )

    def _git_diff(args: dict, state: State) -> ToolResult:
        """`git diff [--staged] [path]` inside the workdir. 1.6.6.9 — lets
        the agent see its accumulated changes between iterations as a tool
        result (which the trace records), rather than buried in bash."""
        workdir = str(args.get("workdir") or default_workdir)
        path = args.get("path")
        staged = bool(args.get("staged", False))
        parts = ["git diff"]
        if staged:
            parts.append("--staged")
        if isinstance(path, str) and path:
            parts.append("--")
            parts.append(shlex.quote(path))
        sh = f"cd {shlex.quote(workdir)} && {' '.join(parts)}"
        exit_code, output = _exec(["bash", "-lc", sh])
        return ToolResult(
            tool="git_diff",
            is_error=(exit_code != 0),
            output=_truncate(output, READ_CAP_BYTES),  # diffs can be large
            detail={"returncode": exit_code, "workdir": workdir, "path": path, "staged": staged},
        )

    def _symbol_nav(action: str, args: dict, state: State, tool_name: str) -> ToolResult:
        """Shared dispatcher for goto_def / find_refs / outline (1.7.0).

        Stages a small jedi-backed script into the container via base64, then
        runs it. Lazy-installs jedi if not present (pip --user). Returns the
        script's JSON output as the tool output.
        """
        root = str(args.get("root") or default_workdir)
        payload = {"action": action, "root": root, **args}
        payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        script_text = (
            "import base64, json, os, subprocess, sys\n"
            f"args = json.loads(base64.b64decode('{payload_b64}').decode('utf-8'))\n"
            "try:\n"
            "    import jedi\n"
            "except ImportError:\n"
            "    subprocess.check_call([sys.executable, '-m', 'pip', 'install',\n"
            "        '--quiet', '--disable-pip-version-check', '--user', 'jedi>=0.19'])\n"
            "    sys.path.insert(0, os.path.expanduser('~/.local/lib/python%d.%d/site-packages' % sys.version_info[:2]))\n"
            "    import jedi\n"
            "root = args['root']\n"
            "project = jedi.Project(root)\n"
            "action = args['action']\n"
            "out = []\n"
            "def _to_row(n):\n"
            "    p = str(n.module_path) if n.module_path else ''\n"
            "    return {'path': p, 'line': n.line, 'col': n.column, 'type': n.type, 'name': n.name}\n"
            "if action == 'goto_def':\n"
            "    name = args['name']\n"
            "    seen = set()\n"
            "    for hit in list(project.search(name))[:30]:\n"
            "        try:\n"
            "            for d in hit.goto(follow_imports=True):\n"
            "                row = _to_row(d)\n"
            "                key = (row['path'], row['line'], row['name'])\n"
            "                if key in seen: continue\n"
            "                seen.add(key)\n"
            "                if row['name'] == name or row['name'].endswith('.' + name):\n"
            "                    out.append(row)\n"
            "        except Exception as e:\n"
            "            continue\n"
            "    out = out[:20]\n"
            "elif action == 'find_refs':\n"
            "    name = args['name']\n"
            "    defs = []\n"
            "    for hit in list(project.search(name))[:10]:\n"
            "        if hit.name == name:\n"
            "            try:\n"
            "                defs.extend(hit.goto(follow_imports=True))\n"
            "            except Exception:\n"
            "                pass\n"
            "    if not defs:\n"
            "        print(json.dumps({'error': f'no definition found for {name!r}'})); sys.exit(0)\n"
            "    d = defs[0]\n"
            "    if not d.module_path:\n"
            "        print(json.dumps({'error': 'definition has no module_path'})); sys.exit(0)\n"
            "    script = jedi.Script(path=str(d.module_path), project=project)\n"
            "    refs = script.get_references(d.line, d.column, scope='project')\n"
            "    for r in refs[:50]:\n"
            "        row = _to_row(r)\n"
            "        try:\n"
            "            lines = open(row['path']).read().splitlines()\n"
            "            row['snippet'] = lines[row['line']-1].strip()[:120] if row['line'] <= len(lines) else ''\n"
            "        except Exception:\n"
            "            row['snippet'] = ''\n"
            "        out.append(row)\n"
            "elif action == 'outline':\n"
            "    path = args['path']\n"
            "    if not os.path.isabs(path):\n"
            "        path = os.path.join(root, path)\n"
            "    script = jedi.Script(path=path, project=project)\n"
            "    names = script.get_names(all_scopes=False, definitions=True, references=False)\n"
            "    for n in names:\n"
            "        out.append({'name': n.name, 'type': n.type, 'line': n.line})\n"
            "print(json.dumps(out, indent=2))\n"
        )
        script_b64 = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/cat_symnav_{uuid.uuid4().hex[:8]}.py"
        sh = (
            f"echo {script_b64} | base64 -d > {tmp} && "
            f"python3 {tmp}; rc=$?; rm -f {tmp}; exit $rc"
        )
        exit_code, output = _exec(["bash", "-lc", sh])
        return ToolResult(
            tool=tool_name,
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "action": action},
        )

    def _goto_def(args: dict, state: State) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(tool="goto_def", is_error=True, output="missing 'name'")
        return _symbol_nav("goto_def", args, state, "goto_def")

    def _find_refs(args: dict, state: State) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(tool="find_refs", is_error=True, output="missing 'name'")
        return _symbol_nav("find_refs", args, state, "find_refs")

    def _outline(args: dict, state: State) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(tool="outline", is_error=True, output="missing 'path'")
        return _symbol_nav("outline", args, state, "outline")

    def _fd(args: dict, state: State) -> ToolResult:
        """fd-flavored file finder; falls back to `find` when fd unavailable."""
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(tool="fd", is_error=True, output="missing 'pattern'")
        paths = args.get("paths") or "."
        if isinstance(paths, list):
            paths_str = " ".join(shlex.quote(str(p)) for p in paths)
        else:
            paths_str = shlex.quote(str(paths))
        flags = str(args.get("flags") or "")
        fd_cmd = f"fd {flags} -- {shlex.quote(pattern)} {paths_str}"
        # find's syntax differs; use -iname for a sensible default mapping
        find_cmd = (
            f"find {paths_str} -iname {shlex.quote(pattern if any(c in pattern for c in '*?[') else '*' + pattern + '*')}"
            + (f" {flags}" if flags else "")
        )
        sh = f"if command -v fd >/dev/null 2>&1; then {fd_cmd}; else {find_cmd}; fi"
        exit_code, output = _exec(["bash", "-lc", sh])
        return ToolResult(
            tool="fd",
            is_error=(exit_code != 0),
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={"returncode": exit_code, "pattern": pattern, "paths": paths},
        )

    reg = CapabilityRegistry()
    reg.register(Capability(
        name="bash",
        description=(
            "Run a shell command inside the container via docker exec. "
            "Returns stdout+stderr; is_error=true on non-zero exit. "
            "Use this for grep/find/sed/python/pytest/etc. "
            "The command is wrapped in a `timeout` so it cannot hang past timeout_s."
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
        name="read",
        description=(
            "Read a file from inside the container. Default cap: 2000 lines or 50KB "
            "(whichever hits first). "
            "Use `offset` (1-indexed line number) and `limit` (max lines) for narrow reads — "
            "STRONGLY RECOMMENDED after `goto_def` or `find_refs` returns a location: "
            "e.g. `read(path, offset=line-5, limit=50)` instead of bare `read(path)`. "
            "When you need the full file, continue with offset until complete. "
            "The truncation message tells you how many lines exist and where to resume."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "absolute path inside the container"},
                "offset": {"type": "integer",
                           "description": "1-indexed line number to start reading from (default 1)"},
                "limit":  {"type": "integer",
                           "description": "max lines to return (default 2000)"},
            },
            "required": ["path"],
        },
        handler=_read,
    ))
    reg.register(Capability(
        name="list",
        description="List directory contents inside the container (ls -la).",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "path to list (default '.')"},
            },
        },
        handler=_list,
    ))
    reg.register(Capability(
        name="write",
        description="Write content to a file inside the container (creates parent dirs, overwrites existing).",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path inside the container"},
                "content": {"type": "string", "description": "full file contents (UTF-8)"},
            },
            "required": ["path", "content"],
        },
        handler=_write,
    ))
    reg.register(Capability(
        name="str_replace",
        description=(
            "Edit an existing file inside the container by replacing an exact string. "
            "old_str must match exactly once — make it specific enough (include surrounding lines). "
            "Prefer this over `write` for patching existing files."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path inside the container"},
                "old_str": {"type": "string", "description": "exact string to find (must appear exactly once)"},
                "new_str": {"type": "string", "description": "replacement string (empty string to delete)"},
            },
            "required": ["path", "old_str"],
        },
        handler=_str_replace,
    ))
    # 1.6.6.2 — first-class ripgrep + fd (with grep/find fallbacks)
    reg.register(Capability(
        name="rg",
        description=(
            "Recursive search via ripgrep (falls back to `grep -rn` if rg is not installed). "
            "Prefer this over `bash(\"grep ...\")` for code search — much faster on large codebases. "
            "Returns matches with line numbers."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "regex/pattern to search for"},
                "paths":   {"type": ["string", "array"],
                            "description": "path or list of paths to search (default '.')"},
                "flags":   {"type": "string", "description": "extra flags, e.g. '-i' for case-insensitive"},
                "context": {"type": "integer",
                            "description": "lines of surrounding context per match (-C N). Default 0."},
            },
            "required": ["pattern"],
        },
        handler=_rg,
    ))
    reg.register(Capability(
        name="fd",
        description=(
            "Find files by name via fd (falls back to `find -iname` if fd is not installed). "
            "Prefer this over `bash(\"find ...\")`. "
            "Pattern can be a substring (auto-globbed) or shell glob (*, ?, [...])."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "name pattern (substring or glob)"},
                "paths":   {"type": ["string", "array"],
                            "description": "path or list of paths to search (default '.')"},
                "flags":   {"type": "string", "description": "extra flags (fd or find compatible)"},
            },
            "required": ["pattern"],
        },
        handler=_fd,
    ))
    # 1.6.6.7 — apply_patch (Hermes-style first-class patch tool)
    reg.register(Capability(
        name="apply_patch",
        description=(
            "Apply a unified-diff patch in the workdir via `git apply --reject --whitespace=fix`. "
            "Use this for multi-hunk edits instead of crafting a heredoc + bash. "
            "Hunks that don't match are written to .rej files (partial apply); "
            "the tool result lists any .rej files so you can fix and retry."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "patch":   {"type": "string", "description": "unified-diff text (the contents of a .patch file)"},
                "workdir": {"type": "string", "description": f"directory to apply in (default {default_workdir})"},
            },
            "required": ["patch"],
        },
        handler=_apply_patch,
    ))
    # 1.6.6.8 — multi_edit (Pi-style atomic multi-block edit)
    reg.register(Capability(
        name="multi_edit",
        description=(
            "Apply N (old_text → new_text) replacements to one file atomically. "
            "Each old_text matches against the ORIGINAL file (not after prior edits), "
            "so order doesn't matter and line numbers can't drift. "
            "Every old_text must appear exactly once in the original; otherwise the whole call aborts. "
            "Prefer this over multiple `str_replace` calls when fixing several hunks in one file."
        ),
        json_schema={
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
        },
        handler=_multi_edit,
    ))
    # 1.6.6.9 — git_status / git_diff first-class
    reg.register(Capability(
        name="git_status",
        description=(
            "Show repo status (git status -sb) for the workdir. "
            "Use between iterations to see which files you've touched."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "workdir": {"type": "string", "description": f"directory (default {default_workdir})"},
            },
        },
        handler=_git_status,
    ))
    reg.register(Capability(
        name="git_diff",
        description=(
            "Show accumulated diff (git diff [--staged] [path]) for the workdir. "
            "Use to inspect your own edits as a single coherent change. "
            "Output capped at 64KB."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "workdir": {"type": "string", "description": f"directory (default {default_workdir})"},
                "path":    {"type": "string", "description": "limit diff to a path (optional)"},
                "staged":  {"type": "boolean", "description": "use --staged (default false)"},
            },
        },
        handler=_git_diff,
    ))
    # 1.7.0 — symbol_nav: structural code navigation via jedi
    reg.register(Capability(
        name="goto_def",
        description=(
            "Find where a Python symbol is defined (class, function, variable). "
            "Returns up to 20 {path, line, col, type, name} hits. "
            "Prefer this over `rg(\"def foo|class foo\")` — works through imports and "
            "respects Python scoping."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "symbol name to locate (function, class, variable)"},
                "root": {"type": "string", "description": f"project root for the search (default {default_workdir})"},
            },
            "required": ["name"],
        },
        handler=_goto_def,
    ))
    reg.register(Capability(
        name="find_refs",
        description=(
            "Find all references to a Python symbol across the project. "
            "Returns up to 50 {path, line, col, snippet} hits. "
            "Prefer this over `rg(name)` for 'who calls X' / 'where is X used' questions — "
            "filters out string matches and respects Python scoping."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "symbol name to find references of"},
                "root": {"type": "string", "description": f"project root (default {default_workdir})"},
            },
            "required": ["name"],
        },
        handler=_find_refs,
    ))
    reg.register(Capability(
        name="outline",
        description=(
            "List top-level classes, functions, and module-level names in a Python file "
            "(structural outline). Returns {name, type, line} per entry. "
            "Prefer this over `read` when you only need to know what's in a file."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "file path (relative to root or absolute)"},
                "root": {"type": "string", "description": f"project root (default {default_workdir})"},
            },
            "required": ["path"],
        },
        handler=_outline,
    ))
    return reg
