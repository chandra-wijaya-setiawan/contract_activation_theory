"""CAT schema library — single source of truth for all Pydantic data shapes.

Naming convention (see design_schema.md):
  - Pydantic BaseModel → ends in `Schema`
  - Runtime dataclass → no suffix
  - PlanPatcher op → ends in `Op` (each Op is also a Schema)

The cat/plan_schema.py shim re-exports old names for back-compat.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


Intent = Literal["investigate", "design", "mutate", "verify", "other"]
PhaseName = Literal["MAP", "DESIGN", "IMPLEMENT", "VERIFY", "OTHER"]
VerifierOutcome = Literal["PASS", "FAIL", "N/A"]


class ExpectsSchema(BaseModel):
    """The planner's predictions for what should be true when this step is done.

    The attestor judges step completion against these fields. Each field is
    one prediction-vs-actual comparison (see docs/iteration.md §8 surprise
    vector).
    """

    durable_change: bool = Field(
        False,
        description=(
            "True iff this step is expected to modify files. Mechanically "
            "verified by checking git diff after the action."
        ),
    )
    files_modified: list[str] = Field(
        default_factory=list,
        description=(
            "Expected modified file paths (only meaningful for mutate steps; "
            "leave empty for investigate / design / verify)."
        ),
    )
    verifier_outcome: VerifierOutcome = Field(
        "N/A",
        description=(
            "Expected verifier result for verify-intent steps. "
            "Use 'N/A' when this step is not a verify step."
        ),
    )
    result: str = Field(
        "",
        description=(
            "Prose statement of what should be true when this step is done. "
            "The attestor judges step completion against THIS criterion. "
            "MUST include negative-result possibilities where applicable, e.g.: "
            "'is_zero is defined in numbers.py OR confirmed not defined there "
            "(implying it is inherited from a parent class).' "
            "A search that returns 0 matches IS a valid evidence of negative "
            "results if the step asked to verify a presence."
        ),
    )


class ExecutionSchema(BaseModel):
    """One step in the plan tree."""

    description: str = Field(
        description="Imperative task: what the agent should do this turn."
    )
    intent: Intent = Field(
        description=(
            "Which family this step belongs to: investigate (read/search), "
            "design (decide what to change), mutate (write to a file), "
            "verify (run tests), or other."
        )
    )
    suggested_tool: Literal[
        "bash", "read", "write", "list",
        "str_replace", "multi_edit", "apply_patch",
        "rg", "fd", "outline", "goto_def", "find_refs",
        "git_diff", "git_status",
    ] | None = Field(
        None,
        description=(
            "The tool you (Stipulator) recommend the Performer use for this step. "
            "Pick from the allowed set (enforced by validator). "
            "Leave null only if no clear tool exists (e.g., pure design steps)."
        ),
    )
    suggested_args: dict = Field(
        default_factory=dict,
        description=(
            "The tool arguments you recommend. The Performer sees this as a hint "
            "and may follow or adapt. Examples: {'path': '/testbed/foo.py'} for read, "
            "{'pattern': 'def is_zero', 'paths': '/testbed/sympy/core/expr.py'} for rg, "
            "{'path': '/testbed/foo.py', 'old_str': 'return False', 'new_str': 'return None'} "
            "for str_replace. For mutate steps, BE SPECIFIC: include the exact target file, "
            "the exact old_str pattern, the exact new_str replacement. "
            "May be null/omitted for design steps that have no associated tool — "
            "the validator coerces null to {} so downstream consumers can rely on dict-like access."
        ),
    )

    @field_validator("suggested_args", mode="before")
    @classmethod
    def _coerce_none_to_empty_dict(cls, v):
        # Stipulator system prompt allows null for design steps; downstream
        # code accesses this as a dict (.get()), so normalize at the boundary.
        return v if isinstance(v, dict) else {}
    expects: ExpectsSchema = Field(
        default_factory=ExpectsSchema,
        description="Predicted result of this step; used by the attestor.",
    )

    @model_validator(mode="after")
    def _auto_fill_tool_for_action_intents(self):
        """R19.9: suggested_tool defaults by intent when missing.

        Stipulator may legitimately leave tool=null for a pure 'design' step.
        For any other intent, the Performer needs a starting tool — leaving
        it null causes wrong-tool selection (observed: django-13315 with
        intent=mutate + tool=null → Performer chose read instead of
        str_replace). Auto-fill a sensible default so the Performer always
        has a starting contract.

        Could later be tightened to ValidationError + retry-on-correction
        once the test fixtures are migrated; for now, auto-fill keeps the
        invariant without churn.
        """
        if self.intent != "design" and self.suggested_tool is None:
            _default_by_intent = {
                "investigate": "read",
                "mutate": "str_replace",
                "verify": "bash",
                "other": "bash",
            }
            self.suggested_tool = _default_by_intent.get(self.intent, "bash")
        return self


class PhaseSchema(BaseModel):
    """A grouping of related steps."""

    name: PhaseName = Field(
        description=(
            "MAP (investigation), DESIGN (decisions), IMPLEMENT (mutations), "
            "VERIFY (run tests). OTHER if it does not fit."
        )
    )
    steps: list[ExecutionSchema] = Field(default_factory=list)


class PhasedPlanSchema(BaseModel):
    """Top-level container for the Stipulator's cold-start output."""

    phases: list[PhaseSchema] = Field(default_factory=list)

    @field_validator("phases", mode="after")
    @classmethod
    def _require_verify_step(cls, phases: list[PhaseSchema]) -> list[PhaseSchema]:
        """Every plan must include at least one step with intent='verify'.

        Without this, the LLM stipulator occasionally emits a plan that stops
        at IMPLEMENT — the agent runs all its mutate steps and exhausts the
        plan tree without ever running the test (observed on django-12039:
        7 steps across MAP/DESIGN/IMPLEMENT, no VERIFY phase → plan exhausted
        → failed criteria check at the end → wasted 164K tokens).

        Reject the plan at the schema boundary so the existing retry-on-
        correction loop in call_stipulator_phased asks the LLM again with an
        explicit correction message.
        """
        if not phases:
            return phases  # empty plan handled elsewhere
        has_verify = any(
            step.intent == "verify"
            for phase in phases
            for step in phase.steps
        )
        if not has_verify:
            raise ValueError(
                "PhasedPlan must include at least one step with intent='verify' "
                "(typically the last step: 'Run the oracle test command and confirm "
                "it exits 0'). Plans that stop at IMPLEMENT skip the test and "
                "fail final criteria check without running pytest."
            )
        return phases


