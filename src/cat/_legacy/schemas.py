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
    """Attestor's plan-aware verdict (R37).

    The Attestor sees the Performer's result + prev/next N plan steps + the
    overall goal. It decides the next action, not just "is this step done?".

    `verdict` values:
    - complete     → step satisfied, advance attention to next node
    - patch_plan   → step not done, plan is sound, PlanPatcher can fix locally
    - restipulate  → plan premise is wrong, call Stipulator again with failure context
    - give_up      → unrecoverable, abort the task

    `diagnosis` is required for patch_plan and restipulate — what specifically
    is wrong, so PlanPatcher / the new Stipulator can act on it.

    Backward-compat: `complete` property maps verdict=="complete" → True so
    older code paths that check attestor_done still work during migration.
    """
    verdict: Literal["complete", "patch_plan", "restipulate", "give_up"] = Field(
        description="What the Attestor decided to do next."
    )
    reason: str = Field(
        default="",
        description="Short justification (≤300 chars). Truncated by caller.",
    )
    diagnosis: str = Field(
        default="",
        description="For patch_plan/restipulate: what is wrong and why. "
                    "Required for actionable recovery.",
    )

    @property
    def complete(self) -> bool:
        """Backward-compat: True when verdict=='complete'."""
        return self.verdict == "complete"


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

    `execution_status` is the deterministic outcome — set by the handler
    (e.g. bash exit code, str_replace old_str-not-found). When "error",
    the Activator short-circuits the Attestor LLM call (no judgement
    needed for a deterministic event) and routes directly to the
    Resolver/PlanPatcher pipeline. See R29.
    """
    tool_name: str
    args: dict = Field(default_factory=dict)
    reason: str = ""
    attempt: int = 1      # which inner-loop attempt produced this call (R38)
    succeeded: bool = True
    execution_status: Literal["success", "error"] = "success"
    output_text: str = ""
    error: str = ""
    durable_change: bool = False
    files_modified: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_status_with_succeeded(self):
        """Keep `execution_status` and `succeeded` in lockstep so older code
        paths that only set `succeeded` still produce a coherent record."""
        if self.execution_status == "success" and not self.succeeded:
            self.execution_status = "error"
        elif self.execution_status == "error" and self.succeeded:
            self.succeeded = False
        return self


_WRITE_TOOLS = ("str_replace", "multi_edit", "apply_patch", "write")
_TEST_COMMAND_MARKERS = ("pytest", "runtests", "tox", "unittest", "django-admin test")


class OutcomeSchema(BaseModel):
    """Structured result of one Performer inner-loop run (R38).

    Replaces the old single-turn OutcomeSchema. The Performer now runs a
    bounded react loop (up to max_inner attempts) and self-reports its
    verdict. The Attestor validates and routes based on this.

    `verdict`:
    - completed  → Performer self-assessed the step is done
    - exhausted  → K attempts without success; reason is a summarisation
    - give_up    → Performer hit a fundamental blocker; bypass Attestor
    `performer_reason` is MANDATORY for all verdicts — always explains why.
    """
    text_response: str = ""
    invocations: list[InvocationSchema] = Field(default_factory=list)
    finish_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    # R38: inner-loop verdict fields
    verdict: Literal["completed", "exhausted", "give_up"] = "completed"
    performer_reason: str = ""   # mandatory explanation from Performer

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


# ────────────────────────────────────────────────────────────────────────────
# v3 schemas — hypothesis-based loop engine (see design.v3.md)
# ────────────────────────────────────────────────────────────────────────────


class HypothesisSchema(BaseModel):
    """The Stipulator's output in v3: one specific, testable prediction
    about what change will satisfy the contract obligation.

    NOT a multi-step plan — a single hypothesis about WHERE to act,
    WHAT to do, and WHAT will be true after. The Performer figures out
    HOW to implement it.
    """
    location: str = Field(description="WHERE to act — file path, function name, API endpoint, record ID, etc.")
    change: str = Field(description="WHAT to do — specific enough for Performer to execute directly.")
    predicts: str = Field(description="WHAT will be true after — verifiable statement about world state.")
    basis: str = Field(default="", description="WHY this hypothesis — reasoning from obligation + knowledge.")
    start_from: str = Field(
        default="base",
        description=(
            "WHICH checkpoint to start this hypothesis from. 'base' = the original "
            "unmodified state. 'h1', 'h2', ... = the state after that prior attempt's "
            "changes. Choose 'base' when prior changes were wrong and should be discarded; "
            "choose a prior checkpoint when its changes were partially correct and this "
            "hypothesis builds on them."
        ),
    )


class PerformerTaskSchema(BaseModel):
    """What the Performer receives in v3: narrow, focused context.

    Deliberately does NOT include the full message history or plan tree —
    only what is needed to implement the hypothesis and verify it.
    Enables cheaper model tier for Performer.
    """
    obligation: str = Field(description="Original task — semantic anchor so Performer understands intent.")
    hypothesis: HypothesisSchema
    oracle: str = Field(description="Test/verification command — Performer can self-verify after mutation.")
    context: str = Field(default="", description="Compressed knowledge from prior attempts — key facts already known.")
    max_attempts: int = Field(default=5)


class KnowledgeSchema(BaseModel):
    """What transfers between hypothesis cycles in v3.

    Populated by the Performer's findings and oracle failure messages.
    Passed to the next Stipulator call so it can generate a better
    hypothesis without re-investigating already-known facts.
    """
    findings: dict = Field(default_factory=dict, description="location → what was actually found there")
    failed_hypotheses: list = Field(default_factory=list, description="list of {hypothesis, verdict, reason} dicts")
    oracle_failures: list = Field(default_factory=list, description="specific oracle failure messages from prior attempts")


class AttestorV3Schema(BaseModel):
    """Attestor output in v3 — only two verdicts.

    Called ONCE per hypothesis cycle, only when the oracle failed.
    Decides: try a new hypothesis (with updated knowledge) or give up.
    """
    verdict: Literal["new_hypothesis", "give_up"] = Field(
        description="new_hypothesis: try again | give_up: task unrecoverable"
    )
    reason: str = Field(description="Specific evidence for the verdict.")
    diagnosis: str = Field(default="", description="What was wrong — informs the next hypothesis.")


# ────────────────────────────────────────────────────────────────────────────
# PerformerPlanSchema — Performer's internal north star (v3)
# ────────────────────────────────────────────────────────────────────────────


class PerformerStepSchema(BaseModel):
    """One step in the Performer's internal execution plan.

    Domain-agnostic: describes WHAT to do and WHY, not which tool.
    The Performer picks the appropriate tool at execution time.
    The last step is ALWAYS oracle/criterion verification.
    """
    action: str = Field(
        description="What to do — domain-agnostic description of the action."
    )
    rationale: str = Field(
        description="Why this step is needed before the oracle can pass."
    )
    status: Literal["pending", "done", "failed"] = Field(
        default="pending",
        description="Execution status — updated as the Performer works through the plan.",
    )


class PerformerPlanSchema(BaseModel):
    """The Performer's internal execution plan — from current position to oracle pass.

    Created once at the start of the inner loop (before the first tool call).
    Revised when a step fails or new information changes the path forward.

    Design principles:
    - Universal: works for 1-step (just verify) or N-step (inspect → change → verify)
    - Domain-agnostic: steps use semantic language, not tool names
    - Last step is ALWAYS oracle/criterion verification
    - confidence drives the give_up decision — low after repeated failures

    Lifecycle:
      1. Created by first LLM call (no tools yet)
      2. Executor works through steps in order
      3. Each step updates status: pending → done | failed
      4. On failure: plan is revised or confidence drops
      5. confidence="low" → Performer should call give_up()
      6. All steps done + oracle step done → call done()
    """
    current_state: str = Field(
        description=(
            "Where we are RIGHT NOW — what has been inspected, changed, or "
            "confirmed so far. 'Not started' if this is the first attempt."
        )
    )
    steps: list[PerformerStepSchema] = Field(
        description=(
            "Ordered list of actions from current state to oracle pass. "
            "Must contain at least one step. Last step must be oracle/criterion verification."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "How confident the Performer is this plan will reach oracle pass. "
            "high=clear path; medium=uncertain but worth trying; low=approach likely wrong→consider give_up."
        )
    )

    @property
    def next_step(self) -> "Optional[PerformerStepSchema]":
        """First pending step — the Performer's immediate target."""
        return next((s for s in self.steps if s.status == "pending"), None)

    @property
    def is_complete(self) -> bool:
        """True when all steps are done (oracle step included)."""
        return bool(self.steps) and all(s.status == "done" for s in self.steps)

    @property
    def pending_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "pending")

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "failed")


