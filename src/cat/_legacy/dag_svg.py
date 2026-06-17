"""Render a RunDAG as inline SVG for the HTML report (1.6.3).

Layout:
  - BFS depth from root = row
  - within-row position = column (sibling index)
  - longest-chain nodes get thicker stroke (the "spine")
  - colors by CAT layer (represent / decompose / activate / invoke / verify /
    lifecycle / llm)
  - hovering a node shows its kind + label as a native SVG <title> tooltip

No external dependencies. Pure Python string templating; the produced SVG is
self-contained and inlines into any HTML.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Optional

from cat.dag import Node, RunDAG


# ── Style constants ────────────────────────────────────────────────────────


LAYER_COLOR: dict[str, str] = {
    "lifecycle":  "#d1d5db",   # gray
    "represent":  "#93c5fd",   # blue
    "stipulate":  "#86efac",   # green (was: decompose)
    "activate":   "#fcd34d",   # amber
    "perform":    "#fdba74",   # orange (was: invoke)
    "verify":     "#f87171",   # red default; override green for OK
    "llm":        "#c4b5fd",   # purple
    "other":     "#e5e5e5",
    # Backward-compat for old traces:
    "decompose":  "#86efac",
    "invoke":     "#fdba74",
}

NODE_W = 150
NODE_H = 34
COL_GAP = 22
ROW_GAP = 18


@dataclass
class _Layout:
    positions: dict[str, tuple[int, int]]   # event_id → (x, y)
    width: int
    height: int


def _compute_layout(dag: RunDAG) -> _Layout:
    """BFS-assign depth → row, sibling index → column. Returns positions + dims."""
    # 1. BFS to compute depths
    depths: dict[str, int] = {}
    frontier = list(dag.roots)
    cur_depth = 0
    while frontier:
        next_frontier: list[str] = []
        for ev_id in frontier:
            if ev_id in depths:
                continue
            depths[ev_id] = cur_depth
            n = dag.nodes.get(ev_id)
            if n:
                next_frontier.extend(n.children_ids)
        frontier = next_frontier
        cur_depth += 1
    # Catch any orphans (defensive — shouldn't happen with 1.6.1 discipline)
    for ev_id in dag.nodes:
        depths.setdefault(ev_id, 0)

    # 2. Group nodes by depth → assign within-row position
    by_depth: dict[int, list[str]] = {}
    for ev_id, d in depths.items():
        by_depth.setdefault(d, []).append(ev_id)
    # Stable sort: by ts within each depth so siblings are roughly chronological
    for d in by_depth:
        by_depth[d].sort(key=lambda eid: dag.nodes[eid].ts)

    # 3. Compute (x, y) for each node
    positions: dict[str, tuple[int, int]] = {}
    max_cols = max(len(ids) for ids in by_depth.values()) if by_depth else 1
    for d, ids in by_depth.items():
        # Center each row by left-padding when row is narrower than max
        left_pad = (max_cols - len(ids)) * (NODE_W + COL_GAP) // 2
        for i, ev_id in enumerate(ids):
            x = 20 + left_pad + i * (NODE_W + COL_GAP)
            y = 20 + d * (NODE_H + ROW_GAP)
            positions[ev_id] = (x, y)

    width = 40 + max_cols * (NODE_W + COL_GAP)
    height = 40 + (max(by_depth.keys()) + 1) * (NODE_H + ROW_GAP) if by_depth else 60
    return _Layout(positions=positions, width=width, height=height)


def _node_color(n: Node) -> str:
    """Layer color, with overrides for verify status."""
    if n.layer == "verify":
        p = n.payload
        if p.get("judge"):
            return "#34d399" if p.get("complete") else "#f87171"
        return "#34d399" if p.get("satisfied") else "#f87171"
    return LAYER_COLOR.get(n.layer, LAYER_COLOR["other"])


def render_dag_svg(dag: RunDAG) -> str:
    """Return one self-contained <svg>…</svg> string for embedding in HTML."""
    if not dag.nodes:
        return '<div style="color:#999;font-size:11px;">(empty trace)</div>'

    layout = _compute_layout(dag)
    spine_ids = {n.event_id for n in dag.longest_chain()}
    term = dag.terminal_state()
    term_id = term.event_id if term else None

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {layout.width} {layout.height}" '
        f'style="background:#fafafa;border:1px solid #e5e5e5;border-radius:4px;display:block;'
        f'max-width:100%;height:auto;">'
    )

    # ── Edges first (drawn behind nodes) ──
    for ev_id, n in dag.nodes.items():
        if n.parent_id and n.parent_id in layout.positions:
            x1, y1 = layout.positions[n.parent_id]
            x2, y2 = layout.positions[ev_id]
            sx, sy = x1 + NODE_W // 2, y1 + NODE_H
            ex, ey = x2 + NODE_W // 2, y2
            mid_y = (sy + ey) // 2
            stroke = "#94a3b8" if ev_id in spine_ids and n.parent_id in spine_ids else "#cbd5e1"
            width = 2 if ev_id in spine_ids and n.parent_id in spine_ids else 1
            # Cubic bezier curve
            parts.append(
                f'<path d="M{sx},{sy} C{sx},{mid_y} {ex},{mid_y} {ex},{ey}" '
                f'stroke="{stroke}" stroke-width="{width}" fill="none"/>'
            )

    # ── Nodes ──
    for ev_id, (x, y) in layout.positions.items():
        n = dag.nodes[ev_id]
        color = _node_color(n)
        on_spine = ev_id in spine_ids
        is_terminal = ev_id == term_id
        stroke = "#1f2937" if (on_spine or is_terminal) else "#94a3b8"
        stroke_w = 2 if is_terminal else (1.5 if on_spine else 0.8)
        label = html.escape(n.label())
        if len(label) > 22:
            label = label[:21] + "…"
        # Tooltip: <title> works natively in SVG
        tooltip = html.escape(f"{n.kind}  |  {n.label()}  |  id={n.event_id}")
        parts.append(f'<g transform="translate({x},{y})">')
        parts.append(f'  <title>{tooltip}</title>')
        parts.append(
            f'  <rect width="{NODE_W}" height="{NODE_H}" rx="6" '
            f'fill="{color}" stroke="{stroke}" stroke-width="{stroke_w}"/>'
        )
        # Layer label (small, top-left)
        parts.append(
            f'  <text x="6" y="11" font-size="8" fill="#374151" '
            f'font-family="ui-monospace,monospace">{n.layer}</text>'
        )
        # Main label (center)
        parts.append(
            f'  <text x="{NODE_W // 2}" y="{NODE_H // 2 + 6}" font-size="11" '
            f'fill="#111827" font-family="ui-monospace,monospace" '
            f'text-anchor="middle" font-weight="{"600" if on_spine else "400"}">{label}</text>'
        )
        parts.append('</g>')

    # ── Legend ──
    legend_y = layout.height - 18
    parts.append(f'<g transform="translate(20,{legend_y})">')
    legend_items = [
        ("represent", LAYER_COLOR["represent"]),
        ("stipulate", LAYER_COLOR["stipulate"]),
        ("activate",  LAYER_COLOR["activate"]),
        ("perform",   LAYER_COLOR["perform"]),
        ("verify ✓",  "#34d399"),
        ("verify ✗",  "#f87171"),
        ("lifecycle", LAYER_COLOR["lifecycle"]),
        ("llm",       LAYER_COLOR["llm"]),
    ]
    for i, (name, color) in enumerate(legend_items):
        lx = i * 80
        parts.append(
            f'<rect x="{lx}" y="0" width="10" height="10" rx="2" fill="{color}" '
            f'stroke="#888" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{lx + 14}" y="9" font-size="9" font-family="ui-monospace,monospace" '
            f'fill="#555">{html.escape(name)}</text>'
        )
    parts.append('</g>')

    parts.append('</svg>')
    return '\n'.join(parts)
