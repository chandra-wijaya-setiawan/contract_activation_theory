"""PlanTree — the central working data structure for the v2 loop redesign.

Per docs/iteration.md §3-§4. A plan tree organizes work into phases
(MAP/DESIGN/IMPLEMENT/VERIFY), each containing ordered steps. Steps may be
refined into substeps, branched into alternatives, merged back, or dropped.

The tree is mutated only through ops (COMMIT, CHECKOUT, BRANCH, MERGE,
DROP, COMMIT_NEW, REFINE). Each op application is auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional


PhaseName = Literal["MAP", "DESIGN", "IMPLEMENT", "VERIFY", "OTHER"]
Intent = Literal["investigate", "design", "mutate", "verify", "other"]
Status = Literal["pending", "active", "complete", "pruned", "branched"]


@dataclass
class PlanNode:
    id: str
    parent_id: Optional[str]
    phase_name: PhaseName
    description: str
    intent: Intent
    expects: dict[str, Any] = field(default_factory=dict)
    status: Status = "pending"
    children: list[str] = field(default_factory=list)
    created_at_iter: int = 0
    completed_at_iter: Optional[int] = None
    completion_reason: Optional[str] = None
    branch_of: Optional[str] = None
    """If this node was created by a BRANCH op, points to the node it branched from."""


@dataclass
class TreeEditOp:
    """Represents one tree edit. Echoed into the trace recorder."""
    op: str
    payload: dict[str, Any]
    reflection: str = ""


class PlanTree:
    """Working tree of plan nodes. Mutated only via apply_op().

    Node IDs are activator-assigned, hierarchical: "A", "A1", "A2", "B", ...
    Branch IDs append "-alt-N": "C1-alt-1".
    """

    def __init__(self, obligation_class: str = ""):
        self.obligation_class = obligation_class
        self.nodes: dict[str, PlanNode] = {}
        self.root_id: Optional[str] = None
        self.attention_id: Optional[str] = None
        self.phase_order: list[str] = []  # phase node IDs in order: ["A", "B", "C", "D"]
        self._next_step_idx: dict[str, int] = {}  # per-phase counter
        self._next_alt_idx: dict[str, int] = {}  # per-node alt counter
        self._iter: int = 0  # set externally before apply_op

    # ── Construction (used by cold-start Stipulate) ───────────────────────
    @classmethod
    def from_phased_plan(
        cls,
        phased_plan: dict[str, Any],
        obligation_class: str = "",
    ) -> "PlanTree":
        """Build a PlanTree from a cold-start Stipulator phased-plan JSON.

        Shape per docs/iteration.md §6.1:
            {"phases": [{"name": "MAP", "steps": [{description, intent, expects}, ...]}, ...]}

        IDs are assigned: phase = "A", "B", ...; step = "A1", "A2", ...
        """
        tree = cls(obligation_class=obligation_class)
        phases = phased_plan.get("phases", []) or []
        for phase_idx, phase in enumerate(phases):
            phase_name = phase.get("name", "OTHER")
            if phase_name not in ("MAP", "DESIGN", "IMPLEMENT", "VERIFY"):
                phase_name = "OTHER"
            phase_id = chr(ord("A") + phase_idx)
            phase_node = PlanNode(
                id=phase_id,
                parent_id=None,
                phase_name=phase_name,
                description=phase_name,
                intent="other",
                status="pending",
            )
            tree.nodes[phase_id] = phase_node
            tree.phase_order.append(phase_id)
            tree._next_step_idx[phase_id] = 1
            if tree.root_id is None:
                tree.root_id = phase_id
            for step in phase.get("steps", []) or []:
                step_id = f"{phase_id}{tree._next_step_idx[phase_id]}"
                tree._next_step_idx[phase_id] += 1
                desc = step.get("description", "")
                intent = step.get("intent", "other")
                if intent not in ("investigate", "design", "mutate", "verify"):
                    intent = "other"
                step_node = PlanNode(
                    id=step_id,
                    parent_id=phase_id,
                    phase_name=phase_name,
                    description=desc,
                    intent=intent,
                    expects=(step.get("expects") if isinstance(step.get("expects"), dict) else {}) or {},
                )
                tree.nodes[step_id] = step_node
                phase_node.children.append(step_id)
        # set attention to first leaf step
        first_leaf = tree._first_pending_leaf()
        if first_leaf:
            tree.attention_id = first_leaf
            tree.nodes[first_leaf].status = "active"
        return tree

    def _first_pending_leaf(self) -> Optional[str]:
        for phase_id in self.phase_order:
            for child_id in self.nodes[phase_id].children:
                child = self.nodes[child_id]
                if child.status == "pending" and not child.children:
                    return child_id
        return None

    def _next_pending_leaf_after(self, node_id: str) -> Optional[str]:
        """First pending leaf in document order strictly after node_id."""
        order: list[str] = []
        for phase_id in self.phase_order:
            for child_id in self.nodes[phase_id].children:
                order.append(child_id)
        try:
            idx = order.index(node_id)
        except ValueError:
            return self._first_pending_leaf()
        for nid in order[idx + 1:]:
            n = self.nodes[nid]
            if n.status == "pending" and not n.children:
                return nid
        return None

    # ── Op application ────────────────────────────────────────────────────
    def apply_op(self, op: TreeEditOp, iter_n: int = 0) -> tuple[bool, str]:
        """Apply one op. Returns (ok, error_or_empty)."""
        self._iter = iter_n
        kind = op.op
        p = op.payload
        try:
            if kind == "COMMIT":
                return self._op_commit(p)
            if kind == "CHECKOUT":
                return self._op_checkout(p)
            if kind == "DROP":
                return self._op_drop(p)
            if kind == "COMMIT_NEW":
                return self._op_commit_new(p)
            if kind == "REFINE":
                return self._op_refine(p)
            if kind == "BRANCH":
                return self._op_branch(p)
            if kind == "MERGE":
                return self._op_merge(p)
            return False, f"unknown op: {kind}"
        except Exception as e:
            return False, f"op {kind} raised: {e}"

    def _op_commit(self, p: dict) -> tuple[bool, str]:
        node_id = p.get("node_id")
        if not node_id or node_id not in self.nodes:
            return False, f"COMMIT: unknown node_id {node_id!r}"
        n = self.nodes[node_id]
        n.status = "complete"
        n.completed_at_iter = self._iter
        n.completion_reason = p.get("reason", "")
        return True, ""

    def _op_checkout(self, p: dict) -> tuple[bool, str]:
        node_id = p.get("node_id")
        if not node_id or node_id not in self.nodes:
            return False, f"CHECKOUT: unknown node_id {node_id!r}"
        # only checkout to leaf-status pending nodes
        target = self.nodes[node_id]
        if target.children:
            return False, f"CHECKOUT: {node_id} is not a leaf"
        if self.attention_id and self.attention_id in self.nodes:
            cur = self.nodes[self.attention_id]
            if cur.status == "active":
                cur.status = "pending"
        target.status = "active"
        self.attention_id = node_id
        return True, ""

    def _op_drop(self, p: dict) -> tuple[bool, str]:
        node_id = p.get("node_id")
        if not node_id or node_id not in self.nodes:
            return False, f"DROP: unknown node_id {node_id!r}"
        # mark node + all descendants as pruned (history kept)
        stack = [node_id]
        while stack:
            cur = stack.pop()
            n = self.nodes[cur]
            n.status = "pruned"
            n.completed_at_iter = self._iter
            n.completion_reason = p.get("reason", "")
            stack.extend(n.children)
        return True, ""

    def _op_commit_new(self, p: dict) -> tuple[bool, str]:
        parent_id = p.get("parent_id")
        if not parent_id or parent_id not in self.nodes:
            return False, f"COMMIT_NEW: unknown parent_id {parent_id!r}"
        new_step = p.get("new_step")
        if not isinstance(new_step, dict):
            return False, "COMMIT_NEW: new_step must be a dict"
        parent = self.nodes[parent_id]
        # Assign id: if parent is a phase, use phase-step counter; else parent.id + "." + idx
        if parent_id in self.phase_order:
            step_id = f"{parent_id}{self._next_step_idx[parent_id]}"
            self._next_step_idx[parent_id] += 1
        else:
            idx = len([c for c in parent.children]) + 1
            step_id = f"{parent_id}.{idx}"
        intent = new_step.get("intent", "other")
        if intent not in ("investigate", "design", "mutate", "verify"):
            intent = "other"
        node = PlanNode(
            id=step_id,
            parent_id=parent_id,
            phase_name=parent.phase_name,
            description=new_step.get("description", ""),
            intent=intent,
            expects=(new_step.get("expects") if isinstance(new_step.get("expects"), dict) else {}) or {},
            created_at_iter=self._iter,
        )
        self.nodes[step_id] = node
        parent.children.append(step_id)
        return True, ""

    def _op_refine(self, p: dict) -> tuple[bool, str]:
        node_id = p.get("node_id")
        if not node_id or node_id not in self.nodes:
            return False, f"REFINE: unknown node_id {node_id!r}"
        substeps = p.get("substeps") or []
        # Filter out non-dict entries (LLM sometimes emits strings)
        substeps = [s for s in substeps if isinstance(s, dict)]
        parent = self.nodes[node_id]
        # The refined node becomes a parent: its status pending; substeps become its children
        for idx, sub in enumerate(substeps, start=1):
            sub_id = f"{node_id}.{idx}"
            intent = sub.get("intent", "other")
            if intent not in ("investigate", "design", "mutate", "verify"):
                intent = "other"
            node = PlanNode(
                id=sub_id,
                parent_id=node_id,
                phase_name=parent.phase_name,
                description=sub.get("description", ""),
                intent=intent,
                expects=(sub.get("expects") if isinstance(sub.get("expects"), dict) else {}) or {},
                created_at_iter=self._iter,
            )
            self.nodes[sub_id] = node
            parent.children.append(sub_id)
        # If the refined node was the attention, shift attention to the first new substep
        if self.attention_id == node_id and parent.children:
            parent.status = "pending"  # no longer a leaf; can't be active
            first_sub = parent.children[0]
            self.nodes[first_sub].status = "active"
            self.attention_id = first_sub
        return True, ""

    def _op_branch(self, p: dict) -> tuple[bool, str]:
        node_id = p.get("node_id")
        if not node_id or node_id not in self.nodes:
            return False, f"BRANCH: unknown node_id {node_id!r}"
        alt_steps = p.get("alt_steps") or []
        alt_steps = [s for s in alt_steps if isinstance(s, dict)]
        if not alt_steps:
            return False, "BRANCH: alt_steps empty"
        orig = self.nodes[node_id]
        # Branch ID: append "-alt-N"
        self._next_alt_idx[node_id] = self._next_alt_idx.get(node_id, 0) + 1
        alt_n = self._next_alt_idx[node_id]
        branch_id = f"{node_id}-alt-{alt_n}"
        # Mark original as branched (not destroyed; future MERGE may restore)
        orig.status = "branched"
        # Create branch_id container node as a sibling of orig
        parent_id = orig.parent_id
        if parent_id is None:
            return False, f"BRANCH: cannot branch root node {node_id}"
        parent = self.nodes[parent_id]
        branch_node = PlanNode(
            id=branch_id,
            parent_id=parent_id,
            phase_name=orig.phase_name,
            description=f"alternative branch for {node_id}",
            intent="other",
            status="pending",
            branch_of=node_id,
            created_at_iter=self._iter,
        )
        self.nodes[branch_id] = branch_node
        # Insert branch_id right after node_id in parent.children
        try:
            idx = parent.children.index(node_id)
            parent.children.insert(idx + 1, branch_id)
        except ValueError:
            parent.children.append(branch_id)
        # Add alt steps as children of branch_id
        for sidx, sub in enumerate(alt_steps, start=1):
            sub_id = f"{branch_id}.{sidx}"
            intent = sub.get("intent", "other")
            if intent not in ("investigate", "design", "mutate", "verify"):
                intent = "other"
            sub_node = PlanNode(
                id=sub_id,
                parent_id=branch_id,
                phase_name=orig.phase_name,
                description=sub.get("description", ""),
                intent=intent,
                expects=(sub.get("expects") if isinstance(sub.get("expects"), dict) else {}) or {},
                created_at_iter=self._iter,
            )
            self.nodes[sub_id] = sub_node
            branch_node.children.append(sub_id)
        # Move attention to first alt step
        if self.attention_id == node_id:
            first_sub = branch_node.children[0]
            self.nodes[first_sub].status = "active"
            self.attention_id = first_sub
        return True, ""

    def _op_merge(self, p: dict) -> tuple[bool, str]:
        branch_id = p.get("branch_id")
        target_id = p.get("target_id")
        if not branch_id or branch_id not in self.nodes:
            return False, f"MERGE: unknown branch_id {branch_id!r}"
        if not target_id or target_id not in self.nodes:
            return False, f"MERGE: unknown target_id {target_id!r}"
        branch = self.nodes[branch_id]
        target = self.nodes[target_id]
        # The merge: mark branch as complete, target as superseded (pruned), but
        # keep both in the tree. Conceptually: "the branch is the new main."
        branch.status = "complete"
        branch.completed_at_iter = self._iter
        branch.completion_reason = "merged from branch"
        target.status = "pruned"
        target.completion_reason = f"superseded by {branch_id}"
        return True, ""

    # ── Read helpers ──────────────────────────────────────────────────────
    def get_attention(self) -> Optional[PlanNode]:
        if not self.attention_id:
            return None
        return self.nodes.get(self.attention_id)

    def advance_attention(self) -> Optional[str]:
        """Move attention to the next pending leaf after the current one."""
        if not self.attention_id:
            first = self._first_pending_leaf()
            if first:
                self.attention_id = first
                self.nodes[first].status = "active"
            return self.attention_id
        cur = self.nodes.get(self.attention_id)
        if cur and cur.status == "active":
            # the current node is presumed done; the caller decides whether to COMMIT first
            pass
        nxt = self._next_pending_leaf_after(self.attention_id)
        if nxt:
            self.nodes[nxt].status = "active"
            self.attention_id = nxt
        else:
            self.attention_id = None
        return self.attention_id

    def lookahead(self, max_steps: int = 4) -> list[PlanNode]:
        """Return up to N upcoming pending-leaf nodes after current attention."""
        out: list[PlanNode] = []
        if not self.attention_id:
            return out
        # walk pending leaves from current attention onward
        order: list[str] = []
        for phase_id in self.phase_order:
            for child_id in self.nodes[phase_id].children:
                if not self.nodes[child_id].children:
                    order.append(child_id)
                else:
                    for gc in self.nodes[child_id].children:
                        if not self.nodes[gc].children:
                            order.append(gc)
        try:
            idx = order.index(self.attention_id)
        except ValueError:
            return out
        for nid in order[idx:]:
            n = self.nodes[nid]
            if n.status in ("pending", "active"):
                out.append(n)
                if len(out) >= max_steps:
                    break
        return out

    def all_complete(self) -> bool:
        for nid in self.nodes:
            n = self.nodes[nid]
            if not n.children and n.status not in ("complete", "pruned"):
                return False
        return True

    # ── Serialization ─────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_class": self.obligation_class,
            "root_id": self.root_id,
            "attention_id": self.attention_id,
            "phase_order": list(self.phase_order),
            "nodes": {nid: asdict(n) for nid, n in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanTree":
        tree = cls(obligation_class=d.get("obligation_class", ""))
        tree.root_id = d.get("root_id")
        tree.attention_id = d.get("attention_id")
        tree.phase_order = list(d.get("phase_order", []))
        for nid, nd in (d.get("nodes") or {}).items():
            tree.nodes[nid] = PlanNode(**nd)
        # rebuild counters
        for phase_id in tree.phase_order:
            n = sum(1 for c in tree.nodes[phase_id].children if not c.startswith(phase_id + "-alt"))
            tree._next_step_idx[phase_id] = n + 1
        return tree

    def render_outline(self) -> str:
        """Return a short text outline of the tree for prompt injection."""
        lines: list[str] = []
        for phase_id in self.phase_order:
            phase = self.nodes[phase_id]
            lines.append(f"[{phase.status[:1].upper()}] {phase_id} {phase.phase_name}")
            for child_id in phase.children:
                child = self.nodes[child_id]
                marker = {
                    "pending": " ",
                    "active": ">",
                    "complete": "x",
                    "pruned": "-",
                    "branched": "b",
                }.get(child.status, "?")
                lines.append(f"  [{marker}] {child_id} ({child.intent}): {child.description[:80]}")
                for gc_id in child.children:
                    gc = self.nodes[gc_id]
                    gc_marker = {
                        "pending": " ",
                        "active": ">",
                        "complete": "x",
                        "pruned": "-",
                        "branched": "b",
                    }.get(gc.status, "?")
                    lines.append(f"      [{gc_marker}] {gc_id} ({gc.intent}): {gc.description[:80]}")
        return "\n".join(lines)
