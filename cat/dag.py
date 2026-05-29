"""RunDAG — typed reasoning DAG built from a trace JSONL.

After 1.6.1, every TraceEvent carries a valid `parent_id` (the immediate
causal parent) or `None` (the root state-init event). That means the trace
already describes a DAG; this module materializes it as a typed object that
the report layer and future cross-run learning code can query.

Usage:
    dag = RunDAG.from_jsonl("path/to/trace.jsonl")
    print(dag.longest_chain())             # list[Node]
    print(dag.branch_points())             # list[Node] with >1 children
    print(dag.path_to(event_id))           # ancestor chain back to root
    dag.write_json("path/to/dag.json")     # persist for offline analysis
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Node taxonomy ──────────────────────────────────────────────────────────


# One node per trace event. The `layer` tags which CAT step produced it.
# This is the field the HTML report colors by.
EVENT_TO_LAYER: dict[str, str] = {
    "contract_state":      "lifecycle",       # state transitions are the spine
    "contract_represent":  "represent",       # Step 2
    "contract_stipulate":  "stipulate",       # Step 3 (renamed from contract_decompose per PRD 1.6.2.9)
    "contract_admit":      "activate",        # Step 4
    "capability_perform":  "perform",         # Step 6 (renamed from capability_invoke)
    "tool_call":           "perform",         # belongs to current PERFORM
    "tool_result":         "perform",         # belongs to current PERFORM
    "criterion_check":     "verify",          # Step 7
    "llm_usage":           "llm",             # cuts across roles; secondary
    "step_end":            "lifecycle",       # transitions toward DISCHARGE
    "session_start":       "lifecycle",
    "session_end":         "lifecycle",
    "user_message":        "lifecycle",
    "assistant_message":   "perform",
    "thinking":            "perform",
    # Backward-compat: alias legacy event kinds for pre-rename traces (PRD 1.6.2.9 §6)
    "contract_decompose":  "stipulate",       # legacy alias
    "capability_invoke":   "perform",         # legacy alias
}


@dataclass
class Node:
    """One trace event, typed for DAG traversal."""
    event_id: str
    parent_id: Optional[str]
    kind: str                     # raw TraceEvent kind
    layer: str                    # mapped CAT layer (REPRESENT/DECOMPOSE/INVOKE/VERIFY/lifecycle/llm)
    ts: str
    payload: dict
    children_ids: list[str] = field(default_factory=list)   # back-pointers, filled by RunDAG

    # ── convenience accessors ──
    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def label(self) -> str:
        """Short human label for visualization (≤ 40 chars)."""
        k = self.kind
        p = self.payload or {}
        if k == "contract_state":
            return f"{p.get('from') or 'init'}→{p.get('to')}"
        if k == "contract_represent":
            return f"REPRESENT ({len(p.get('representations', []))} reps)"
        if k in ("contract_stipulate", "contract_decompose"):
            return f"STIPULATE ({len(p.get('steps', []))} steps)"
        if k == "contract_admit":
            return f"ACTIVATE {'✓' if p.get('ok') else '✗'}"
        if k in ("capability_perform", "capability_invoke"):
            return f"perform {p.get('iteration', '?')}: {p.get('tool', '?')}"
        if k == "tool_call":
            return f"call {p.get('tool', '?')}"
        if k == "tool_result":
            return "OK" if not p.get("is_error") else "ERR"
        if k == "criterion_check":
            if p.get("judge"):
                return f"judge {p.get('step_id')}: {'✓' if p.get('complete') else '✗'}"
            return f"oracle {'✓' if p.get('satisfied') else '✗'}"
        if k == "llm_usage":
            return f"llm({p.get('role')}) {p.get('in', 0)}+{p.get('out', 0)}t"
        if k == "step_end":
            return f"step_end ({p.get('final_state')})"
        return k


# ── RunDAG ─────────────────────────────────────────────────────────────────


@dataclass
class RunDAG:
    """The typed DAG of one run.

    `nodes` is a dict from event_id to Node. `roots` is the set of nodes whose
    parent_id is None (well-formed CAT runs have exactly one; sessions may add
    a session_start root).
    """
    nodes: dict[str, Node] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "RunDAG":
        events: list[dict] = []
        with Path(path).open() as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return cls.from_events(events)

    @classmethod
    def from_events(cls, events: list[dict]) -> "RunDAG":
        dag = cls()
        for ev in events:
            n = Node(
                event_id=ev["event_id"],
                parent_id=ev.get("parent_id"),
                kind=ev["kind"],
                layer=EVENT_TO_LAYER.get(ev["kind"], "other"),
                ts=ev.get("ts", ""),
                payload=ev.get("payload", {}) or {},
            )
            dag.nodes[n.event_id] = n
        # Wire children + collect roots
        for n in dag.nodes.values():
            if n.parent_id is None:
                dag.roots.append(n.event_id)
            elif n.parent_id in dag.nodes:
                dag.nodes[n.parent_id].children_ids.append(n.event_id)
        return dag

    # ── Queries ──

    def is_acyclic(self) -> bool:
        """Walk every node's ancestor chain; assert no cycles."""
        for start_id in self.nodes:
            seen: set[str] = set()
            cur = start_id
            while cur is not None:
                if cur in seen:
                    return False
                seen.add(cur)
                cur = self.nodes[cur].parent_id if cur in self.nodes else None
        return True

    def path_to(self, event_id: str) -> list[Node]:
        """Ancestor chain from `event_id` back to its root (inclusive)."""
        path: list[Node] = []
        cur: Optional[str] = event_id
        while cur is not None and cur in self.nodes:
            n = self.nodes[cur]
            path.append(n)
            cur = n.parent_id
        return list(reversed(path))

    def longest_chain(self) -> list[Node]:
        """The longest root-to-leaf chain by node count."""
        # leaves = nodes with no children
        leaves = [n.event_id for n in self.nodes.values() if not n.children_ids]
        best: list[Node] = []
        for leaf in leaves:
            path = self.path_to(leaf)
            if len(path) > len(best):
                best = path
        return best

    def branch_points(self) -> list[Node]:
        """Nodes with >1 child (the DAG forks here)."""
        return [n for n in self.nodes.values() if len(n.children_ids) > 1]

    def nodes_by_layer(self) -> dict[str, list[Node]]:
        """Group nodes by CAT layer (represent/decompose/activate/invoke/verify/lifecycle/llm)."""
        out: dict[str, list[Node]] = {}
        for n in self.nodes.values():
            out.setdefault(n.layer, []).append(n)
        return out

    def terminal_state(self) -> Optional[Node]:
        """The final contract_state event (FULFILLED / FAILED / SUSPENDED / EXPIRED)."""
        terminals = ("fulfilled", "failed", "suspended", "expired")
        candidates = [
            n for n in self.nodes.values()
            if n.kind == "contract_state"
            and not n.payload.get("rejected")
            and n.payload.get("to") in terminals
        ]
        if not candidates:
            return None
        # Pick by latest timestamp
        return max(candidates, key=lambda n: n.ts)

    # ── Persistence ──

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "n_nodes": len(self.nodes),
            "roots": self.roots,
            "nodes": [
                {
                    "event_id": n.event_id,
                    "parent_id": n.parent_id,
                    "kind": n.kind,
                    "layer": n.layer,
                    "ts": n.ts,
                    "label": n.label(),
                    "children": n.children_ids,
                    # Keep payload but redact large fields for size
                    "payload": _redact_payload(n.payload),
                }
                for n in self.nodes.values()
            ],
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))


def _redact_payload(p: dict) -> dict:
    """Trim large fields so dag.json stays compact (full data is in the JSONL)."""
    out = {}
    for k, v in p.items():
        if isinstance(v, str) and len(v) > 400:
            out[k] = v[:400] + f"... [+{len(v)-400} chars]"
        elif isinstance(v, (list, dict)) and len(json.dumps(v)) > 600:
            out[k] = f"<{type(v).__name__} truncated, see JSONL>"
        else:
            out[k] = v
    return out
