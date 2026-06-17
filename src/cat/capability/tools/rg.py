"""rg tool — ripgrep-flavored search (docker-only, requires exec_fn injected via closure)."""
from __future__ import annotations

import shlex
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
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
}


def _ast_grep_fallback(pattern: str, paths, broader_path: str, exec_fn) -> str:
    """Structural fallback when rg finds 0 exact matches."""
    import re as _re

    name = None
    m = _re.search(r'\bdef\s+([a-zA-Z_]\w+)', pattern)
    if m:
        name = m.group(1)
        sg_pattern = f"def {name}($$$)"
    else:
        m = _re.search(r'\bclass\s+([a-zA-Z_]\w+)', pattern)
        if m:
            name = m.group(1)
            sg_pattern = f"class {name} {{$$$}}"
        else:
            words = _re.findall(r'[a-zA-Z_]\w{7,}', pattern)
            if words:
                name = max(words, key=len)
                sg_pattern = f"{name}($$$)"

    if not name:
        return (
            f"CONCLUSION: pattern not found. No distinctive identifier "
            f"could be extracted for structural fallback.\n"
            f"Try a simpler search term.\n"
        )

    search_path = str(paths) if not isinstance(paths, list) else " ".join(str(p) for p in paths)
    if search_path == "." or not search_path:
        search_path = broader_path

    sg_cmd = (
        f"if command -v sg >/dev/null 2>&1; then "
        f"  sg run -p {shlex.quote(sg_pattern)} --lang python {shlex.quote(search_path)} 2>/dev/null | head -80; "
        f"elif pip show ast-grep-cli >/dev/null 2>&1; then "
        f"  pip install -q ast-grep-cli 2>/dev/null && "
        f"  sg run -p {shlex.quote(sg_pattern)} --lang python {shlex.quote(search_path)} 2>/dev/null | head -80; "
        f"else "
        f"  echo 'ast-grep not available'; "
        f"fi"
    )
    _, sg_out = exec_fn(["bash", "-lc", sg_cmd])
    sg_out = (sg_out or "").strip()

    if sg_out and "ast-grep not available" not in sg_out and len(sg_out) > 10:
        return (
            f"STRUCTURAL FALLBACK (ast-grep pattern: {sg_pattern!r}):\n"
            f"Exact text not found — showing structurally matching code:\n\n"
            f"{sg_out[:3000]}\n\n"
            f"HINT: Copy exact text from above for old_str. "
            f"Do not use the original search pattern as old_str.\n"
        )

    rg_name_cmd = f"rg -n -C 5 -- {shlex.quote(name)} {shlex.quote(search_path)} 2>/dev/null | head -60"
    _, rg_out = exec_fn(["bash", "-lc", rg_name_cmd])
    rg_out = (rg_out or "").strip()

    if rg_out:
        return (
            f"IDENTIFIER FALLBACK (searched for {name!r}):\n"
            f"Exact pattern not found — showing occurrences of '{name}':\n\n"
            f"{rg_out[:2000]}\n\n"
            f"HINT: Read the file at the shown line numbers to get exact text for old_str.\n"
        )

    return (
        f"CONCLUSION: pattern {pattern!r} not found. "
        f"Tried structural fallback for identifier {name!r} — also no matches.\n"
        f"Check the file path or try a broader search.\n"
    )


def create_rg_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the rg capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(tool="rg", is_error=True, output="missing 'pattern'")
        paths = args.get("paths") or args.get("path") or "."
        flags = str(args.get("flags") or "")
        ctx = args.get("context")
        default_ctx = 10
        if isinstance(ctx, (int, float)):
            ctx_lines = max(0, int(ctx))
        else:
            ctx_lines = default_ctx
        if ctx_lines > 0 and "-C" not in flags and "-A" not in flags and "-B" not in flags:
            flags = (flags + f" -C {ctx_lines}").strip()

        def _do_search(p) -> tuple[int, str, int]:
            if isinstance(p, list):
                p_str = " ".join(shlex.quote(str(x)) for x in p)
            else:
                p_str = shlex.quote(str(p))
            rg_cmd = f"rg {flags} -- {shlex.quote(pattern)} {p_str}"
            grep_cmd = f"grep -rn {flags} -- {shlex.quote(pattern)} {p_str}"
            sh = f"if command -v rg >/dev/null 2>&1; then {rg_cmd}; else {grep_cmd}; fi"
            code, out = exec_fn(["bash", "-lc", sh])
            n = 0
            for ln in (out or "").splitlines():
                if not ln.strip() or ln.startswith("--"):
                    continue
                if ":" in ln and not ln.startswith("grep:") and not ln.lower().startswith("warning"):
                    n += 1
            return code, out, n

        code1, out1, n1 = _do_search(paths)

        def _is_narrow(p) -> bool:
            if isinstance(p, list):
                return all(_is_narrow(x) for x in p) if p else False
            s = str(p)
            return s.endswith(".py") or s.endswith(".pyi") or ("/testbed/" in s and s != "/testbed")

        broader_path = "/testbed"
        do_fallback = (
            n1 == 0
            and _is_narrow(paths)
            and (str(paths) != broader_path)
        )
        if do_fallback:
            _, out2, n2 = _do_search(broader_path)
            output = (
                f"=== rg result for pattern {pattern!r} ===\n"
                f"PRIMARY:  0 matches in {paths!r}\n"
                f"FALLBACK: {n2} matches in {broader_path!r}\n\n"
                f"--- fallback excerpts (truncated) ---\n"
                f"{(out2 or '').strip()[:4000]}\n\n"
                f"CONCLUSION: pattern {pattern!r} is NOT in the requested "
                f"location {paths!r}; "
                f"{'found ' + str(n2) + ' match(es) in broader scope (see above)' if n2 else 'not found anywhere in ' + broader_path}.\n"
            )
            is_err = False
        elif n1 == 0:
            sg_output = _ast_grep_fallback(pattern, paths, broader_path, exec_fn)
            output = (
                f"=== rg result for pattern {pattern!r} ===\n"
                f"PRIMARY: 0 matches in {paths!r}\n\n"
                f"{sg_output}"
            )
            is_err = (code1 not in (0, 1))
        else:
            raw = (out1 or "").strip()
            if len(raw.encode("utf-8")) > 3000:
                raw = raw.encode("utf-8")[:3000].decode("utf-8", errors="replace")
                raw += f"\n... [rg output truncated at 3000 bytes — use read(offset=N) for full context]"
            output = (
                f"=== rg result for pattern {pattern!r} ===\n"
                f"PRIMARY: {n1} match(es) in {paths!r}\n\n"
                f"{raw}\n"
            )
            is_err = (code1 not in (0, 1))

        return ToolResult(
            tool="rg",
            is_error=is_err,
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={
                "returncode": code1, "pattern": pattern, "paths": paths,
                "n_primary_matches": n1, "fallback_used": do_fallback,
            },
        )

    return Capability(
        name="rg",
        description=(
            "Search for a pattern in code. Returns matching lines WITH surrounding context (±10 lines). "
            "Use this to locate a function/class before editing — get the line number, "
            "then use read(path, offset=N, limit=40) to get exact text for str_replace. "
            "Prefer over bash(grep) — automatically widens search if exact pattern not found."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
