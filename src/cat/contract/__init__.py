"""Contract lifecycle — the heart of CAT.

The contract package owns the formal structure that every task must satisfy:
  - model.py          Contract, Criterion, State, Constraints, Cost, ContractState
  - schemas.py        All Pydantic data shapes (Hypothesis, PerformerTask, Knowledge, etc.)
  - admission.py      Admission gate logic (boundary, environment, capacity checks)

Design commitments (see DESIGN.md):
  - Criteria are callable predicates over State, not LLM judgments
  - The Contract holds its own lifecycle state; the Activator drives transitions
  - Budget is a first-class Constraint, enforced mid-run
"""
from cat.contract.model import (
    Contract,
    ContractState,
    Criterion,
    Condition,
    Constraints,
    Cost,
    State,
    Representation,
    Evidence,
    ContractPolicy,
    AdmissionDecision,
)
from cat.contract.schemas import (
    HypothesisSchema,
    PerformerTaskSchema,
    PerformerPlanSchema,
    PerformerStep,
    KnowledgeSchema,
    AttestorV3Schema,
    InvocationSchema,
    OutcomeSchema,
    ExecutionSchema,
    ExpectsSchema,
)

__all__ = [
    "Contract",
    "ContractState",
    "Criterion",
    "Condition",
    "Constraints",
    "Cost",
    "State",
    "Representation",
    "Evidence",
    "ContractPolicy",
    "AdmissionDecision",
    "HypothesisSchema",
    "PerformerTaskSchema",
    "PerformerPlanSchema",
    "PerformerStep",
    "KnowledgeSchema",
    "AttestorV3Schema",
    "InvocationSchema",
    "OutcomeSchema",
    "ExecutionSchema",
    "ExpectsSchema",
]
