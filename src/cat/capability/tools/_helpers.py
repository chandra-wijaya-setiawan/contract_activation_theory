"""Shared helpers for tool implementations."""
from __future__ import annotations

from pathlib import Path

OUTPUT_CAP_BYTES = 8_000
READ_CAP_BYTES = 64_000


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
