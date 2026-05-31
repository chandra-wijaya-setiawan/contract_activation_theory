"""Domain-agnostic state/action canonicalization for the CEGAR-shaped advisor.

Shared by:
  - scripts/dijsktra_simulation.py (offline analysis)
  - cat/corpus.py (persistence)
  - cat/librarian.py (runtime queries)
  - cat/activator.py (live hint injection)

DOMAIN NOTE: action_intent_of() is currently SWE-bench/code-Python aware
(uses bash command classes etc.). When a second domain arrives, this
classification should move to per-capability registration metadata — see
PLAN.md v2.x "cross-domain working memory" note.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Helpers ─────────────────────────────────────────────────────────────────


def short_hash(s: Any) -> str:
    return hashlib.sha256(json.dumps(s, sort_keys=True, default=str).encode()).hexdigest()[:8]


# ── Bash command-class taxonomy (code-Python specific) ──────────────────────

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


# ── Action intent classification ────────────────────────────────────────────

_INVESTIGATE_TOOLS = {"read", "list", "rg", "fd", "outline",
                      "goto_def", "find_refs", "git_diff", "git_status"}
_MUTATE_TOOLS      = {"str_replace", "multi_edit", "apply_patch", "write"}
_VERIFY_BASH       = {"pytest", "django-runtests"}
_MUTATE_BASH       = {"pip-install", "pip-other", "pip-via-python", "mkdir", "fs-mutate"}


def action_intent_of(tool: str, args: dict) -> str:
    """Classify a tool call by reasoning role: investigate | mutate | verify | other."""
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


def is_state_changing(tool: str, args: dict, is_error: bool) -> bool:
    if is_error:
        return False
    return action_intent_of(tool, args) == "mutate"


def canonical_action_args(tool: str, args: dict) -> dict:
    """Strip path randomness, bucket numerics. Used to compute action_class."""
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


# ── Core features (domain-agnostic) ─────────────────────────────────────────

CORE_FEATURES = {
    "obligation_hash", "plan_idx", "last_outcome", "last_action_intent",
    "durable_change_count", "iter_since_durable_change",
    "verify_attempt_count", "investigate_attempt_count", "mutate_attempt_count",
    "n_unique_actions", "n_unique_errors", "attempted_actions",
}


def is_core_feature(feature_name: str) -> bool:
    return feature_name in CORE_FEATURES


# ── Sample (one observed transition) ────────────────────────────────────────


@dataclass
class Sample:
    trace_id: str
    iter_idx: int
    raw_features: dict
    tool: str
    action_args: dict
    action_klass: str
    weight: float
    durable_change: bool
    next_raw_features: dict
    reached_goal: bool
    on_goal_path: bool
    state_class: Optional[int] = None
    next_state_class: Optional[int] = None
    realized_cost_to_goal: float = math.inf


# ── Predicate tree (CEGAR-refinable abstraction) ────────────────────────────


def predicate_holds(raw: dict, feature: str, threshold) -> bool:
    v = raw.get(feature)
    if isinstance(v, (int, float)) and isinstance(threshold, (int, float)) and not isinstance(v, bool):
        return v > threshold
    if isinstance(v, list):
        return threshold in v
    return v == threshold


def describe_predicate(feature: str, threshold, sample_value) -> str:
    if isinstance(sample_value, (int, float)) and isinstance(threshold, (int, float)) and not isinstance(sample_value, bool):
        return f"{feature} > {threshold}"
    if isinstance(sample_value, list):
        return f"{threshold!r} in {feature}"
    return f"{feature} == {threshold!r}"


class PredicateTree:
    """Starts maximally coarse; CEGAR adds splits earned from data."""

    def __init__(self, base_keys=("obligation_hash", "plan_idx")):
        self.base_keys = base_keys
        self._base_to_root: dict[tuple, int] = {}
        self._next_id = 0
        self.splits: dict[int, tuple] = {}
        self.children: dict[int, tuple[int, int]] = {}
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
        desc = describe_predicate(feature, threshold,
                                  sample_value if sample_value is not None else threshold)
        self.splits[leaf_id] = (feature, threshold, desc)
        self.children[leaf_id] = (left, right)
        self._is_leaf[leaf_id] = False
        return left, right

    def n_leaves(self) -> int:
        return sum(1 for v in self._is_leaf.values() if v)


# ── Live feature extraction (used by activator at runtime) ──────────────────


@dataclass
class _FeatureState:
    """Mutable per-trajectory feature tracker; produces raw_features snapshots
    incrementally as events arrive."""
    obligation_hash: str = ""
    plan_step_idx: int = 0
    last_outcome: Optional[str] = None
    last_action_intent: Optional[str] = None
    durable_change_count: int = 0
    iter_since_durable_change: int = 0
    verify_attempt_count: int = 0
    investigate_attempt_count: int = 0
    mutate_attempt_count: int = 0
    last_tool: Optional[str] = None
    last_bash_class: Optional[str] = None
    env_facts: set = field(default_factory=set)
    attempted_actions: set = field(default_factory=set)
    attempted_bash_classes: set = field(default_factory=set)
    seen_errors: set = field(default_factory=set)

    def snapshot(self) -> dict:
        return {
            # core (agnostic)
            "obligation_hash":            self.obligation_hash,
            "plan_idx":                   self.plan_step_idx,
            "last_outcome":               self.last_outcome,
            "last_action_intent":         self.last_action_intent,
            "durable_change_count":       self.durable_change_count,
            "iter_since_durable_change":  self.iter_since_durable_change,
            "verify_attempt_count":       self.verify_attempt_count,
            "investigate_attempt_count":  self.investigate_attempt_count,
            "mutate_attempt_count":       self.mutate_attempt_count,
            "n_unique_actions":           len(self.attempted_actions),
            "n_unique_errors":            len(self.seen_errors),
            "attempted_actions":          sorted(self.attempted_actions),
            # domain-specific
            "env_facts":                  sorted(self.env_facts),
            "last_tool":                  self.last_tool,
            "last_bash_class":            self.last_bash_class,
            "attempted_bash_classes":     sorted(self.attempted_bash_classes),
        }

    def apply_tool_event(self, tool: str, args: dict, is_error: bool):
        """Update state after one tool call's result."""
        intent = action_intent_of(tool, args)
        self.attempted_actions.add(action_class(tool, args))
        if intent == "verify":      self.verify_attempt_count += 1
        elif intent == "investigate": self.investigate_attempt_count += 1
        elif intent == "mutate":      self.mutate_attempt_count += 1
        if tool == "bash":
            bc = bash_command_class(args.get("command", ""))
            self.attempted_bash_classes.add(bc)
            self.last_bash_class = bc
        self.last_tool = tool
        self.last_action_intent = intent
        if is_state_changing(tool, args, is_error):
            self.durable_change_count += 1
            self.iter_since_durable_change = 0
            cmd = (args.get("command") or "").lower()
            if "pip install pytest" in cmd:
                self.env_facts.add("pytest_installed")
        else:
            self.iter_since_durable_change += 1
        if is_error:
            self.seen_errors.add((tool, intent))
        self.last_outcome = "error" if is_error else "ok"

    def apply_criterion_ok(self, step_id: str):
        if step_id and step_id.startswith("s") and step_id[1:].isdigit():
            new_idx = int(step_id[1:])
            if new_idx > self.plan_step_idx:
                self.plan_step_idx = new_idx


