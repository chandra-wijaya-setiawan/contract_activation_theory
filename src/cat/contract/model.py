"""Core contract types: Contract, Criterion, State, Constraints, Cost.

Design commitments (see DESIGN.md):
- Criteria are callable predicates over State, not LLM judgments
- The Contract holds its own lifecycle state; the Activator drives transitions
- Budget is a first-class Constraint, enforced mid-run
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Optional


class ContractState(str, Enum):
    LATENT = "latent"
    ELIGIBLE = "eligible"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    FULFILLED = "fulfilled"
    FAILED = "failed"
    EXPIRED = "expired"


AdmissionFailureType = Literal["boundary", "environment", "capacity"]
PolicyResponse = Literal["FAIL", "SUSPEND", "ESCALATE", "ASK_USER"]


@dataclass
class ContractPolicy:
    on_boundary_fail: PolicyResponse = "FAIL"
    on_environment_fail: PolicyResponse = "SUSPEND"
    on_capacity_fail: PolicyResponse = "FAIL"

    def response_for(self, failure_type: AdmissionFailureType) -> PolicyResponse:
        if failure_type == "boundary":
            return self.on_boundary_fail
        if failure_type == "environment":
            return self.on_environment_fail
        return self.on_capacity_fail


@dataclass
class AdmissionDecision:
    ok: bool
    failure_type: str = ""
    reason: str = ""
    grant: set[str] = field(default_factory=set)

    @staticmethod
    def reject(failure_type: str, reason: str) -> "AdmissionDecision":
        return AdmissionDecision(ok=False, failure_type=failure_type, reason=reason)

    @staticmethod
    def admit(grant: set[str]) -> "AdmissionDecision":
        return AdmissionDecision(ok=True, grant=grant)


@dataclass
class Evidence:
    summary: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class Criterion:
    id: str = ""
    description: str = ""
    predicate: Optional[Callable] = None
    is_oracle: bool = False
    satisfied: bool = False
    last_evidence: Optional[Evidence] = None

    def check(self, state: "State") -> bool:
        if self.predicate is None:
            return False
        ok, evidence = self.predicate(state)
        self.satisfied = ok
        self.last_evidence = evidence
        return ok


@dataclass
class Condition:
    description: str = ""
    check: Optional[Callable] = None


@dataclass
class Constraints:
    max_tokens_total: int = 200_000
    max_tool_calls: int = 200
    max_llm_calls: int = 100
    max_usd: float = 10.0
    max_wall_s: float = 600.0
    max_depth: int = 5
    deadline_ts: Optional[float] = None

    def check(self, cost: "Cost", depth: int, now_ts: float) -> Optional[str]:
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
class Cost:
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    usd: float = 0.0
    wall_s: float = 0.0

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    def add(self, other: "Cost") -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.tool_calls += other.tool_calls
        self.llm_calls += other.llm_calls
        self.usd += other.usd
        self.wall_s += other.wall_s


@dataclass
class Representation:
    key: str = ""
    value: str = ""
    evidence: str = ""


@dataclass
class State:
    workdir: Path = field(default_factory=lambda: Path("."))
    kv: dict = field(default_factory=dict)
    last_tool_result: Optional[dict] = None


@dataclass
class Contract:
    id: str = ""
    obligation: str = ""
    criteria: list[Criterion] = field(default_factory=list)
    representations: list[Representation] = field(default_factory=list)
    constraints: Constraints = field(default_factory=Constraints)
    conditions: list[Condition] = field(default_factory=list)
    policy: ContractPolicy = field(default_factory=ContractPolicy)
    state: ContractState = ContractState.LATENT
    state_reason: str = ""
    cost: Cost = field(default_factory=Cost)
    grant: set[str] = field(default_factory=set)
    capabilities: list[str] = field(default_factory=list)
    depth: int = 0
    suspend_reason: str = ""
    re_entry_condition: str = ""

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
            depth=depth,
        )

    def all_criteria_met(self) -> bool:
        return bool(self.criteria) and all(c.satisfied for c in self.criteria)

    def conditions_met(self, state: State) -> bool:
        if not self.conditions:
            return True
        return all(
            (cond.check(state) if callable(cond.check) else False)
            for cond in self.conditions
        )

    def can_transition(self, to: ContractState) -> bool:
        allowed = _ALLOWED_TRANSITIONS.get(self.state, set())
        return to in allowed


_ALLOWED_TRANSITIONS: dict[ContractState, set[ContractState]] = {
    ContractState.LATENT: {
        ContractState.ELIGIBLE,
        ContractState.SUSPENDED,
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
    """Wrap a bool-returning predicate with default Evidence."""
    def _wrapped(state: State) -> tuple[bool, Evidence]:
        ok = predicate(state)
        detail = evidence_fn(state) if evidence_fn else {}
        return ok, Evidence(summary=("PASS" if ok else "FAIL") + f": {description}", detail=detail)
    return Criterion(id=id, description=description, predicate=_wrapped)
