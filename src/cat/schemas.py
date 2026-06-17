"""Backward-compatibility shim — imports from cat.contract.schemas."""
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
