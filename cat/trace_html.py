"""Render a CAT trace as a chronological "reasoning trace" HTML timeline.

Complements cat/dag_svg.py (which shows DAG structure). This view shows the
actual content at each step — what the agent thought, planned, did, observed.

Each event becomes a collapsible card with a layer-colored header and a body
formatted to its kind:
  - contract_state: from → to + reason
  - contract_represent: bulleted list of representations
  - contract_decompose: numbered plan steps
  - contract_admit: ok / grant / reason
  - capability_invoke / tool_call: tool name + args
  - tool_result: output (truncated, in <pre>)
  - criterion_check: oracle output OR judge reason
  - llm_usage: in/out tokens + role + wall_s

Returns a single self-contained HTML fragment (uses inline styles, no extra
CSS/JS dependencies).
"""
from __future__ import annotations

import html
import json
from typing import Any


LAYER_COLOR: dict[str, str] = {
    "lifecycle":  "#d1d5db",
    "represent":  "#93c5fd",
    "stipulate":  "#86efac",   # was: decompose
    "activate":   "#fcd34d",
    "perform":    "#fdba74",   # was: invoke
    "verify_ok":  "#34d399",
    "verify_no":  "#f87171",
    "llm":        "#c4b5fd",
    "other":     "#e5e5e5",
    # Backward-compat aliases for pre-rename traces:
    "decompose":  "#86efac",
    "invoke":     "#fdba74",
}


# Map event kind → layer (accepts both new and legacy kinds)
def _layer(kind: str, payload: dict) -> str:
    if kind == "contract_represent":
        return "represent"
    if kind in ("contract_stipulate", "contract_decompose"):
        return "stipulate"
    if kind == "contract_admit":
        return "activate"
    if kind in ("capability_perform", "capability_invoke", "tool_call", "tool_result"):
        return "perform"
    if kind == "criterion_check":
        ok = payload.get("complete") if payload.get("judge") else payload.get("satisfied")
        return "verify_ok" if ok else "verify_no"
    if kind == "llm_usage":
        return "llm"
    return "lifecycle"


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + f"… [+{len(s)-n} chars]"


def _format_body(kind: str, payload: dict) -> str:
    p = payload or {}
    esc = html.escape

    if kind == "contract_state":
        from_s = p.get("from") or "•"
        to_s = p.get("to") or "?"
        reason = p.get("reason") or ""
        obligation = p.get("obligation") or ""
        body = f'<div><b>{esc(from_s)}</b> → <b>{esc(to_s)}</b></div>'
        if reason:
            body += f'<div style="color:#555;margin-top:4px;">{esc(reason)}</div>'
        if obligation:
            body += (
                f'<div style="margin-top:6px;padding:6px 8px;background:#f3f4f6;'
                f'border-radius:4px;font-size:11px;"><b>Obligation:</b> {esc(_truncate(obligation, 600))}</div>'
            )
        return body

    if kind == "contract_represent":
        reps = p.get("representations", []) or []
        lines = ""
        for r in reps:
            k = esc(r.get("key", ""))
            v = esc(_truncate(str(r.get("value", "")), 120))
            lines += f'<div style="margin:2px 0;"><code>{k}</code>: {v}</div>'
        return f'<div style="font-size:11px;">{lines or "(no reps)"}</div>'

    if kind in ("contract_stipulate", "contract_decompose"):
        steps = p.get("steps", []) or []
        lines = ""
        for s in steps:
            sid = esc(s.get("id", ""))
            desc = esc(_truncate(s.get("description", ""), 300))
            lines += f'<div style="margin:4px 0;"><b>[{sid}]</b> {desc}</div>'
        return f'<div style="font-size:11px;">{lines or "(no plan)"}</div>'

    if kind == "contract_admit":
        ok = p.get("ok")
        body = f'<div><b>{"✓ admitted" if ok else "✗ rejected"}</b></div>'
        if not ok:
            body += f'<div style="color:#c0392b;margin-top:4px;">failure_type: {esc(str(p.get("failure_type")))}</div>'
        if p.get("reason"):
            body += f'<div style="color:#555;margin-top:4px;">{esc(p["reason"])}</div>'
        grant = p.get("grant", []) or []
        if grant:
            body += (
                f'<div style="margin-top:6px;font-size:11px;color:#555;">'
                f'<b>grant:</b> {esc(", ".join(grant))}</div>'
            )
        return body

    if kind in ("capability_perform", "capability_invoke"):
        tool = p.get("tool", "?")
        args = p.get("arguments", {}) or {}
        args_str = _format_args(args)
        return (
            f'<div><b>iter {p.get("iteration", "?")}</b> · '
            f'<code>{esc(tool)}</code></div>'
            f'<pre style="margin:6px 0 0 0;padding:6px;background:#f3f4f6;border-radius:4px;'
            f'font-size:11px;white-space:pre-wrap;overflow-x:auto;">{esc(args_str)}</pre>'
        )

    if kind == "tool_call":
        tool = p.get("tool", "?")
        args = p.get("args", {}) or {}
        args_str = _format_args(args)
        return (
            f'<div><code>{esc(tool)}</code></div>'
            f'<pre style="margin:6px 0 0 0;padding:6px;background:#f3f4f6;border-radius:4px;'
            f'font-size:11px;white-space:pre-wrap;overflow-x:auto;">{esc(args_str)}</pre>'
        )

    if kind == "tool_result":
        is_err = p.get("is_error")
        out = _truncate(p.get("output") or "", 1200)
        bg = "#fee2e2" if is_err else "#ecfdf5"
        return (
            f'<div><b>{"✗ error" if is_err else "✓ ok"}</b></div>'
            f'<pre style="margin:6px 0 0 0;padding:6px;background:{bg};border-radius:4px;'
            f'font-size:11px;white-space:pre-wrap;overflow-x:auto;max-height:240px;">{esc(out)}</pre>'
        )

    if kind == "criterion_check":
        if p.get("judge"):
            comp = p.get("complete")
            sid = esc(p.get("step_id", ""))
            reason = esc(_truncate(p.get("reason") or "", 400))
            return (
                f'<div><b>JUDGE</b> step <code>{sid}</code> · '
                f'{"✓ complete" if comp else "✗ incomplete"}</div>'
                f'<div style="color:#555;margin-top:4px;">{reason}</div>'
            )
        sat = p.get("satisfied")
        ev = esc(_truncate(p.get("evidence") or "", 400))
        return (
            f'<div><b>ORACLE</b> · {"✓ satisfied" if sat else "✗ failing"}</div>'
            f'<div style="color:#555;margin-top:4px;">{ev}</div>'
        )

    if kind == "llm_usage":
        role = esc(p.get("role", "?"))
        in_t = p.get("in", 0)
        out_t = p.get("out", 0)
        usd = p.get("usd", 0)
        wall = p.get("wall_s", 0)
        return (
            f'<div style="font-size:11px;">'
            f'<b>{role}</b> · in={in_t:,} out={out_t:,} · ${usd:.5f} · {wall:.1f}s'
            f'</div>'
        )

    if kind == "step_end":
        c = p.get("cost", {}) or {}
        return (
            f'<div><b>final_state: {esc(p.get("final_state", "?"))}</b> · '
            f'{p.get("iterations", 0)} iterations</div>'
            f'<div style="font-size:11px;color:#555;margin-top:4px;">'
            f'tokens={c.get("tokens_total", 0):,} · '
            f'tool_calls={c.get("tool_calls", 0)} · '
            f'llm_calls={c.get("llm_calls", 0)} · '
            f'wall={c.get("wall_s", 0):.1f}s · '
            f'usd=${c.get("usd", 0):.5f}'
            f'</div>'
        )

    # Fallback: dump payload as JSON
    return f'<pre style="font-size:11px;">{esc(_truncate(json.dumps(p, indent=2), 600))}</pre>'