# ── Performer Inner Loop Schemas (v3 toolcall mode) ─────────────────────


class ToolCall(BaseModel):
    """A single tool call with name and arguments."""
    name: str = Field(description="Tool name, e.g. read, rg, str_replace, bash")
    arguments: dict = Field(default_factory=dict, description="Tool-specific arguments, e.g. {'path': '/testbed/foo.py'} for read")


class PerformerStep(BaseModel):
    """One step from the performer LLM in the inner ReAct loop.

    The model declares its intent, what tools to call (if any),
    what it expects to see after execution, and what should happen next.

    Validation rules:
    - intent=look/change/check → tools required, expect required, then required
    - intent=done/give_up → reason required, tools must be empty
    """
    thinking: str = Field(
        description="Why these actions make sense given current state + hypothesis"
    )
    intent: Literal["look", "change", "check", "done", "give_up"] = Field(
        description=(
            "What the performer is doing RIGHT NOW: "
            "look=observe/explore, change=modify code, check=verify with oracle, "
            "done=task complete, give_up=can't solve, need new hypothesis"
        )
    )
    tools: list[ToolCall] = Field(
        default_factory=list,
        description=(
            "Tool calls to execute in this step. "
            "Required for look/change/check, must be empty for done/give_up. "
            "Multiple tools allowed for parallel exploration."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description=(
            "Required for done/give_up: why the task is complete or why this "
            "hypothesis is wrong. Null for look/change/check."
        ),
    )
    expect: Optional[str] = Field(
        None,
        description=(
            "Expected result after executing tools. "
            "Required for look/change/check, null for done/give_up. "
            "Example: 'I expect to see the construct_instance function with "
            "the field check condition at line 396.'"
        ),
    )
    then: Optional[Literal["look", "change", "check", "done", "give_up"]] = Field(
        None,
        description=(
            "What should happen next after this step completes. "
            "Required for look/change/check, null for done/give_up. "
            "The loop uses this to detect if the model deviates from its plan."
        ),
    )

    @model_validator(mode="after")
    def _validate_intent_consistency(self):
        """Enforce that terminal intents have reason and no tools,
        while action intents have tools, expect, and then."""
        if self.intent in ("done", "give_up"):
            if not self.reason:
                raise ValueError(f"intent={self.intent!r} requires a reason")
            if self.tools:
                raise ValueError(f"intent={self.intent!r} must not have tools")
        else:  # look, change, check
            if not self.tools:
                raise ValueError(f"intent={self.intent!r} requires at least one tool call")
            if not self.expect:
                raise ValueError(f"intent={self.intent!r} requires expect")
            if not self.then:
                raise ValueError(f"intent={self.intent!r} requires then")
        return self