# ────────────────────────────────────────────────────────────────────────────
# Attestor — per-step "is this step done?" verdict
# ────────────────────────────────────────────────────────────────────────────


class AttestorSchema(BaseModel):
    """Attestor's verdict on whether the current step is done.

    The Activator only consumes `complete` (bool) and `reason` (free-text);
    additional fields can be added without breaking callers.
    """

    complete: bool = Field(description="True iff the step appears done.")
    reason: str = Field(
        default="",
        description="Short justification (≤300 chars). Truncated by caller.",
    )


# ────────────────────────────────────────────────────────────────────────────
# PlanPatcher — tree-edit ops emitted on stall
# ────────────────────────────────────────────────────────────────────────────


class _OpBaseSchema(BaseModel):
    """Common fields for all PlanPatcher ops; reflection is mandatory per §6.3."""

    reflection: str = Field(
        min_length=1,
        description="Why this op (required — drives the librarian's edge log).",
    )


class CommitOp(_OpBaseSchema):
    op: Literal["COMMIT"]
    node_id: str
    reason: str


class CheckoutOp(_OpBaseSchema):
    op: Literal["CHECKOUT"]
    node_id: str


class DropOp(_OpBaseSchema):
    op: Literal["DROP"]
    node_id: str
    reason: str


class CommitNewOp(_OpBaseSchema):
    op: Literal["COMMIT_NEW"]
    parent_id: str
    new_step: dict = Field(description="A ExecutionSchema-shape dict (validated downstream).")