def _format_args(args: dict[str, Any]) -> str:
    """Compact pretty-print for tool args. Path/command first, then rest."""
    if not args:
        return "{}"
    # Prefer concise rendering for common args
    if "command" in args:
        return f"command: {args['command']}\n" + "\n".join(
            f"{k}: {v}" for k, v in args.items() if k != "command"
        )
    if "path" in args:
        out = [f"path: {args['path']}"]
        for k, v in args.items():
            if k == "path":
                continue
            vs = str(v)
            if len(vs) > 200:
                vs = vs[:200] + f"… [+{len(vs)-200} chars]"
            out.append(f"{k}: {vs}")
        return "\n".join(out)
    return json.dumps(args, indent=2)


def render_trace_html(events: list[dict]) -> str:
    """Render a chronological list of trace events with content-rich bodies."""
    if not events:
        return '<div style="color:#999;font-size:11px;">(empty trace)</div>'

    parts: list[str] = [
        '<div style="display:flex;flex-direction:column;gap:4px;font-family:ui-monospace,monospace;">'
    ]
    for i, ev in enumerate(events):
        kind = ev.get("kind", "?")
        payload = ev.get("payload", {}) or {}
        layer = _layer(kind, payload)
        color = LAYER_COLOR.get(layer, LAYER_COLOR["other"])
        title = html.escape(kind)
        body = _format_body(kind, payload)
        ts = html.escape((ev.get("ts") or "")[:23])  # trim ms
        # Default-open the first few, collapsed beyond that
        open_attr = " open" if i < 6 else ""
        parts.append(
            f'<details{open_attr} style="border:1px solid #e5e5e5;border-left:6px solid {color};'
            f'border-radius:4px;background:white;">'
            f'<summary style="padding:6px 10px;cursor:pointer;font-size:11px;'
            f'display:flex;justify-content:space-between;align-items:center;">'
            f'<span><b style="color:#1f2937;">{title}</b> '
            f'<span style="color:#999;margin-left:8px;">{ts}</span></span>'
            f'<span style="color:#888;font-size:10px;">{i+1:03d}</span>'
            f'</summary>'
            f'<div style="padding:8px 12px;border-top:1px solid #f0f0f0;">{body}</div>'
            f'</details>'
        )
    parts.append('</div>')
    return "\n".join(parts)
