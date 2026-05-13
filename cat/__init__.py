"""Contract Activation Theory (CAT) — contract-disciplined agent runtime.

Public surface kept small; deeper modules expose internals for tests and tooling.
"""
from cat.contracts import (
    Contract,
    ContractState,
    Criterion,
    Constraints,
    Cost,
)
from cat.activator import Activator
from cat.tasks import TaskSpec, BudgetCaps, OracleSpec, Outcome
from cat.trace import Recorder, TraceEvent, Trajectory

__all__ = [
    "Contract",
    "ContractState",
    "Criterion",
    "Constraints",
    "Cost",
    "Activator",
    "TaskSpec",
    "BudgetCaps",
    "OracleSpec",
    "Outcome",
    "Recorder",
    "TraceEvent",
    "Trajectory",
]
__version__ = "0.0.1"
