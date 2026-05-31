"""Resolver + Reflection — supervises stalled attention steps.

R6+ extension: the Resolver is now both a *decision* layer (forward /
restipulate / backward / give_up) AND a *reflection* layer (diagnosis
of why we stalled + an actionable next_instruction for the next iter).

Cost-wise this is one LLM call per stall point — same as the basic
Resolver — but the output is richer:

  - decision        : 4-valued action (same as before)
  - reason          : 1-sentence rationale (logging / traces)
  - diagnosis       : 2-3 sentences explaining what's stuck and why
                      (used for cross-task transfer + retrospection)
  - next_instruction: a specific directive injected into the next iter's
                      attention hint (when forward/backward) or into the
                      Stipulator-in-loop prompt (when restipulate)

Three implementations:
  - LLMResolver (default): focused LLM call with diagnostic prompt
  - HeuristicResolver: cheap no-LLM fallback, deterministic rules
  - StubResolver: test helper, returns fixed output

Deterministic safety guards run AFTER the resolver returns, catching
clearly wrong decisions (e.g., 'forward' on a mutate step where no
mutate tool has been called).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


ResolverDecision = Literal["forward", "restipulate", "backward", "give_up"]


@dataclass
class StallContext:
    """Everything the Resolver needs to decide."""
    attention_node_id: str
    attention_description: str
    attention_intent: str               # investigate / design / mutate / verify / other
    attention_phase: str                # MAP / DESIGN / IMPLEMENT / VERIFY
    expects: dict[str, Any]
    surprise_dims: dict[str, Any]       # SurpriseVector.to_dict()
    stall_count: int
    restip_count: int
    plan_tree_outline: str
    recent_tool_calls: list[dict[str, Any]]
    obligation_brief: str
    state_now_class: str
    # R6+ enriched fields:
    attestor_reasons: list[str] = field(default_factory=list)   # last few attestor verdicts
    files_expected: list[str] = field(default_factory=list)     # from step.expects.files_modified
    files_actually_modified: list[str] = field(default_factory=list)
    intent_tool_counts: dict[str, int] = field(default_factory=dict)
    # e.g., {"investigate": 12, "mutate": 0, "verify": 0, "other": 1}

    def to_prompt_payload(self) -> str:
        """Compact stringification for LLM prompt."""
        intent_summary = ", ".join(
            f"{k}={v}" for k, v in self.intent_tool_counts.items()
        ) or "none"
        files_match_summary = (
            f"expected to touch {self.files_expected}, actually touched "
            f"{self.files_actually_modified or '[]'}"
            if self.files_expected else "(no specific files expected)"
        )
        recent_str_parts: list[str] = []
        for tc in self.recent_tool_calls[-6:]:
            line = (
                f"  - {tc.get('tool','?')}({str(tc.get('args',''))[:80]}) "
                f"→ {(tc.get('output_summary','') or '')[:120]}"
            )
            recent_str_parts.append(line)
            ar = tc.get("attestor_reason")
            if ar:
                recent_str_parts.append(f"    attestor: {ar[:160]}")
        recent_str = "\n".join(recent_str_parts) or "  (none)"
        attestor_history = "\n".join(
            f"  - {r[:200]}" for r in self.attestor_reasons[-3:]
        ) or "  (none)"

        return (
            f"OBLIGATION (brief): {self.obligation_brief[:300]}\n\n"
            f"STUCK AT: [{self.attention_node_id}] "
            f"({self.attention_phase}/{self.attention_intent})\n"
            f"  description: {self.attention_description[:200]}\n"
            f"  expects: {json.dumps(self.expects)[:200]}\n\n"
            f"WHY STUCK:\n"
            f"  stall_count    = {self.stall_count} iters on this node\n"
            f"  restip_count   = {self.restip_count} re-stipulates so far\n"
            f"  intent_tools   = {intent_summary} (counts by tool intent)\n"
            f"  files          = {files_match_summary}\n"
            f"  surprise dims  = {json.dumps(self.surprise_dims)[:400]}\n\n"
            f"RECENT ATTESTOR VERDICTS:\n{attestor_history}\n\n"
            f"PLAN TREE:\n{self.plan_tree_outline[:1200]}\n\n"
            f"RECENT TOOL ACTIVITY (last few iters):\n{recent_str}"
        )


@dataclass
class ResolverOutput:
    """Richer output for the Resolver + Reflection layer."""
    decision: ResolverDecision
    reason: str = ""
    diagnosis: str = ""
    next_instruction: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "diagnosis": self.diagnosis,
            "next_instruction": self.next_instruction,
        }


# Public guard — runs after any Resolver's resolve() to enforce constraints
def enforce_guards(output: ResolverOutput, ctx: StallContext) -> ResolverOutput:
    """Deterministic safety net. The Resolver is one signal; the guards
    are the floor. Apply BEFORE returning to the activator.
    """
    # Guard 1: 'forward' on a mutate step with no mutate tool calls is wrong
    if output.decision == "forward" and ctx.attention_intent == "mutate":
        if ctx.intent_tool_counts.get("mutate", 0) == 0:
            return ResolverOutput(
                decision="restipulate",
                reason=f"GUARD: refused 'forward' on un-mutated mutate step "
                       f"(intent_tool_counts.mutate=0). Original: {output.reason[:120]}",
                diagnosis=(output.diagnosis or "")
                          + " [guard override: no mutate tool calls on a mutate step]",
                next_instruction=output.next_instruction
                                 or "Use str_replace, multi_edit, apply_patch, or write to "
                                    "modify the target file. Do not use read/rg/outline.",
            )
    # Guard 2: 'forward' on verify step with failed verifier → restipulate
    if output.decision == "forward" and ctx.attention_intent == "verify":
        if not ctx.surprise_dims.get("verifier_match", True):
            return ResolverOutput(
                decision="restipulate",
                reason=f"GUARD: refused 'forward' on verify step with verifier_match=False. "
                       f"Original: {output.reason[:120]}",
                diagnosis=output.diagnosis,
                next_instruction=output.next_instruction
                                 or "The verifier did not pass. Revise the plan to address "
                                    "the failing test or the prior edit.",
            )
    return output


class Resolver(ABC):
    """Interface for the escalation policy + reflection layer."""

    @abstractmethod
    def resolve(self, ctx: StallContext) -> ResolverOutput:
        """Return a decision + reflection for the stalled attention step."""
        ...


class StubResolver(Resolver):
    """Test helper — always returns a fixed output (decision-only or full)."""

    def __init__(
        self,
        fixed: ResolverDecision = "forward",
        next_instruction: str = "",
        diagnosis: str = "",
        reason: str = "",
    ):
        self.fixed = fixed
        self.next_instruction = next_instruction
        self.diagnosis = diagnosis
        self.reason = reason

    def resolve(self, ctx: StallContext) -> ResolverOutput:
        return ResolverOutput(
            decision=self.fixed,
            reason=self.reason,
            diagnosis=self.diagnosis,
            next_instruction=self.next_instruction,
        )


class HeuristicResolver(Resolver):
    """Cheap no-LLM resolver. Used when LLM unavailable or for cold-start."""

    def resolve(self, ctx: StallContext) -> ResolverOutput:
        # Hard rules first
        if ctx.restip_count >= 5:
            return ResolverOutput(
                decision="forward",
                reason="restip cap hit; force forward",
            )
        if ctx.attention_intent == "verify" and not ctx.surprise_dims.get("verifier_match", True):
            return ResolverOutput(
                decision="backward",
                reason="verify step failed; back up to last mutate",
                diagnosis="verifier did not pass; the previous mutate was likely wrong",
            )
        if ctx.stall_count >= 6:
            return ResolverOutput(
                decision="give_up",
                reason="long stall; budget exhausted",
            )
        # Mutate-intent w/ no mutate calls → strong restipulate
        if ctx.attention_intent == "mutate" and ctx.intent_tool_counts.get("mutate", 0) == 0:
            return ResolverOutput(
                decision="restipulate",
                reason="mutate step but no write tool used",
                diagnosis="agent did not use a write-tool on a mutate step",
                next_instruction="Use str_replace/multi_edit/apply_patch/write to modify the target file.",
            )
        return ResolverOutput(
            decision="restipulate",
            reason="default: revise plan",
        )


_LLM_RESOLVER_SYSTEM_PROMPT = """\
You are the RESOLVER + REFLECTION layer for a contract-disciplined agent. \
The inner loop reached a stall point: the current attention step has not \
progressed for several iterations.