def extract_raw_features_from_events(
    events: list[dict],
    obligation_text: str,
) -> dict:
    """Replay events into a _FeatureState; return the final snapshot.

    Used at runtime to get the current state_class before the next
    performer LLM call.
    """
    fs = _FeatureState(obligation_hash=short_hash(obligation_text))
    for ev in events:
        k = ev.get("kind")
        p = ev.get("payload", {}) or {}
        if k == "tool_result":
            tc = p.get("tool_call_id", "")
            if tc.startswith(("rep-", "oracle-")):
                continue
            # Find the matching tool_call to recover tool + args
            tc_id = tc
            tool, args = _lookup_tool_call(events, tc_id)
            if tool:
                fs.apply_tool_event(tool, args or {}, bool(p.get("is_error")))
        elif k == "criterion_check":
            if p.get("ok") is True and p.get("step_id"):
                fs.apply_criterion_ok(p["step_id"])
    return fs.snapshot()


def _lookup_tool_call(events: list[dict], tool_call_id: str) -> tuple[Optional[str], Optional[dict]]:
    for ev in events:
        if ev.get("kind") == "tool_call":
            p = ev.get("payload", {}) or {}
            if p.get("tool_call_id") == tool_call_id:
                return p.get("tool"), p.get("args") or {}
    return None, None
