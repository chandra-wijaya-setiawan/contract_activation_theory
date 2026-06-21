"""HTML trace renderer — chronological "reasoning trace" timeline.

Returns a single self-contained HTML fragment (inline styles, no external deps).
"""
from __future__ import annotations

from typing import Any


def render_trace_html(events: list[dict[str, Any]]) -> str:
    """Render trace events as an HTML timeline."""
    ...
