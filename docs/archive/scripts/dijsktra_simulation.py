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


# ── Action intent classification (domain-agnostic, declared at register-time) ──
#
# Three primary intents map cleanly to the abstract reasoning shape:
#   investigate → gather information without changing state
#   mutate      → change durable state (edit files, install packages)
#   verify      → check whether a hypothesis / fix holds
# Anything else falls back to "other" (rare).
#
# When CAT eventually adds tau-bench, this dispatch becomes per-domain — each
# Capability declares its intent at registration. For now we hard-code the
# mapping for the swe10 tool surface.

_INVESTIGATE_TOOLS = {"read", "list", "rg", "fd", "outline",
                      "goto_def", "find_refs", "git_diff", "git_status"}
_MUTATE_TOOLS      = {"str_replace", "multi_edit", "apply_patch", "write"}

# For bash, intent depends on what's being run.
_VERIFY_BASH       = {"pytest", "django-runtests"}
_MUTATE_BASH       = {"pip-install", "pip-other", "pip-via-python", "mkdir", "fs-mutate"}
# everything else (ls, cat, find, grep, head-tail-wc, echo, bash-other, sed,
# python-c-script, python-script, git-other, git-log) → "investigate"


def action_intent_of(tool: str, args: dict) -> str:
    """Classify a tool call by its abstract reasoning role."""
    if tool in _INVESTIGATE_TOOLS:
        return "investigate"
    if tool in _MUTATE_TOOLS:
        return "mutate"
    if tool == "bash":
        bc = bash_command_class(args.get("command", ""))
        if bc in _VERIFY_BASH: return "verify"
        if bc in _MUTATE_BASH: return "mutate"
        return "investigate"
    return "other"


# State-changing = an action that successfully changed durable state.
# Re-expressed in terms of intent: every "mutate" with no error.
def is_state_changing(tool: str, args: dict, is_error: bool) -> bool:
    if is_error:
        return False
    return action_intent_of(tool, args) == "mutate"


# ── Feature classification ──────────────────────────────────────────────────
#
# CORE_FEATURES are domain-agnostic — they exist in every domain CAT runs in
# (code, API calls, browser, etc.). These should be the predicates the
# CEGAR tree prefers to split on, because they generalize.
#
# DOMAIN_FEATURES are code-Python-specific. The tree may still split on them
# when they're more predictive, but the reporter flags such splits so we
# know which predicates carry domain assumptions.

CORE_FEATURES = {
    "obligation_hash",          # always-on base key (every domain)
    "plan_idx",                  # always-on base key (every domain)
    "last_outcome",              # ok / error — every domain has outcomes
    "last_action_intent",        # last call's intent class
    "durable_change_count",      # how many mutations so far (replaces workdir_version)
    "iter_since_durable_change", # drift indicator (the f59669ec marker)
    "verify_attempt_count",      # how many verify-intent calls
    "investigate_attempt_count", # how many investigate-intent calls
    "mutate_attempt_count",      # how many mutate-intent calls
    "n_unique_actions",          # diversity of approaches tried
    "n_unique_errors",           # diversity of errors seen
}

# All other keys in raw_features (e.g. last_bash_class, env_facts,
# attempted_bash_classes) are domain-specific.


def is_core_feature(feature_name: str) -> bool:
    return feature_name in CORE_FEATURES


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

    # ── Progressive state vars (1.7.0.x two-tier refactor) ─────────────────
    # CORE (domain-agnostic) counters
    plan_step_idx               = 0
    last_outcome: Optional[str] = None
    last_action_intent: Optional[str] = None
    durable_change_count        = 0     # was workdir_version
    iter_since_durable_change   = 0     # drift indicator
    verify_attempt_count        = 0
    investigate_attempt_count   = 0
    mutate_attempt_count        = 0
    attempted_actions: set      = set()
    seen_errors: set            = set()
    # DOMAIN-SPECIFIC (code-Python) counters
    env_facts: set              = set()
    last_tool: Optional[str]    = None
    last_bash_class: Optional[str] = None
    attempted_bash_classes: set = set()

    # Buffer for the current iter
    current = None

    def snapshot_raw():
        # Two-tier shape: core keys (always populated) + domain-specific keys
        # (sparse, code-Python today). The CEGAR tree sees them all as a flat
        # dict; the reporter distinguishes them via is_core_feature.
        return {
            # ── CORE (domain-agnostic) ──
            "obligation_hash":            obligation_hash,
            "plan_idx":                   plan_step_idx,
            "last_outcome":               last_outcome,
            "last_action_intent":         last_action_intent,
            "durable_change_count":       durable_change_count,
            "iter_since_durable_change":  iter_since_durable_change,
            "verify_attempt_count":       verify_attempt_count,
            "investigate_attempt_count":  investigate_attempt_count,
            "mutate_attempt_count":       mutate_attempt_count,
            "n_unique_actions":           len(attempted_actions),
            "n_unique_errors":            len(seen_errors),
            # Set-membership (core) — useful for "have you tried X yet?" predicates
            "attempted_actions":          sorted(attempted_actions),
            # ── DOMAIN-SPECIFIC (code-Python) ──
            "env_facts":                  sorted(env_facts),
            "last_tool":                  last_tool,
            "last_bash_class":            last_bash_class,
            "attempted_bash_classes":     sorted(attempted_bash_classes),
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
                intent = action_intent_of(t, a)
                current["intent"] = intent
                # Core (agnostic) counters
                attempted_actions.add(action_class(t, a))
                if intent == "verify":      verify_attempt_count += 1
                elif intent == "investigate": investigate_attempt_count += 1
                elif intent == "mutate":      mutate_attempt_count += 1
                # Domain-specific counters
                if t == "bash":
                    bcls = bash_command_class(a.get("command", ""))
                    attempted_bash_classes.add(bcls)
                    last_bash_class = bcls
                last_tool = t
                last_action_intent = intent
                if is_state_changing(t, a, err):
                    current["durable"] = True
                    durable_change_count += 1
                    iter_since_durable_change = 0   # reset on durable change
                    cmd = (a.get("command") or "").lower()
                    if "pip install pytest" in cmd:
                        env_facts.add("pytest_installed")
                else:
                    iter_since_durable_change += 1
                if err:
                    # Track error variety (domain-agnostic; the actual error text
                    # is too noisy, so we key by tool+intent — "what KIND of
                    # thing erred")
                    seen_errors.add((t, intent))
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


def predicate_holds(raw: dict, feature: str, threshold) -> bool:
    """Evaluate a learned predicate on raw features.

    Type-dispatch on the value's runtime type:
      - numeric value + numeric threshold  → `v > threshold`
      - list value                          → `threshold in v` (membership)
      - everything else (str/None)          → `v == threshold`
    """
    v = raw.get(feature)
    if isinstance(v, (int, float)) and isinstance(threshold, (int, float)) and not isinstance(v, bool):
        return v > threshold
    if isinstance(v, list):
        return threshold in v
    return v == threshold


def describe_predicate(feature: str, threshold, sample_value) -> str:
    """Human-readable predicate string."""
    if isinstance(sample_value, (int, float)) and isinstance(threshold, (int, float)) and not isinstance(sample_value, bool):
        return f"{feature} > {threshold}"
    if isinstance(sample_value, list):
        return f"{threshold!r} in {feature}"
    return f"{feature} == {threshold!r}"


class PredicateTree:
    """Starts maximally coarse — every sample with the same base-key tuple is
    one leaf. v2 adds CEGAR-driven splits via .split().

    base_keys are the dimensions the tree treats as ALWAYS-ON: every other
    raw feature must be earned into the tree via a counterexample-driven
    split.
    """

    def __init__(self, base_keys=("obligation_hash", "plan_idx")):
        self.base_keys = base_keys
        self._base_to_root: dict[tuple, int] = {}     # base_tuple -> root node id
        self._next_id = 0
        # For internal (non-leaf) nodes:
        self.splits: dict[int, tuple] = {}             # node_id -> (feature, threshold, description)
        self.children: dict[int, tuple[int, int]] = {} # node_id -> (left_leaf_id, right_leaf_id)
        # Track which node ids are CURRENTLY leaves (for iteration)
        self._is_leaf: dict[int, bool] = {}

    def _alloc_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        self._is_leaf[i] = True
        return i

    def _base_tuple(self, raw: dict) -> tuple:
        return tuple(raw.get(k) for k in self.base_keys)

    def state_class(self, raw: dict) -> int:
        bt = self._base_tuple(raw)
        if bt not in self._base_to_root:
            self._base_to_root[bt] = self._alloc_id()
        node_id = self._base_to_root[bt]
        while node_id in self.splits:
            feature, threshold, _desc = self.splits[node_id]
            left_id, right_id = self.children[node_id]
            node_id = left_id if predicate_holds(raw, feature, threshold) else right_id
        return node_id

    def split(self, leaf_id: int, feature: str, threshold, sample_value=None) -> tuple[int, int]:
        if leaf_id in self.splits:
            raise ValueError(f"node {leaf_id} already split")
        if not self._is_leaf.get(leaf_id, False):
            raise ValueError(f"node {leaf_id} is not a leaf")
        left = self._alloc_id()
        right = self._alloc_id()
        desc = describe_predicate(feature, threshold, sample_value if sample_value is not None else threshold)
        self.splits[leaf_id] = (feature, threshold, desc)
        self.children[leaf_id] = (left, right)
        self._is_leaf[leaf_id] = False
        return left, right

    def n_leaves(self) -> int:
        return sum(1 for v in self._is_leaf.values() if v)

    def depth_of(self, node_id: int) -> int:
        """Depth from root for a node id. Linear walk; cheap for our tree sizes."""
        for root_id in self._base_to_root.values():
            d = self._depth_walk(root_id, node_id, 0)
            if d is not None:
                return d
        return -1

    def _depth_walk(self, current: int, target: int, depth: int) -> Optional[int]:
        if current == target:
            return depth
        if current not in self.splits:
            return None
        l, r = self.children[current]
        for child in (l, r):
            d = self._depth_walk(child, target, depth + 1)
            if d is not None:
                return d
        return None


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


# ── CEGAR: counterexample detection + decision-tree-stump split ────────────


def find_counterexample(
    samples: list[Sample],
    tol_cv: float = 0.30,
    min_samples: int = 4,
) -> Optional[dict]:
    """Trigger B (value_split): leaf with highest CV of realized_cost_to_goal
    above threshold. Considers only PASS-path samples (realized cost is
    well-defined). Trigger A (belief_split, self-loop) is added in v2.1.
    """
    by_leaf: dict[int, list[Sample]] = defaultdict(list)
    for s in samples:
        if s.on_goal_path and s.realized_cost_to_goal != math.inf:
            by_leaf[s.state_class].append(s)
    worst = None
    worst_cv = tol_cv
    for leaf, ss in by_leaf.items():
        if len(ss) < min_samples:
            continue
        costs = [s.realized_cost_to_goal for s in ss]
        mean = statistics.mean(costs)
        if mean == 0:
            continue
        stdev = statistics.stdev(costs) if len(costs) > 1 else 0.0
        cv = stdev / mean
        if cv > worst_cv:
            worst_cv = cv
            worst = {"leaf": leaf, "samples": ss, "kind": "value_split", "cv": cv}
    return worst


def _variance_loss(values: list[float]) -> float:
    """Population variance; 0 if fewer than 2 samples."""
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return sum((v - m) ** 2 for v in values) / len(values)


def choose_predicate(
    samples: list[Sample],
    kind: str,
    base_keys: tuple = ("obligation_hash", "plan_idx"),
) -> Optional[dict]:
    """Hand-rolled max_depth=1 decision stump over raw_features.

    Iterates every (feature, candidate_threshold) pair; picks the one with
    minimum weighted variance of the target. Target depends on kind:
      - value_split: regression on realized_cost_to_goal
      - belief_split: classification on durable_change (0/1)

    Excludes base_keys from candidates (always-on; can't be a refinement
    target). Returns {feature, threshold, description, loss, sample_value}
    or None if no informative split exists.
    """
    if kind == "value_split":
        y = [s.realized_cost_to_goal for s in samples]
    else:
        y = [1.0 if s.durable_change else 0.0 for s in samples]

    # Collect candidate (feature, threshold) pairs
    candidates: dict[str, set] = defaultdict(set)
    for s in samples:
        for fkey, fval in s.raw_features.items():
            if fkey in base_keys:
                continue
            if isinstance(fval, (int, float)) and not isinstance(fval, bool):
                candidates[fkey].add(fval)
            elif isinstance(fval, str) or fval is None:
                candidates[fkey].add(fval)
            elif isinstance(fval, list):
                for item in fval:
                    candidates[fkey].add(item)
            elif isinstance(fval, bool):
                candidates[fkey].add(fval)

    baseline_loss = _variance_loss(y)
    best = None
    best_loss = baseline_loss  # any valid split must beat the baseline

    for feature, thresholds in candidates.items():
        for threshold in thresholds:
            left_y, right_y = [], []
            sample_value_for_desc = None
            for s, y_i in zip(samples, y):
                v = s.raw_features.get(feature)
                if sample_value_for_desc is None and v is not None:
                    sample_value_for_desc = v
                if predicate_holds(s.raw_features, feature, threshold):
                    left_y.append(y_i)
                else:
                    right_y.append(y_i)
            if not left_y or not right_y:
                continue
            n = len(y)
            loss = (len(left_y) / n) * _variance_loss(left_y) + (len(right_y) / n) * _variance_loss(right_y)
            if loss < best_loss:
                best_loss = loss
                best = {
                    "feature":         feature,
                    "threshold":       threshold,
                    "loss":            loss,
                    "baseline_loss":   baseline_loss,
                    "reduction":       baseline_loss - loss,
                    "n_left":          len(left_y),
                    "n_right":         len(right_y),
                    "sample_value":    sample_value_for_desc,
                    "description":     describe_predicate(feature, threshold,
                                                          sample_value_for_desc if sample_value_for_desc is not None else threshold),
                }
    return best


def learn_abstraction(
    samples: list[Sample],
    max_iterations: int = 12,
    max_depth: int = 6,
    tol_cv: float = 0.30,
    min_samples: int = 4,
) -> tuple[PredicateTree, list[str]]:
    """The CEGAR loop. Returns (final_tree, history_log)."""
    tree = PredicateTree()
    history: list[str] = []

    for i in range(max_iterations):
        # Re-apply tree to every sample
        for s in samples:
            s.state_class = tree.state_class(s.raw_features)
            s.next_state_class = tree.state_class(s.next_raw_features)
        # Find worst counterexample
        cex = find_counterexample(samples, tol_cv=tol_cv, min_samples=min_samples)
        if cex is None:
            history.append(f"iter {i}: no counterexample above CV={tol_cv} (n_leaves={tree.n_leaves()})")
            break
        # Depth guard
        if tree.depth_of(cex["leaf"]) >= max_depth:
            history.append(f"iter {i}: leaf {cex['leaf']} at max_depth={max_depth}; skipping (consider raising max_depth)")
            break
        # Choose predicate
        pred = choose_predicate(cex["samples"], cex["kind"], base_keys=tree.base_keys)
        if pred is None:
            history.append(f"iter {i}: leaf {cex['leaf']} CV={cex['cv']:.2f} but no informative predicate found")
            break
        # Apply split
        left, right = tree.split(cex["leaf"], pred["feature"], pred["threshold"], pred["sample_value"])
        history.append(
            f"iter {i}: split leaf {cex['leaf']} (CV={cex['cv']:.2f}, n={len(cex['samples'])})  "
            f"on  {pred['description']}  "
            f"→ leaves {left}/{right}  "
            f"(variance: {pred['baseline_loss']:.0f} → {pred['loss']:.0f},  Δ={pred['reduction']:.0f})"
        )
    else:
        history.append(f"iter {max_iterations}: max_iterations reached")

    # Final state-class assignment
    for s in samples:
        s.state_class = tree.state_class(s.raw_features)
        s.next_state_class = tree.state_class(s.next_raw_features)
    return tree, history


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


def report_tree_structure(tree: PredicateTree) -> str:
    """Show the learned predicate tree, flagging core vs domain-specific splits."""
    lines = ["# Learned predicate tree"]
    n_core = sum(1 for f, _, _ in tree.splits.values() if is_core_feature(f))
    n_domain = len(tree.splits) - n_core
    lines.append(f"  {tree.n_leaves()} leaves, {len(tree.splits)} splits "
                 f"({n_core} core / {n_domain} domain-specific)")
    if not tree.splits:
        lines.append("  (no splits — only base-key leaves)")
        return "\n".join(lines)
    for split_node, (feature, threshold, desc) in sorted(tree.splits.items()):
        left, right = tree.children[split_node]
        tag = "CORE  " if is_core_feature(feature) else "DOMAIN"
        lines.append(f"  [{tag}] node {split_node}: split on '{desc}' → leaves {left}/{right}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", nargs="+", help="path(s) to tb-runs leaves; multiple = merged corpus")
    ap.add_argument("--json", action="store_true", help="emit samples + graph as JSON")
    ap.add_argument("--actions", action="store_true", help="show candidate actions per leaf")
    ap.add_argument("--cegar", action="store_true", help="run CEGAR refinement loop (v2)")
    ap.add_argument("--tol-cv", type=float, default=0.30, help="counterexample threshold (CV)")
    ap.add_argument("--max-iter", type=int, default=12, help="max CEGAR iterations")
    ap.add_argument("--max-depth", type=int, default=6, help="max predicate tree depth")
    ap.add_argument("--min-samples", type=int, default=4, help="min samples per leaf to consider splitting")
    args = ap.parse_args()

    samples: list[Sample] = []
    for d in args.run_dir:
        samples.extend(ingest_run_dir(Path(d)))
    if not samples:
        print("no samples ingested", file=sys.stderr)
        sys.exit(1)

    if args.cegar:
        tree, history = learn_abstraction(
            samples,
            max_iterations=args.max_iter,
            max_depth=args.max_depth,
            tol_cv=args.tol_cv,
            min_samples=args.min_samples,
        )
        graph = build_graph(samples, tree)
        print("# CEGAR refinement log")
        for line in history:
            print(f"  {line}")
        print()
        print(report_tree_structure(tree))
        print()
        print(report_variance_per_leaf(samples, tree))
        if args.actions:
            print()
            print(report_actions_per_leaf(samples, tree, graph))
        return

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
