"""fd tool — file finder (docker-only, requires exec_fn injected via closure)."""
from __future__ import annotations

import shlex
from typing import Callable

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State
from cat.capability.tools._helpers import _truncate, OUTPUT_CAP_BYTES

_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "name pattern (substring or glob)"},
        "paths":   {"type": ["string", "array"],
                    "description": "path or list of paths to search (default '.')"},
        "flags":   {"type": "string", "description": "extra flags (fd or find compatible)"},
    },
    "required": ["pattern"],
}


def create_fd_capability(exec_fn: Callable, default_workdir: str = "/testbed") -> Capability:
    """Create the fd capability bound to a docker exec function."""

    def _handle(args: dict, state: State) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(tool="fd", is_error=True, output="missing 'pattern'")
        paths = args.get("paths") or args.get("path") or "."
        flags = str(args.get("flags") or "")

        def _do_find(p) -> tuple[int, str, int]:
            if isinstance(p, list):
                p_str = " ".join(shlex.quote(str(x)) for x in p)
            else:
                p_str = shlex.quote(str(p))
            fd_cmd = f"fd {flags} -- {shlex.quote(pattern)} {p_str}"
            find_pat = pattern if any(c in pattern for c in '*?[') else '*' + pattern + '*'
            find_cmd = (
                f"find {p_str} -iname {shlex.quote(find_pat)}"
                + (f" {flags}" if flags else "")
            )
            sh = f"if command -v fd >/dev/null 2>&1; then {fd_cmd}; else {find_cmd}; fi"
            code, out = exec_fn(["bash", "-lc", sh])
            n = sum(1 for ln in (out or "").splitlines() if ln.strip())
            return code, out, n

        code1, out1, n1 = _do_find(paths)

        def _is_narrow(p) -> bool:
            if isinstance(p, list):
                return all(_is_narrow(x) for x in p) if p else False
            s = str(p)
            return s.endswith("/") is False and ("/testbed/" in s) and s != "/testbed"

        broader_path = "/testbed"
        do_fallback = n1 == 0 and _is_narrow(paths) and str(paths) != broader_path

        if do_fallback:
            _, out2, n2 = _do_find(broader_path)
            output = (
                f"=== fd result for pattern {pattern!r} ===\n"
                f"PRIMARY:  0 matches in {paths!r}\n"
                f"FALLBACK: {n2} matches in {broader_path!r}\n\n"
                f"--- fallback paths ---\n"
                f"{(out2 or '').strip()[:4000]}\n\n"
                f"CONCLUSION: no file matching {pattern!r} in the requested location {paths!r}; "
                f"{'found ' + str(n2) + ' in broader scope (see above)' if n2 else 'not found anywhere in ' + broader_path}.\n"
            )
            is_err = False
        elif n1 == 0:
            output = (
                f"=== fd result for pattern {pattern!r} ===\n"
                f"PRIMARY: 0 matches in {paths!r}\n\n"
                f"CONCLUSION: no file matching {pattern!r} in the searched location.\n"
            )
            is_err = (code1 != 0)
        else:
            output = (
                f"=== fd result for pattern {pattern!r} ===\n"
                f"PRIMARY: {n1} match(es) in {paths!r}\n\n"
                f"{(out1 or '').strip()}\n"
            )
            is_err = (code1 != 0)

        return ToolResult(
            tool="fd",
            is_error=is_err,
            output=_truncate(output, OUTPUT_CAP_BYTES),
            detail={
                "returncode": code1, "pattern": pattern, "paths": paths,
                "n_primary_matches": n1, "fallback_used": do_fallback,
            },
        )

    return Capability(
        name="fd",
        description=(
            "Find files by name via fd (falls back to `find -iname` if fd is not installed). "
            "Prefer this over `bash(\"find ...\")`. "
            "Pattern can be a substring (auto-globbed) or shell glob (*, ?, [...])."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )
