"""Admission gate logic — validates a contract before discharge.

Extracted from BaseActivator._admit() for reuse and testability.

See design.v3.md §"Step 4: ACTIVATE".
"""
from __future__ import annotations

from typing import Any

from cat.capability.registry import CapabilityRegistry
from cat.contract.model import (
    AdmissionDecision,
    Contract,
    State,
)


def check_admission(
    contract: Contract,
    state: State,
    registry: CapabilityRegistry,
    run: Any = None,
) -> AdmissionDecision:
    """Validate a contract is ready for discharge.

    Checks:
      (a) Boundary — is the contract well-formed?
      (b) Environment — does the world match the contract's assumptions?
      (c) Capacity — is there room to run?
      (d) Grant — derive granted capability set.
    """
    from cat.agent.base import ActivatorRun
    if run is None:
        run = ActivatorRun(contract=contract, state=state)

    if not (contract.obligation or "").strip():
        return AdmissionDecision.reject("boundary", "empty obligation")
    if not contract.criteria:
        return AdmissionDecision.reject("boundary", "no criteria declared")
    if not run.plan:
        return AdmissionDecision.reject("boundary", "empty plan from DECOMPOSE")
    cons = contract.constraints
    if cons.max_tokens_total <= 0 or cons.max_tool_calls <= 0 or cons.max_llm_calls <= 0:
        return AdmissionDecision.reject("boundary", "non-positive constraint bounds")
    import time as _time
    now = _time.time()
    if cons.deadline_ts is not None and now > cons.deadline_ts:
        return AdmissionDecision.reject("boundary", "deadline already passed")
    if not contract.conditions_met(state):
        return AdmissionDecision.reject("environment", "declared conditions not met")
    for cap_name in contract.capabilities or []:
        if registry.get(cap_name) is None:
            return AdmissionDecision.reject("environment", f"capability '{cap_name}' not in registry")
    violation = cons.check(contract.cost, contract.depth, now)
    if violation:
        return AdmissionDecision.reject("capacity", violation)
    grant = set(registry.names())
    if contract.constraints.deadline_ts is None and contract.constraints.max_wall_s > 0:
        contract.constraints.deadline_ts = _time.time() + contract.constraints.max_wall_s
    return AdmissionDecision.admit(grant)
