"""Pydantic models for the Stipulator's plan output.

Single source of truth for the plan schema. Both the cold-start Stipulator
and the in-loop Stipulator emit JSON that validates against these models;
the activator + attestor consume them as typed objects.

Why pydantic:
  - Renaming a field propagates everywhere (no manual JSON-key tracking)
  - Validation at the LLM boundary (catches malformed output deterministically)
  - Field descriptions become part of the JSON schema sent to the LLM
  - .model_dump() / .model_validate_json() are trivial round-trips
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


Intent = Literal["investigate", "design", "mutate", "verify", "other"]
PhaseName = Literal["MAP", "DESIGN", "IMPLEMENT", "VERIFY", "OTHER"]
VerifierOutcome = Literal["PASS", "FAIL", "N/A"]


class StepExpects(BaseModel):
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


class PlanStep(BaseModel):
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
    suggested_tool: str | None = Field(
        None,
        description=(
            "The tool you (Stipulator) recommend the Performer use for this step. "
            "Pick from: bash, read, write, list, str_replace, multi_edit, apply_patch, "
            "rg, fd, outline, goto_def, find_refs, git_diff, git_status. "
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
    expects: StepExpects = Field(
        default_factory=StepExpects,
        description="Predicted result of this step; used by the attestor.",
    )


class Phase(BaseModel):
    """A grouping of related steps."""

    name: PhaseName = Field(
        description=(
            "MAP (investigation), DESIGN (decisions), IMPLEMENT (mutations), "
            "VERIFY (run tests). OTHER if it does not fit."
        )
    )
    steps: list[PlanStep] = Field(default_factory=list)


class PhasedPlan(BaseModel):
    """Top-level container for the Stipulator's cold-start output."""

    phases: list[Phase] = Field(default_factory=list)
