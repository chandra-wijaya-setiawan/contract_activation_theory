"""Contract Activation Theory (CAT) — contract-disciplined agent runtime.

Public API:
    from cat import Contract, Activator, LLMKernel, Recorder
"""
from cat.contract.model import (
    Contract,
    ContractState,
    Criterion,
    Constraints,
    Cost,
)
from cat.agent.hypothesis import HypothesisLoopActivator
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
