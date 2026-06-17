"""Backward-compatibility shim — imports from cat.contract.model."""
from cat.contract.model import (
    AdmissionDecision,
    Condition,
    Constraints,
    Contract,
    ContractPolicy,
    ContractState,
    Cost,
    Criterion,
    Evidence,
    Representation,
    State,
    deterministic_criterion,
)

__all__ = [
    "AdmissionDecision",
    "Condition",
    "Constraints",
    "Contract",
    "ContractPolicy",
    "ContractState",
    "Cost",
    "Criterion",
    "Evidence",
    "Representation",
    "State",
    "deterministic_criterion",
]
