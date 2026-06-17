"""Contract Activation Theory (CAT) — contract-disciplined agent runtime.

Public surface kept small; deeper modules expose internals for tests and tooling.

Usage:
    from cat import Contract, Activator, HypothesisLoopActivator
    from cat.contract import ContractState, Criterion
    from cat.llm import LLMKernel

See design.v3.md for the full architecture spec.
See refactor_plan.md for the module layout and migration status.
"""
from cat.contract.model import (
    Contract,
    ContractState,
    Criterion,
    Constraints,
    Cost,
)
from cat.engine.activator import HypothesisLoopActivator
from cat.trace import Recorder, TraceEvent, Trajectory

__all__ = [
    "Contract",
    "ContractState",
    "Criterion",
    "Constraints",
    "Cost",
    "HypothesisLoopActivator",
    "Recorder",
    "TraceEvent",
    "Trajectory",
]
__version__ = "0.3.0"
