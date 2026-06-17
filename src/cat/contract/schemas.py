"""Pydantic schemas for all CAT data shapes.

Naming convention: Pydantic BaseModel -> ends in `Schema`.

See design.v3.md §"Schemas" and design_schema.md.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


Intent = Literal["investigate", "design", "mutate", "verify", "other"]
PhaseName = Literal["MAP", "DESIGN", "IMPLEMENT", "VERIFY", "OTHER"]
VerifierOutcome = Literal["PASS", "FAIL", "N/A"]


class ExpectsSchema(BaseModel):
    durable_change: bool = False
    files_modified: list[str] = Field(default_factory=list)
    verifier_outcome: VerifierOutcome = "N/A"
    result: str = ""


class ExecutionSchema(BaseModel):
    description: str = ""
    intent: Intent = "other"
    suggested_tool: Optional[str] = None
    suggested_args: dict = Field(default_factory=dict)
    expects: ExpectsSchema = Field(default_factory=ExpectsSchema)


class HypothesisSchema(BaseModel):
    location: str = ""
    change: str = ""
    predicts: str = ""
    basis: str = ""
    start_from: str = "base"


class PerformerTaskSchema(BaseModel):
    obligation: str = ""
    hypothesis: HypothesisSchema = Field(default_factory=HypothesisSchema)
    oracle: str = ""
    context: str = ""
    max_attempts: int = 5


class PerformerStep(BaseModel):
    action: str = ""
    rationale: str = ""
    status: str = "pending"


class PerformerPlanSchema(BaseModel):
    current_state: str = ""
    steps: list[PerformerStep] = Field(default_factory=list)
    next_step: Optional[PerformerStep] = None
    confidence: str = "medium"


class KnowledgeSchema(BaseModel):
    findings: dict[str, str] = Field(default_factory=dict)
    failed_hypotheses: list[str] = Field(default_factory=list)
    oracle_failures: list[str] = Field(default_factory=list)


class AttestorV3Schema(BaseModel):
    verdict: Literal["new_hypothesis", "give_up"] = "new_hypothesis"
    reason: str = ""
    diagnosis: str = ""


class InvocationSchema(BaseModel):
    attempt: int = 0
    tool_name: str = ""
    args: dict = Field(default_factory=dict)
    reason: str = ""
    execution_status: Literal["success", "error"] = "success"
    output_text: str = ""
    error: str = ""
    durable_change: bool = False
    files_modified: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.execution_status == "success"


class OutcomeSchema(BaseModel):
    text_response: str = ""
    invocations: list[InvocationSchema] = Field(default_factory=list)
    finish_reason: str = ""
    verdict: str = "exhausted"
    performer_reason: str = ""
    any_tool_called: bool = False
    any_durable_change: bool = False
    any_tool_errored: bool = False


# ── v3 Performer execution schemas ───────────────────────────────────────────


class ToolCall(BaseModel):
    """A single tool call with name and arguments."""
    name: str = Field(default="", description="Tool name, e.g. read, rg, str_replace, bash")
    arguments: dict = Field(default_factory=dict, description="Tool-specific arguments")


PerformerIntent = Literal["look", "change", "check", "done", "give_up"]


class PerformerExecutionStep(BaseModel):
    """One step from the Performer LLM in the inner ReAct loop.

    The model declares its intent, what tools to call (if any),
    what it expects to see after execution, and what should happen next.

    Validation rules:
    - intent=look/change/check -> tools required, expect required, then required
    - intent=done/give_up      -> reason required, tools must be empty
    """
    thinking: str = Field(
        default="",
        description="Why these actions make sense given current state + hypothesis",
    )
    intent: PerformerIntent = Field(
        default="look",
        description=(
            "What the performer is doing RIGHT NOW: "
            "look=observe/explore, change=modify, check=verify with oracle, "
            "done=task complete, give_up=can't solve"
        ),
    )
    tools: list[ToolCall] = Field(
        default_factory=list,
        description=(
            "Tool calls to execute in this step. "
            "Required for look/change/check, must be empty for done/give_up."
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
            "Required for look/change/check, null for done/give_up."
        ),
    )
    then: Optional[PerformerIntent] = Field(
        None,
        description=(
            "What should happen next after this step completes. "
            "Required for look/change/check, null for done/give_up."
        ),
    )

    @model_validator(mode="after")
    def _validate_intent_consistency(self):
        if self.intent in ("done", "give_up"):
            if not self.reason:
                raise ValueError(f"intent={self.intent!r} requires a reason")
            if self.tools:
                raise ValueError(f"intent={self.intent!r} must not have tools")
        else:
            if not self.tools:
                raise ValueError(f"intent={self.intent!r} requires at least one tool call")
            if not self.expect:
                raise ValueError(f"intent={self.intent!r} requires expect")
            if not self.then:
                raise ValueError(f"intent={self.intent!r} requires then")
        return self