You receive a structured stall report. Diagnose why the agent is stuck and \
output FOUR fields:

  decision         one of: forward | restipulate | backward | give_up
  reason           1-sentence rationale
  diagnosis        2-3 sentences: what's actually stuck, what evidence shows it
  next_instruction a SPECIFIC, ACTIONABLE directive injected into the next iter
                   (e.g., "use str_replace on file X to add Y" or "run the test
                   command Z and inspect the failure")

DECISION CRITERIA — read carefully:

  forward          The step is genuinely complete or close enough.
                   Use ONLY when:
                   - investigate/design intent: agent has gathered useful info
                     (attestor was strict but evidence is enough)
                   - mutate intent: actual edit happened (intent_tools.mutate > 0)
                     AND files_actually_modified matches expected
                   - verify intent: verifier_match is True
                   DO NOT pick 'forward' on a mutate step where intent_tools.mutate
                   is 0. That means the agent never edited anything; the step is
                   not done.

  restipulate      Revise the plan. Use this when:
                   - the surprise reveals new info (code is structured differently
                     than the plan assumed)
                   - the agent is on a mutate step but only used read/rg tools
                     (intent_tools.mutate=0): supply a clearer edit target via
                     next_instruction
                   - the attestor keeps complaining with the same reason: re-frame
                     the step

  backward         Abandon the current subtree, restore attention to last completed
                   mutate ancestor. Use when:
                   - verify step failed AND prior edit was likely wrong
                   - multiple restipulates already tried on same node
                   - the current path is a dead end

  give_up          Mark the contract FAILED. Use ONLY when:
                   - restip_count >= 5 AND no progress
                   - max backwards already tried
                   - further iterations would waste budget

DIAGNOSIS HINTS:

  - If intent_tools.mutate == 0 on a mutate step → agent is investigating instead
    of editing. Likely because the step is vague. Restipulate with specific file
    and edit target.
  - If files_actually_modified ≠ expected → agent edited wrong file. Restipulate
    with explicit path.
  - If same attestor reason appears in attestor_reasons history → agent doesn't
    understand the step. Re-frame in next_instruction.
  - If many investigate calls in a row → maybe forward (info gathered) OR
    restipulate (info wasn't useful).

NEXT_INSTRUCTION FORMAT:

  - For forward: 1 sentence on what to focus on at the next attention step
  - For restipulate: 1-2 sentences with SPECIFIC instructions (file paths,
    function names, expected changes) that the in-loop Stipulator should bake
    into the new plan
  - For backward: 1 sentence on what to do differently when restored
  - For give_up: not used (leave empty)

Output ONLY this JSON shape:

{
  "decision": "...",
  "reason": "...",
  "diagnosis": "...",
  "next_instruction": "..."
}
"""


class LLMResolver(Resolver):
    """Default resolver — focused LLM call returning decision + reflection.

    Cost: ~500-800 tokens per invocation. Only fires at escalation points,
    not every iter.
    """

    def __init__(self, llm: Any, recorder: Any = None) -> None:
        self.llm = llm
        self.recorder = recorder

    def resolve(self, ctx: StallContext) -> ResolverOutput:
        try:
            messages = [
                {"role": "system", "content": _LLM_RESOLVER_SYSTEM_PROMPT},
                {"role": "user", "content": ctx.to_prompt_payload()},
            ]
            result = self.llm.chat(
                messages=messages,
                response_format={"type": "json_object"},
                role_tag="resolver",
                temperature=0.1,
            )
            if not getattr(result, "text", None):
                return _safe_fallback(ctx)
            from cat.plan_schema import ResolverVerdict
            from pydantic import ValidationError
            try:
                verdict = ResolverVerdict.model_validate_json(result.text)
            except ValidationError:
                return _safe_fallback(ctx)
            return ResolverOutput(
                decision=verdict.decision,
                reason=verdict.reason[:300],
                diagnosis=verdict.diagnosis[:500],
                next_instruction=verdict.next_instruction[:500],
            )
        except Exception:
            return _safe_fallback(ctx)


def _safe_fallback(ctx: StallContext) -> ResolverOutput:
    """When LLMResolver fails, fall back to a conservative default."""
    if ctx.stall_count >= 5:
        return ResolverOutput(decision="forward", reason="fallback: long stall")
    return ResolverOutput(decision="restipulate", reason="fallback: default replan")