class RefineOp(_OpBaseSchema):
    op: Literal["REFINE"]
    node_id: str
    substeps: list[dict] = Field(min_length=1)


class BranchOp(_OpBaseSchema):
    op: Literal["BRANCH"]
    node_id: str
    alt_steps: list[dict] = Field(min_length=1)


class MergeOp(_OpBaseSchema):
    op: Literal["MERGE"]
    branch_id: str
    target_id: str


# Discriminated union — Pydantic picks the right model by `op` field.
PatcherOp = CommitOp | CheckoutOp | DropOp | CommitNewOp | RefineOp | BranchOp | MergeOp


class PatcherSchema(BaseModel):
    """Top-level container for the PlanPatcher's ops list."""

    ops: list[PatcherOp] = Field(
        default_factory=list,
        description="Tree-edit ops; the Activator applies them in order.",
    )


# ────────────────────────────────────────────────────────────────────────────
# Resolver — stall-escalation decision
# ────────────────────────────────────────────────────────────────────────────


class ResolverSchema(BaseModel):
    """LLMResolver's decision when the loop stalls.

    Mirrors the existing ResolverOutput dataclass in cat/resolver.py but
    enforced at the schema boundary so malformed LLM responses fall back
    to _safe_fallback() via ValidationError instead of silently returning
    forward/patch_plan on best-effort string matching.
    """

    decision: Literal["forward", "patch_plan", "backward", "give_up"]
    reason: str = Field(default="", description="Truncated to 300 by caller.")
    diagnosis: str = Field(default="", description="Truncated to 500 by caller.")
    next_instruction: str = Field(default="", description="Truncated to 500 by caller.")


# ────────────────────────────────────────────────────────────────────────────
# Attribution auditor — per-layer pass/fail verdicts (offline analysis)
# ────────────────────────────────────────────────────────────────────────────


# Enum constants kept in sync with cat/attribution.py.
AttributionStatus = Literal["OK", "DEGRADED", "FAILED", "NA"]
AttributionLayer = Literal["represent", "stipulate", "perform", "verify"]


class LayerStatusSchema(BaseModel):
    status: AttributionStatus = "NA"
    evidence: str = Field(default="", description="Short justification.")


class AttributionSchema(BaseModel):
    """LLM-driven post-task attribution: which CAT layer was the limiter?

    Mirrors the four-layer CAT spec (`represent / stipulate / perform / verify`)
    plus a diagnostic header. Diagnostic fields (`limitation_kind`,
    `improvement_vector`, `confidence`) stay free-form strings here — they
    have their own enum constants in cat/attribution.py and the auditor
    post-processes them with explicit validation; lifting those into Pydantic
    would duplicate the constants without much new safety.
    """

    represent: LayerStatusSchema = Field(default_factory=LayerStatusSchema)
    stipulate: LayerStatusSchema = Field(default_factory=LayerStatusSchema)
    perform: LayerStatusSchema = Field(default_factory=LayerStatusSchema)
    verify: LayerStatusSchema = Field(default_factory=LayerStatusSchema)
    primary: AttributionLayer = "perform"
    limitation_kind: str = ""
    limitation_secondary: str = ""
    improvement_vector: str = ""
    confidence: str = ""


# ────────────────────────────────────────────────────────────────────────────
# Performer outcome — structured normalization of one executor turn
#
# Each per-iter LLM call (Performer) used to be summarized as a single bool
# ("keep_going") with the real signals scattered across run.messages and re-
# derived by separate helpers (_infer_durable_change, _infer_verifier_outcome,
# step_done's [ERROR] check). OutcomeSchema centralizes all of that into
# one typed object consumed by every downstream phase (observe, attestor,
# surprise, resolver). Also closes the R19 leak: a "text-only, no tool call"
# response on a mutate/verify step is no longer silently interpreted as DONE.
# ────────────────────────────────────────────────────────────────────────────


