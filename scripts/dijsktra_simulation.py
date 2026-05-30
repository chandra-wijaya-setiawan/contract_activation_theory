"""POC iter v1 — CEGAR-shaped trace ingestion.

Co-developed with docs/dijsktralayer_manual_by_hand_draft.md.

Three layers (per external research):
  mechanism  : CEGAR predicate-tree abstraction (TriCEGAR)
  criterion  : value bisimulation = cost-to-GOAL  (Ferns/Precup)
  objective  : backward Dijkstra / shortest path  (our v1.7)

v1 deliberately uses the COARSEST possible abstraction (one leaf per
(obligation_hash, plan_idx) pair) so we can SEE the cost-to-goal variance
inside it. That variance is the counterexample signal v2 will use to split.

Usage:
    python scripts/dijsktra_simulation.py <run_dir>
    python scripts/dijsktra_simulation.py <run_dir> --json

<run_dir> is a tb-runs leaf containing results.json + per-task subdirs.

Iteration discipline: every output here should drive a specific decision in
the .md. Keep raw features rich and unhashed — refinement is a function
applied over them, not a hash recomputed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from heapq import heappop, heappush
from pathlib import Path
from typing import Any, Optional


# ── Canonicalization helpers (raw_features inputs, NOT hashed yet) ──────────


def short_hash(s: Any) -> str:
    return hashlib.sha256(json.dumps(s, sort_keys=True, default=str).encode()).hexdigest()[:8]


_BASH_PATTERNS = [
    (r"^pip install", "pip-install"),
    (r"^pip ",        "pip-other"),
    (r"^python\s+-m\s+pytest", "pytest"),
    (r"^python\s+-m\s+pip",    "pip-via-python"),
    (r"^python\s+-c\s+",       "python-c-script"),
    (r"^python\s+tests/runtests", "django-runtests"),
    (r"^python\s+",            "python-script"),
    (r"^pytest",               "pytest"),
    (r"^git diff",             "git-diff"),
    (r"^git log",              "git-log"),
    (r"^git\s+",               "git-other"),
    (r"^ls\b",                 "ls"),
    (r"^cat\b",                "cat"),
    (r"^find\b",               "find"),
    (r"^grep\b",               "grep"),
    (r"^sed\b",                "sed"),
    (r"^head\b|^tail\b|^wc\b", "head-tail-wc"),
    (r"^echo\b",               "echo"),
    (r"^mkdir\b",              "mkdir"),
    (r"^rm\b|^cp\b|^mv\b",     "fs-mutate"),
]


def bash_command_class(cmd: str) -> str:
    c = (cmd or "").strip().lower()
    c = re.sub(r"^cd\s+\S+\s*&&\s*", "", c)
    for pat, klass in _BASH_PATTERNS:
        if re.match(pat, c):
            return klass
    return "bash-other"


# State-changing tool classes — used to mark durable_change on each sample
STATE_CHANGING_TOOLS = {"str_replace", "multi_edit", "apply_patch", "write"}
STATE_CHANGING_BASH = {"pip-install", "pip-other", "pip-via-python", "mkdir", "fs-mutate"}


def is_state_changing(tool: str, args: dict, is_error: bool) -> bool:
    if is_error: return False
    if tool in STATE_CHANGING_TOOLS: return True
    if tool == "bash":
        return bash_command_class(args.get("command", "")) in STATE_CHANGING_BASH
    return False


def canonical_action_args(tool: str, args: dict) -> dict:
    """Light canonicalization — strips path randomness, buckets numerics.
    Used to compute action_class (which IS hashed)."""
    a = dict(args or {})
    for k in ("path", "paths", "root", "workdir"):
        v = a.get(k)
        if isinstance(v, str):
            a[k] = v.replace("/testbed/", "<repo>/")
        elif isinstance(v, list):
            a[k] = [x.replace("/testbed/", "<repo>/") if isinstance(x, str) else x for x in v]
    for k in ("offset", "limit", "context"):
        v = a.get(k)
        if isinstance(v, (int, float)):
            iv = int(v)
            a[k] = "small" if iv <= 10 else "medium" if iv <= 100 else "large"
    if tool == "bash":
        a["command_class"] = bash_command_class(a.get("command", ""))
        a.pop("command", None)
        a.pop("timeout_s", None)
    return a


def action_class(tool: str, args: dict) -> str:
    return short_hash({"tool": tool, "args": canonical_action_args(tool, args)})


# ── Sample: the unit of data — raw_features UNHASHED ───────────────────────


@dataclass
class Sample:
    """One concrete (state→action→next_state) transition observed in a trace.

    raw_features stays RICH and UNHASHED — the PredicateTree applies a
    function over it to get state_class. Refinement just changes the
    function, not the data.
    """
    trace_id: str               # which run+task this came from
    iter_idx: int               # 1-indexed iteration within trajectory
    # --- raw inputs (state side, BEFORE action) ---
    raw_features: dict          # everything that could become a predicate
    # --- action side ---
    tool: str
    action_args: dict           # canonicalized for action_class only
    action_klass: str           # short hash
    weight: float               # cost = performer_in + attestor_in tokens
    # --- effect ---
    durable_change: bool        # action changed durable state?
    next_raw_features: dict     # state AFTER action
    # --- terminal (trajectory-level) ---
    reached_goal: bool           # from harness oracle (is_resolved)
    on_goal_path: bool           # this sample is in a PASS trajectory
    # --- filled later ---
    state_class: Optional[int] = None        # set by PredicateTree
    next_state_class: Optional[int] = None
    realized_cost_to_goal: float = math.inf  # set after trajectory walk


# ── Trace ingestion: produces list of Samples per task ──────────────────────


def _extract_obligation_and_plan(events: list[dict]) -> tuple[str, list[dict]]:
    obligation = ""
    plan = []
    for ev in events:
        if ev.get("kind") == "contract_state" and ev["payload"].get("obligation"):
            obligation = ev["payload"]["obligation"]
        if ev.get("kind") == "contract_stipulate":
            plan = ev["payload"].get("steps", [])
            break
    return obligation, plan


def ingest_trace(jsonl_path: Path, reached_goal: bool) -> list[Sample]:
    """Walk a trace JSONL → list of Samples. Single pass.

    reached_goal MUST come from the harness oracle, NOT the agent's contract
    state machine (the v1.6.6 divergence: agent state=failed but harness=PASS).
    """
    events = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    if not events:
        return []
    obligation_text, plan_steps = _extract_obligation_and_plan(events)
    obligation_hash = short_hash(obligation_text)
    trace_id = jsonl_path.parent.parent.name  # task-trial dir name

    samples: list[Sample] = []

    # Progressive state vars (raw features that evolve during the trajectory)
    workdir_version = 0
    plan_step_idx = 0
    env_facts: set = set()
    last_outcome: Optional[str] = None
    last_tool: Optional[str] = None
    last_bash_class: Optional[str] = None
    attempted_actions: set = set()
    attempted_bash_classes: set = set()

    # Buffer for the current iter
    current = None

    def snapshot_raw():
        return {
            "obligation_hash":       obligation_hash,
            "plan_idx":              plan_step_idx,
            "workdir_version":       workdir_version,
            "env_facts":             sorted(env_facts),
            "last_outcome":          last_outcome,
            "last_tool":             last_tool,
            "last_bash_class":       last_bash_class,
            "n_attempted_actions":   len(attempted_actions),
            "n_attempted_bash":      len(attempted_bash_classes),
            "attempted_actions":     sorted(attempted_actions),
            "attempted_bash_classes": sorted(attempted_bash_classes),
        }

    for ev in events:
        k = ev.get("kind")
        p = ev.get("payload", {}) or {}

        if k == "capability_perform":
            if current is not None:
                # finalize previous: capture post-state, append to samples
                current["next_raw"] = snapshot_raw()
                samples.append(_finalize_sample(current, trace_id, reached_goal))
            current = {
                "iter":   len(samples) + 1,
                "raw":    snapshot_raw(),
                "tool":   None,
                "args":   None,
                "error":  None,
                "tokens": 0,
                "durable": False,
            }

        elif k == "tool_call" and current is not None:
            tc = p.get("tool_call_id", "")
            if tc.startswith(("rep-", "oracle-")):
                continue
            current["tool"] = p.get("tool")
            current["args"] = p.get("args", {}) or {}

        elif k == "tool_result" and current is not None:
            tc = p.get("tool_call_id", "")
            if tc.startswith(("rep-", "oracle-")):
                continue
            err = bool(p.get("is_error"))
            current["error"] = err
            # apply effects to progressive state
            if current["tool"]:
                t = current["tool"]
                a = current["args"] or {}
                attempted_actions.add(action_class(t, a))
                if t == "bash":
                    bcls = bash_command_class(a.get("command", ""))
                    attempted_bash_classes.add(bcls)
                    last_bash_class = bcls
                last_tool = t
                if is_state_changing(t, a, err):
                    current["durable"] = True
                    workdir_version += 1
                    cmd = (a.get("command") or "").lower()
                    if "pip install pytest" in cmd:
                        env_facts.add("pytest_installed")
                last_outcome = "error" if err else "ok"

        elif k == "criterion_check" and current is not None:
            ok = p.get("ok")
            if ok is True and p.get("step_id"):
                sid = p["step_id"]
                if sid.startswith("s") and sid[1:].isdigit():
                    new_idx = int(sid[1:])
                    if new_idx > plan_step_idx:
                        plan_step_idx = new_idx

        elif k == "llm_usage" and current is not None:
            current["tokens"] += int(p.get("in", 0))

    if current is not None:
        current["next_raw"] = snapshot_raw()
        samples.append(_finalize_sample(current, trace_id, reached_goal))

    # Compute realized_cost_to_goal per sample (sum of weights from this iter
    # forward to end of trajectory) — only meaningful if reached_goal.
    if reached_goal and samples:
        suffix_cost = 0.0
        for s in reversed(samples):
            suffix_cost += s.weight
            s.realized_cost_to_goal = suffix_cost
    # If trajectory failed, realized_cost_to_goal stays inf (default).

    return samples


def _finalize_sample(buf: dict, trace_id: str, reached_goal: bool) -> Sample:
    return Sample(
        trace_id=trace_id,
        iter_idx=buf["iter"],
        raw_features=buf["raw"],
        tool=buf["tool"] or "",
        action_args=canonical_action_args(buf["tool"] or "", buf["args"] or {}),
        action_klass=action_class(buf["tool"] or "", buf["args"] or {}),
        weight=float(buf["tokens"]),
        durable_change=buf["durable"],
        next_raw_features=buf["next_raw"],
        reached_goal=reached_goal,
        on_goal_path=reached_goal,
    )


# ── PredicateTree (v1: only base keys, ONE conceptual leaf per base-tuple) ──


class PredicateTree:
    """Starts maximally coarse — every sample with the same base-key tuple is
    one leaf. Refinement (v2+) will add learned predicates as splits.

    base_keys are the dimensions the tree treats as ALWAYS-ON: every other
    raw feature must be earned into the tree via a counterexample-driven
    split.
    """

    def __init__(self, base_keys=("obligation_hash", "plan_idx")):
        self.base_keys = base_keys
        # Stable assignment: (base_tuple) → leaf_id (int)
        self._next_id = 0
        self._key_to_id: dict[tuple, int] = {}
        # In v1 we ONLY have base-key leaves. v2 adds learned predicates per leaf.

    def _base_tuple(self, raw: dict) -> tuple:
        return tuple(raw.get(k) for k in self.base_keys)

    def state_class(self, raw: dict) -> int:
        key = self._base_tuple(raw)
        if key not in self._key_to_id:
            self._key_to_id[key] = self._next_id
            self._next_id += 1
        return self._key_to_id[key]

    def n_leaves(self) -> int:
        return len(self._key_to_id)


# ── Graph + backward Dijkstra ───────────────────────────────────────────────


GOAL_NODE = -1  # special sentinel node id for the virtual GOAL sink


def build_graph(samples: list[Sample], tree: PredicateTree) -> dict:
    """Apply the tree to each sample; build a graph keyed by state_class.

    Edges are weighted by the MEAN cost of the (s, a, s') transition across
    all traces that observed it.
    """
    # Assign state classes
    for s in samples:
        s.state_class = tree.state_class(s.raw_features)
        s.next_state_class = tree.state_class(s.next_raw_features)
    # Collect parallel-edge weights for averaging
    edge_buckets: dict[tuple, list[float]] = defaultdict(list)
    for s in samples:
        key = (s.state_class, s.action_klass, s.next_state_class)
        edge_buckets[key].append(s.weight)
    # 0-cost edge from any state that was last-of-PASS-trajectory to GOAL
    pass_terminal_states: set[int] = set()
    by_trace: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_trace[s.trace_id].append(s)
    for trace_id, samps in by_trace.items():
        if samps[-1].reached_goal:
            pass_terminal_states.add(samps[-1].next_state_class)
    # Build out-edges dict for forward graph, in-edges dict for backward Dijkstra
    out_edges: dict[int, list[tuple]] = defaultdict(list)
    in_edges: dict[int, list[tuple]] = defaultdict(list)
    for (src, akls, dst), weights in edge_buckets.items():
        w_mean = sum(weights) / len(weights)
        out_edges[src].append((akls, dst, w_mean, len(weights)))
        in_edges[dst].append((src, w_mean))
    for term in pass_terminal_states:
        out_edges[term].append(("__goal__", GOAL_NODE, 0.0, 1))
        in_edges[GOAL_NODE].append((term, 0.0))
    return {
        "out_edges": dict(out_edges),
        "in_edges":  dict(in_edges),
        "edge_buckets": dict(edge_buckets),
        "pass_terminal_states": pass_terminal_states,
    }


def backward_dijkstra(graph: dict, target: int = GOAL_NODE) -> dict[int, float]:
    """d*(s) = shortest distance from s to target (GOAL by default)."""
    in_edges = graph["in_edges"]
    dist: dict[int, float] = {target: 0.0}
    pq: list[tuple[float, int]] = [(0.0, target)]
    while pq:
        d, u = heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for src, w in in_edges.get(u, []):
            nd = d + w
            if nd < dist.get(src, math.inf):
                dist[src] = nd
                heappush(pq, (nd, src))
    return dist


# ── Reporters ───────────────────────────────────────────────────────────────


def report_variance_per_leaf(samples: list[Sample], tree: PredicateTree) -> str:
    """For each state class with ≥2 PASS-path samples, compute variance of
    realized_cost_to_goal. THIS is the counterexample signal (Trigger B).
    """
    lines = []
    lines.append(f"# Predicate tree leaves: {tree.n_leaves()}")
    lines.append(f"# Total samples: {len(samples)}")
    lines.append(f"# PASS-path samples: {sum(1 for s in samples if s.on_goal_path)}")
    lines.append("")
    by_leaf: dict[int, list[Sample]] = defaultdict(list)
    for s in samples:
        if s.on_goal_path:
            by_leaf[s.state_class].append(s)
    lines.append("leaf | n_samples | n_traces | mean_real_cost | stdev_real_cost | CV")
    lines.append("-----|-----------|----------|----------------|------------------|----")
    for leaf in sorted(by_leaf):
        ss = by_leaf[leaf]
        costs = [s.realized_cost_to_goal for s in ss if s.realized_cost_to_goal != math.inf]
        if not costs:
            continue
        n_traces = len(set(s.trace_id for s in ss))
        mean = statistics.mean(costs)
        stdev = statistics.stdev(costs) if len(costs) > 1 else 0.0
        cv = stdev / mean if mean else 0.0
        lines.append(f"{leaf:>4} | {len(costs):>9} | {n_traces:>8} | {mean:>14.0f} | {stdev:>16.0f} | {cv:>3.2f}")
    return "\n".join(lines)


def report_actions_per_leaf(samples: list[Sample], tree: PredicateTree, graph: dict, top: int = 3) -> str:
    """For each leaf, list candidate actions seen, their mean cost, and
    cost-to-goal of their target leaf (d*(s'))."""
    lines = []
    lines.append("# Candidate actions per leaf (top per leaf by visit count)")
    lines.append("")
    by_leaf_action: dict[tuple[int, str], list[Sample]] = defaultdict(list)
    for s in samples:
        by_leaf_action[(s.state_class, s.action_klass)].append(s)
    leaves_seen = sorted(set(s.state_class for s in samples))
    d_star = backward_dijkstra(graph)
    for leaf in leaves_seen:
        lines.append(f"\n  leaf {leaf}:  d*={d_star.get(leaf, math.inf):.0f}   (reachable={d_star.get(leaf, math.inf) != math.inf})")
        actions = [(k, v) for k, v in by_leaf_action.items() if k[0] == leaf]
        actions.sort(key=lambda x: -len(x[1]))
        for (l, akls), ss in actions[:top]:
            mean_w = statistics.mean(s.weight for s in ss)
            durable_count = sum(1 for s in ss if s.durable_change)
            next_d = statistics.median(d_star.get(s.next_state_class, math.inf) for s in ss)
            tool = ss[0].tool
            args = ss[0].action_args
            arg_s = json.dumps(args)[:60]
            lines.append(
                f"    [{akls}] n={len(ss):>3}  tool={tool:<12} args={arg_s:<60}  "
                f"mean_w={mean_w:>6.0f}  durable={durable_count:>2}/{len(ss):<2}  "
                f"d*(next)={'inf' if next_d == math.inf else f'{next_d:.0f}'}"
            )
    return "\n".join(lines)


# ── Run-dir ingestion ───────────────────────────────────────────────────────


def ingest_run_dir(run_dir: Path) -> list[Sample]:
    """Find all task subdirs under run_dir, read results.json for oracle
    outcomes, ingest each trace."""
    results_path = run_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"results.json not found at {results_path}")
    results = json.loads(results_path.read_text())
    oracle: dict[str, bool] = {}
    for r in results.get("results", []):
        oracle[r["task_id"]] = bool(r.get("is_resolved"))
    all_samples: list[Sample] = []
    n_traces = 0
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir() or task_dir.name in ("_attribution_cache",):
            continue
        for trial_dir in task_dir.iterdir():
            if not trial_dir.is_dir():
                continue
            logs = list((trial_dir / "agent-logs").glob("*.jsonl")) if (trial_dir / "agent-logs").exists() else []
            for jsonl in logs:
                reached = oracle.get(task_dir.name, False)
                samples = ingest_trace(jsonl, reached)
                if samples:
                    all_samples.extend(samples)
                    n_traces += 1
    print(f"# ingested {n_traces} traces, {len(all_samples)} samples from {run_dir.name}", file=sys.stderr)
    return all_samples


# ── CLI ─────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", nargs="+", help="path(s) to tb-runs leaves; multiple = merged corpus")
    ap.add_argument("--json", action="store_true", help="emit samples + graph as JSON")
    ap.add_argument("--actions", action="store_true", help="show candidate actions per leaf")
    args = ap.parse_args()

    samples: list[Sample] = []
    for d in args.run_dir:
        samples.extend(ingest_run_dir(Path(d)))
    if not samples:
        print("no samples ingested", file=sys.stderr)
        sys.exit(1)

    tree = PredicateTree()
    graph = build_graph(samples, tree)

    if args.json:
        out = {
            "n_samples": len(samples),
            "n_leaves": tree.n_leaves(),
            "n_edges":  sum(len(v) for v in graph["out_edges"].values()),
            "pass_terminal_states": list(graph["pass_terminal_states"]),
        }
        print(json.dumps(out, indent=2))
        return

    print(report_variance_per_leaf(samples, tree))
    if args.actions:
        print()
        print(report_actions_per_leaf(samples, tree, graph))


if __name__ == "__main__":
    main()
