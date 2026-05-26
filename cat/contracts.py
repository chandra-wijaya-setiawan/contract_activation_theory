"""Core CAT types: Contract, Criterion, Condition, Constraints, Cost, state machine.

Design commitments (see DESIGN.md):
- Criteria are *callable predicates* over a State snapshot, not LLM judgments.
  A subjective criterion delegates to a sub-contract whose criterion *is*
  deterministic (the judge's structured verdict). The safety floor: closure
  always rests on a predicate returning True with evidence attached.
- The Contract holds its own lifecycle state. The Activator drives transitions;
  Contracts don't drive themselves.
- Budget is a first-class Constraint and is enforced by the Activator mid-run,
  not after the fact (carried-gap #2 from the prior project).
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class ContractState(str, Enum):
    LATENT = "latent"          # received, conditions not yet met
    ELIGIBLE = "eligible"      # conditions met, awaiting activation
    ACTIVE = "active"          # currently being fulfilled
    SUSPENDED = "suspended"    # paused awaiting child / external event
    FULFILLED = "fulfilled"    # criteria met, closed
    FAILED = "failed"          # constraint violated or unrecoverable error
    EXPIRED = "expired"        # TTL / deadline passed without fulfillment


@dataclass
class Cost:
    """Cumulative cost on a contract. Activator updates after each capability call."""
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    wall_s: float = 0.0
    usd: float = 0.0
    llm_calls: int = 0

    def add(self, other: "Cost") -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.tool_calls += other.tool_calls
        self.wall_s += other.wall_s
        self.usd += other.usd
        self.llm_calls += other.llm_calls

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class Constraints:
    """Hard limits enforced by the Activator. Exceeding any → FAILED."""
    max_tokens_total: int = 200_000
    max_tool_calls: int = 50
    max_llm_calls: int = 30
    max_wall_s: float = 600.0
    max_usd: float = 2.0
    max_depth: int = 4              # max nested decomposition depth
    deadline_ts: Optional[float] = None   # absolute UNIX time

    def check(self, cost: Cost, depth: int, now_ts: float) -> Optional[str]:
        """Return a violation reason if any limit is exceeded; else None."""
        if cost.tokens_total > self.max_tokens_total:
            return f"tokens_total {cost.tokens_total} > {self.max_tokens_total}"
        if cost.tool_calls > self.max_tool_calls:
            return f"tool_calls {cost.tool_calls} > {self.max_tool_calls}"
        if cost.llm_calls > self.max_llm_calls:
            return f"llm_calls {cost.llm_calls} > {self.max_llm_calls}"
        if cost.wall_s > self.max_wall_s:
            return f"wall_s {cost.wall_s:.1f} > {self.max_wall_s}"
        if cost.usd > self.max_usd:
            return f"usd {cost.usd:.4f} > {self.max_usd}"
        if depth > self.max_depth:
            return f"depth {depth} > {self.max_depth}"
        if self.deadline_ts is not None and now_ts > self.deadline_ts:
            return f"deadline passed ({now_ts:.1f} > {self.deadline_ts:.1f})"
        return None


@dataclass
class Evidence:
    """What proves a criterion was satisfied (or refuted)."""
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


# A predicate takes the world State (an opaque dict-like) and returns (satisfied, evidence).
CriterionPredicate = Callable[["State"], tuple[bool, Evidence]]


@dataclass
class Criterion:
    """A measurable predicate on world state.

    `check(state)` is the only path to satisfaction — even a "LLM-judge" criterion
    delegates to a child contract that *produces* state observable by a predicate.

    is_oracle=True marks a test-runner criterion as authoritative: when any oracle
    criterion exists, the Activator runs ONLY oracle criteria during VERIFY and
    skips LLM-judge criteria entirely. This ensures the contract closes on real
    test results, not on model opinion.
    """
    id: str
    description: str
    predicate: CriterionPredicate
    satisfied: bool = False
    last_evidence: Optional[Evidence] = None
    is_oracle: bool = False

    def check(self, state: "State") -> bool:
        ok, ev = self.predicate(state)
        self.satisfied = ok
        self.last_evidence = ev
        return ok


# A Condition gates *activation* (not closure); same signature as a predicate.
Condition = CriterionPredicate


@dataclass
class State:
    """Opaque world snapshot. Capabilities mutate it via well-defined hooks.

    For v0.0.1 we expose just `workdir`, `last_tool_result`, and a free-form
    `kv` dict so capabilities can stash arbitrary observations. Predicates
    read from these without coupling to the agent's internal LLM messages.
    """
    workdir: Path
    last_tool_result: Optional[dict[str, Any]] = None
    kv: dict[str, Any] = field(default_factory=dict)


@dataclass
class Representation:
    """A verified fact about the current world state, established before decomposition.

    Representations ground the obligation in reality — a contract formed without
    them is formally incomplete (Hart & Moore: obligations contingent on unverified
    state of nature). The Representer runs a fixed probe set and populates these
    before the planner sees the obligation.

    key      — what fact is being established (e.g. "workdir", "test_command")
    value    — concise verified value (first meaningful line of evidence)
    evidence — raw probe output that warrants the claim
    probed_at — UNIX timestamp of the probe run
    """
    key: str
    value: str
    evidence: str
    probed_at: float = field(default_factory=time.time)


@dataclass
class Contract:
    """A first-class obligation. The unit of discipline.

    Lifecycle:
        LATENT → ELIGIBLE → ACTIVE → (FULFILLED | FAILED | SUSPENDED | EXPIRED)

    The Activator owns transitions; Contract objects expose state but do not
    drive themselves. This keeps the runtime small and inspectable.
    """
    id: str
    obligation: str
    criteria: list[Criterion]
    conditions: list[Condition] = field(default_factory=list)
    constraints: Constraints = field(default_factory=Constraints)
    capabilities: list[str] = field(default_factory=list)  # names registered with Activator
    parent_id: Optional[str] = None
    children: list[str] = field(default_factory=list)
    state: ContractState = ContractState.LATENT
    cost: Cost = field(default_factory=Cost)
    created_at: float = field(default_factory=time.time)
    priority: int = 0
    state_reason: Optional[str] = None    # why we transitioned (last)
    depth: int = 0                        # nesting depth from root
    representations: list[Representation] = field(default_factory=list)  # Step 2: REPRESENT

    @classmethod
    def new(
        cls,
        obligation: str,
        criteria: list[Criterion],
        *,
        constraints: Optional[Constraints] = None,
        capabilities: Optional[list[str]] = None,
        parent_id: Optional[str] = None,
        depth: int = 0,
    ) -> "Contract":
        return cls(
            id=uuid.uuid4().hex[:12],
            obligation=obligation,
            criteria=criteria,
            constraints=constraints or Constraints(),
            capabilities=capabilities or [],
            parent_id=parent_id,
            depth=depth,
        )

    # ── Lifecycle helpers (the Activator calls these; Contracts never self-mutate state outside these methods) ──

    def all_criteria_met(self) -> bool:
        return bool(self.criteria) and all(c.satisfied for c in self.criteria)

    def conditions_met(self, state: State) -> bool:
        """All activation conditions hold (vacuously true if none)."""
        if not self.conditions:
            return True
        return all(cond(state)[0] for cond in self.conditions)

    def can_transition(self, to: ContractState) -> bool:
        """Validate a state transition. Forbids skipping or going backwards."""
        allowed = _ALLOWED_TRANSITIONS.get(self.state, set())
        return to in allowed


_ALLOWED_TRANSITIONS: dict[ContractState, set[ContractState]] = {
    ContractState.LATENT: {
        ContractState.ELIGIBLE,
        ContractState.FAILED,
        ContractState.EXPIRED,
    },
    ContractState.ELIGIBLE: {
        ContractState.ACTIVE,
        ContractState.FAILED,
        ContractState.EXPIRED,
        ContractState.SUSPENDED,
    },
    ContractState.ACTIVE: {
        ContractState.FULFILLED,
        ContractState.FAILED,
        ContractState.SUSPENDED,
        ContractState.EXPIRED,
    },
    ContractState.SUSPENDED: {
        ContractState.ELIGIBLE,
        ContractState.ACTIVE,
        ContractState.FAILED,
        ContractState.EXPIRED,
    },
    # Terminal states — no further transitions.
    ContractState.FULFILLED: set(),
    ContractState.FAILED: set(),
    ContractState.EXPIRED: set(),
}


def deterministic_criterion(
    id: str,
    description: str,
    predicate: Callable[[State], bool],
    evidence_fn: Optional[Callable[[State], dict[str, Any]]] = None,
) -> Criterion:
    """Helper: wrap a bool-returning predicate with default Evidence."""
    def _wrapped(state: State) -> tuple[bool, Evidence]:
        ok = predicate(state)
        detail = evidence_fn(state) if evidence_fn else {}
        return ok, Evidence(summary=("PASS" if ok else "FAIL") + f": {description}", detail=detail)
    return Criterion(id=id, description=description, predicate=_wrapped)