class InvocationSchema(BaseModel):
    """One tool call executed during a Performer turn.

    Carries the canonical (post-normalization) args, the success/error
    signal, and the derived facts (durable_change, files_modified) so
    downstream consumers don't re-derive them from message history.

    `reason` is execution-side metadata — the Performer's one-sentence
    explanation of why this tool was called, especially when it differs
    from the plan's suggested_tool. It is NOT part of the tool's contract
    args; it lives on the Invocation (actual execution) record, not on
    the ExecutionSchema (plan side). Populated by the LLM via a top-level
    `reason` field in tool args, stripped at dispatch.
    """
    tool_name: str
    args: dict = Field(default_factory=dict)
    reason: str = ""
    succeeded: bool = True
    output_text: str = ""
    error: str = ""
    durable_change: bool = False
    files_modified: list[str] = Field(default_factory=list)


_WRITE_TOOLS = ("str_replace", "multi_edit", "apply_patch", "write")
_TEST_COMMAND_MARKERS = ("pytest", "runtests", "tox", "unittest", "django-admin test")


class OutcomeSchema(BaseModel):
    """Structured result of one Performer turn. Replaces the old (keep_going: bool)
    return value of _perform_once. Every downstream phase reads off this object.
    """
    text_response: str = ""
    invocations: list[InvocationSchema] = Field(default_factory=list)
    finish_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    # ── Derived properties (computed from invocations) ────────────────────

    @property
    def any_tool_called(self) -> bool:
        return len(self.invocations) > 0

    @property
    def any_durable_change(self) -> bool:
        return any(inv.durable_change for inv in self.invocations if inv.succeeded)

    @property
    def any_tool_errored(self) -> bool:
        return any(not inv.succeeded for inv in self.invocations)

    @property
    def files_modified(self) -> list[str]:
        out: list[str] = []
        for inv in self.invocations:
            if inv.succeeded:
                for f in inv.files_modified:
                    if f and f not in out:
                        out.append(f)
        return out

    @property
    def verifier_outcome(self) -> "Optional[str]":
        """Parse pytest/runtests/tox output for PASS/FAIL.

        Returns None when the last invocation wasn't a recognizable test
        invocation (so we don't false-positive on unrelated bash output).
        """
        if not self.invocations:
            return None
        # Look at the last invocation — if it's a test command, parse its output.
        last = self.invocations[-1]
        cmd = ""
        if last.tool_name == "bash":
            cmd = (last.args.get("command", "") or "").lower()
        is_test = (
            last.tool_name in ("oracle", "runtests") or
            (last.tool_name == "bash" and any(m in cmd for m in _TEST_COMMAND_MARKERS))
        )
        if not is_test:
            return None
        if not last.succeeded:
            return "FAIL"
        out = last.output_text or ""
        lc = out.lower()
        fail_markers = (
            " failed", "failed,", "failed]", "= fail", "errored",
            "traceback (most recent call last)", "internalerror",
            "no module named",
        )
        if any(m in lc for m in fail_markers):
            return "FAIL"
        if "FAILED" in out or "ERROR" in out[-2000:]:
            return "FAIL"
        if "passed" in lc and "failed" not in lc:
            return "PASS"
        if "no tests ran" in lc:
            return None  # ambiguous
        return None

    # ── Decision helper ──────────────────────────────────────────────────

    def is_terminal(self, attention_intent: "Optional[str]") -> bool:
        """True iff this Performer response is a legitimate DONE signal
        given the current attention step's intent.

        - Made a tool call → not done (continuing work this iter).
        - No tool call + intent in {mutate, verify} → NOT done (Performer
          described an action without executing it; counts as stall).
        - No tool call + intent in {investigate, design, other} → legit done.
        """
        if self.any_tool_called:
            return False
        if attention_intent in ("mutate", "verify"):
            return False
        return True
