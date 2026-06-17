"""Back-compat shim — re-exports new schema names under their old aliases.

After R26 the canonical schema library lives in `cat/schemas.py` with
`Schema`-suffixed names. This file keeps the old names available so
external code and old trace events that reference the original symbols
keep working. Delete this file once no consumer references the old names.

See `design_schema.md` for the naming convention.
"""
from cat.schemas import (
    # Type aliases (Literal types) — unchanged
    Intent,
    PhaseName,
    VerifierOutcome,
    AttributionStatus,
    AttributionLayer,
    # Renamed schemas
    ExpectsSchema as StepExpects,
    ExecutionSchema as PlanStep,
    PhaseSchema as Phase,
    PhasedPlanSchema as PhasedPlan,
    AttestorSchema as AttestorVerdict,
    ResolverSchema as ResolverVerdict,
    LayerStatusSchema as LayerStatus,
    AttributionSchema as AttributionVerdict,
    InvocationSchema as ToolInvocation,
    OutcomeSchema as PerformerOutcome,
    PatcherSchema as PlanPatcherOps,
    # Op aliases (these kept their original names; re-exported for convenience)
    _OpBaseSchema as _PlanPatcherOpBase,
    PatcherOp as PlanPatcherOp,
    CommitOp,
    CheckoutOp,
    DropOp,
    CommitNewOp,
    RefineOp,
    BranchOp,
    MergeOp,
)

__all__ = [
    "Intent", "PhaseName", "VerifierOutcome",
    "AttributionStatus", "AttributionLayer",
    "StepExpects", "PlanStep", "Phase", "PhasedPlan",
    "AttestorVerdict", "ResolverVerdict",
    "LayerStatus", "AttributionVerdict",
    "ToolInvocation", "PerformerOutcome",
    "PlanPatcherOps", "_PlanPatcherOpBase", "PlanPatcherOp",
    "CommitOp", "CheckoutOp", "DropOp", "CommitNewOp",
    "RefineOp", "BranchOp", "MergeOp",
]
