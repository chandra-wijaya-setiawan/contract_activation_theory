"""Runtime LIBRARIAN: queries the corpus for advisor recommendations.

At Step 5 SELECT, the activator asks the LIBRARIAN: "from this state, what
do prior trajectories suggest?" The LIBRARIAN walks the CEGAR-refined
PredicateTree, looks up the current state_class, and returns the action
with lowest cost-to-goal (Dijkstra recommendation) — or `None` if the
state is cold (not in the corpus) or disconnected (no path to GOAL).

This is the LIGHT-TOUCH integration: returned recommendations get injected
into the performer's context as a hint message; the LLM still decides the
actual action. DEFER/ADVISOR/ELECTION arbitration is "next hardening" per
PLAN.md.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path
from typing import Optional

from cat._legacy.corpus import Corpus
from cat._legacy.state_class import PredicateTree, Sample, action_class, is_core_feature


GOAL_NODE = -1


@dataclass
class ScoredCandidate:
    """One candidate action ranked by the LIBRARIAN for ELECTION.

    `defer=True` means the corpus has no information for this state/action
    pair — the score should be treated as a no-confidence vote. ELECTION
    falls through to the LLM's preferred candidate (the first one emitted)
    when all candidates defer.
    """
    index: int                          # which LLM-emitted candidate this is
    tool: str
    canonical_args: dict
    action_klass: str
    state_class: int                    # current state (for trace event)
    expected_cost: Optional[float] = None       # Q̂(s, a) — mean observed cost
    next_state_d_star: Optional[float] = None   # d*(s') — distance from successor
    total_score: Optional[float] = None         # expected_cost + d*(next)
    confidence_n: int = 0                       # times this (s, a) was observed
    defer: bool = True                          # True when corpus has no signal


@dataclass
class Recommendation:
    """A LIBRARIAN suggestion for the next action."""
    action_klass: str           # short hash; for trace correlation
    tool: str
    canonical_args: dict
    expected_cost: float        # Q̂(s, a) — mean tokens for this edge
    next_state_d_star: float    # d*(s') — distance from successor to GOAL
    total_score: float           # expected_cost + next_state_d_star
    confidence_n: int            # number of prior observations
    state_class: int             # current state_class id
    n_alternatives: int           # how many candidate actions seen from this state

    def format_hint(self) -> str:
        """Human-readable advice line for performer context injection."""
        args_str = ""
        for k, v in self.canonical_args.items():
            if isinstance(v, str) and len(v) > 60:
                v = v[:60] + "…"
            args_str += f"{k}={v} "
        return (
            f"prior runs at this state suggest: {self.tool}({args_str.strip()}) "
            f"— expected cost {self.expected_cost:.0f}, total path-to-goal {self.total_score:.0f}, "
            f"based on n={self.confidence_n} observations across {self.n_alternatives} candidate actions"
        )


class Librarian:
    """Loads the corpus, builds a tree, answers state→action queries."""

    def __init__(
        self,
        corpus_path: Path,
        max_iter: int = 30,
        max_depth: int = 8,
        tol_cv: float = 0.30,
        min_samples: int = 4,
    ):
        self.corpus = Corpus(corpus_path)
        self._samples: Optional[list[Sample]] = None
        self._tree: Optional[PredicateTree] = None
        self._graph: Optional[dict] = None
        self._d_star: dict[int, float] = {}
        self._cfg = dict(max_iter=max_iter, max_depth=max_depth,
                         tol_cv=tol_cv, min_samples=min_samples)

    def is_ready(self) -> bool:
        return self._tree is not None and self._graph is not None

    def stats(self) -> dict:
        s = self.corpus.stats()
        s["tree_leaves"] = self._tree.n_leaves() if self._tree else 0
        s["reachable_states"] = sum(1 for d in self._d_star.values() if d != math.inf)
        return s

    def refresh(self) -> None:
        """Reload samples, rebuild tree + graph + d*. Call after writeback."""
        self._samples = self.corpus.load_all_samples()
        if not self._samples:
            self._tree = None
            self._graph = None
            self._d_star = {}
            return
        # Build CEGAR-refined tree
        self._tree, _history = _learn_abstraction(self._samples, **self._cfg)
        # Build graph
        self._graph = _build_graph(self._samples, self._tree)
        # Compute d* via backward Dijkstra
        self._d_star = _backward_dijkstra(self._graph)

    def score_candidates(
        self,
        candidates: list[dict],
        raw_features: dict,
    ) -> list[ScoredCandidate]:
        """Score each LLM-emitted candidate action by corpus-derived shortest
        path. Each candidate is a dict {'tool': str, 'args': dict} (the
        original args from the LLM's tool_call, not canonicalized).

        Returns one ScoredCandidate per input candidate, in the same order.

        For each candidate:
          - canonicalize args via state_class.canonical_action_args
          - look up (state_class, action_class) edges in the graph
          - if found: total_score = mean expected_cost + d*(next_state)
          - if not found OR successor disconnected from GOAL: defer=True

        ELECTION picks the lowest total_score among non-defer candidates;
        if all defer, ELECTION returns the first (LLM's preferred).
        """
        from cat._legacy.state_class import canonical_action_args
        out: list[ScoredCandidate] = []
        if self._tree is None or self._graph is None:
            sc = -1
        else:
            sc = self._tree.state_class(raw_features)
        out_edges = self._graph.get("out_edges", {}).get(sc, []) if self._graph else []
        # Build a lookup: action_klass → (mean_w, next_sc, n)
        edges_by_action: dict[str, tuple] = {}
        for akls, next_sc, w_mean, n in out_edges:
            if akls == "__goal__":
                continue
            # If the same action_klass appears with multiple next_sc, prefer
            # the one with lowest d*(next) — that's what Dijkstra would pick.
            if akls not in edges_by_action:
                edges_by_action[akls] = (w_mean, next_sc, n)
            else:
                # Already have one; keep whichever leads to cheaper next state
                _w, prev_next, _n = edges_by_action[akls]
                if self._d_star.get(next_sc, math.inf) < self._d_star.get(prev_next, math.inf):
                    edges_by_action[akls] = (w_mean, next_sc, n)

        for i, cand in enumerate(candidates):
            tool = cand.get("tool", "") or cand.get("name", "")
            raw_args = cand.get("args", {}) or cand.get("arguments", {}) or {}
            ca = canonical_action_args(tool, raw_args)
            from cat._legacy.state_class import action_class
            akls = action_class(tool, raw_args)
            sc_obj = ScoredCandidate(
                index=i, tool=tool, canonical_args=ca,
                action_klass=akls, state_class=sc,
            )
            if akls in edges_by_action:
                w_mean, next_sc, n = edges_by_action[akls]
                d_next = self._d_star.get(next_sc, math.inf)
                if d_next != math.inf:
                    sc_obj.expected_cost = w_mean
                    sc_obj.next_state_d_star = d_next
                    sc_obj.total_score = w_mean + d_next
                    sc_obj.confidence_n = n
                    sc_obj.defer = False
            out.append(sc_obj)
        return out

    def recommend(self, raw_features: dict) -> Optional[Recommendation]:
        """Return the best action for this state, or None if cold/disconnected.

        Best = lowest (expected_cost + next_state_d_star). The advisor is
        purely advisory — caller injects this as a hint; LLM still picks.
        """
        if self._tree is None or self._graph is None:
            return None
        sc = self._tree.state_class(raw_features)
        out_edges = self._graph.get("out_edges", {}).get(sc, [])
        if not out_edges:
            return None  # cold or no edges from this state
        # For each candidate action from this state, compute score = w + d*(next)
        best = None
        best_score = math.inf
        action_to_samples: dict[str, list[Sample]] = defaultdict(list)
        for s in self._samples or []:
            if s.state_class == sc:
                action_to_samples[s.action_klass].append(s)
        n_alts = len({a for (a, _d, _w, _n) in out_edges})
        for action_klass, next_sc, w_mean, n in out_edges:
            if action_klass == "__goal__":
                continue
            d_next = self._d_star.get(next_sc, math.inf)
            if d_next == math.inf:
                continue  # successor disconnected from GOAL
            score = w_mean + d_next
            if score < best_score:
                best_score = score
                # Pick representative sample for tool/args info
                rep = action_to_samples[action_klass][0] if action_to_samples[action_klass] else None
                if rep is None:
                    continue
                best = Recommendation(
                    action_klass=action_klass,
                    tool=rep.tool,
                    canonical_args=rep.action_args,
                    expected_cost=w_mean,
                    next_state_d_star=d_next,
                    total_score=score,
                    confidence_n=n,
                    state_class=sc,
                    n_alternatives=n_alts,
                )
        return best


# ── Internals (lifted from scripts/dijsktra_simulation.py) ──────────────────


def _learn_abstraction(samples, max_iter, max_depth, tol_cv, min_samples):
    """The CEGAR loop. Returns (tree, history). Internal — caller should
    use Librarian.refresh()."""
    tree = PredicateTree()
    history: list[str] = []

    for i in range(max_iter):
        for s in samples:
            s.state_class = tree.state_class(s.raw_features)
            s.next_state_class = tree.state_class(s.next_raw_features)
        cex = _find_counterexample(samples, tol_cv, min_samples)
        if cex is None:
            history.append(f"iter {i}: no counterexample above CV={tol_cv}")
            break
        if _depth_of(tree, cex["leaf"]) >= max_depth:
            history.append(f"iter {i}: leaf at max_depth")
            break
        pred = _choose_predicate(cex["samples"], cex["kind"], tree.base_keys)
        if pred is None:
            history.append(f"iter {i}: no informative predicate")
            break
        tree.split(cex["leaf"], pred["feature"], pred["threshold"], pred.get("sample_value"))
        history.append(f"iter {i}: split leaf {cex['leaf']} on {pred['description']}")
    for s in samples:
        s.state_class = tree.state_class(s.raw_features)
        s.next_state_class = tree.state_class(s.next_raw_features)
    return tree, history


def _find_counterexample(samples, tol_cv, min_samples):
    by_leaf = defaultdict(list)
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


def _variance(values):
    if len(values) < 2: return 0.0
    m = sum(values) / len(values)
    return sum((v - m) ** 2 for v in values) / len(values)


def _choose_predicate(samples, kind, base_keys):
    from cat._legacy.state_class import predicate_holds, describe_predicate
    y = [s.realized_cost_to_goal for s in samples] if kind == "value_split" \
        else [1.0 if s.durable_change else 0.0 for s in samples]
    candidates = defaultdict(set)
    for s in samples:
        for fkey, fval in s.raw_features.items():
            if fkey in base_keys: continue
            if isinstance(fval, (int, float)) and not isinstance(fval, bool):
                candidates[fkey].add(fval)
            elif isinstance(fval, str) or fval is None:
                candidates[fkey].add(fval)
            elif isinstance(fval, list):
                for item in fval: candidates[fkey].add(item)
            elif isinstance(fval, bool):
                candidates[fkey].add(fval)
    baseline = _variance(y)
    best = None; best_loss = baseline
    for feature, thresholds in candidates.items():
        for threshold in thresholds:
            left_y, right_y = [], []
            sample_val = None
            for s, y_i in zip(samples, y):
                v = s.raw_features.get(feature)
                if sample_val is None and v is not None:
                    sample_val = v
                if predicate_holds(s.raw_features, feature, threshold):
                    left_y.append(y_i)
                else:
                    right_y.append(y_i)
            if not left_y or not right_y: continue
            n = len(y)
            loss = (len(left_y)/n)*_variance(left_y) + (len(right_y)/n)*_variance(right_y)
            if loss < best_loss:
                best_loss = loss
                best = {"feature": feature, "threshold": threshold,
                        "sample_value": sample_val,
                        "description": describe_predicate(feature, threshold,
                                                          sample_val if sample_val is not None else threshold)}
    return best


def _depth_of(tree, node_id):
    for root in tree._base_to_root.values():
        d = _depth_walk(tree, root, node_id, 0)
        if d is not None:
            return d
    return -1


def _depth_walk(tree, current, target, depth):
    if current == target: return depth
    if current not in tree.splits: return None
    l, r = tree.children[current]
    for child in (l, r):
        d = _depth_walk(tree, child, target, depth + 1)
        if d is not None: return d
    return None


def _build_graph(samples, tree):
    edge_buckets: dict[tuple, list[float]] = defaultdict(list)
    for s in samples:
        s.state_class = tree.state_class(s.raw_features)
        s.next_state_class = tree.state_class(s.next_raw_features)
        key = (s.state_class, s.action_klass, s.next_state_class)
        edge_buckets[key].append(s.weight)
    by_trace: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_trace[s.trace_id].append(s)
    pass_terminal_states: set[int] = set()
    for trace_id, ss in by_trace.items():
        if ss[-1].reached_goal:
            pass_terminal_states.add(ss[-1].next_state_class)
    out_edges: dict[int, list[tuple]] = defaultdict(list)
    in_edges: dict[int, list[tuple]] = defaultdict(list)
    for (src, akls, dst), weights in edge_buckets.items():
        w_mean = sum(weights) / len(weights)
        out_edges[src].append((akls, dst, w_mean, len(weights)))
        in_edges[dst].append((src, w_mean))
    for t in pass_terminal_states:
        out_edges[t].append(("__goal__", GOAL_NODE, 0.0, 1))
        in_edges[GOAL_NODE].append((t, 0.0))
    return {"out_edges": dict(out_edges), "in_edges": dict(in_edges),
            "pass_terminal_states": pass_terminal_states}


def _backward_dijkstra(graph):
    in_edges = graph["in_edges"]
    dist: dict[int, float] = {GOAL_NODE: 0.0}
    pq = [(0.0, GOAL_NODE)]
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
