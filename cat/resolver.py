"""Resolver — pluggable escalation when an attention step stalls.

Per docs/iteration.md §4b's "stall counter" → §4b "escalation" extension.

When step_done is False for N iters on the same attention node, the inner
loop escalates to a Resolver. The Resolver receives a StallContext (what
happened, why it's stuck, surprise dims) and returns one of four decisions:

  - forward       : force-COMMIT current + advance (today's behavior)
  - restipulate   : call Stipulator-in-loop to revise the plan
  - backward      : DROP current subtree + restore attention to last
                    completed mutate ancestor (or root)
  - give_up       : mark the contract FAILED with reason

The default Resolver is LLMResolver (small focused LLM call). The interface
is swappable so we can have a HeuristicResolver (no LLM, cheap fallback)
or a StubResolver (testing).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


ResolverDecision = Literal["forward", "restipulate", "backward", "give_up"]


@dataclass
class StallContext:
    """Everything the Resolver needs to decide. Constructed by the activator
    at the point of escalation."""
    attention_node_id: str
    attention_description: str
    attention_intent: str               # investigate / design / mutate / verify / other
    attention_phase: str                # MAP / DESIGN / IMPLEMENT / VERIFY
    expects: dict[str, Any]             # from attention_node.expects
    surprise_dims: dict[str, Any]       # SurpriseVector.to_dict()
    stall_count: int                    # how many iters on this node without progress
    restip_count: int                   # how many in-loop re-stipulates so far this task
    plan_tree_outline: str              # current tree state, render_outline()
    recent_tool_calls: list[dict[str, Any]]  # last N (tool, args, output_summary, attestor_reason)
    obligation_brief: str               # first ~300 chars
    state_now_class: str                # for identity / cross-task transfer

    def to_prompt_payload(self) -> str:
        """A reasonably compact stringification for LLM prompt injection."""
        lines = [
            f"OBLIGATION (brief): {self.obligation_brief[:300]}",
            f"",
            f"STUCK AT: [{self.attention_node_id}] ({self.attention_phase}/{self.attention_intent})",
            f"  description: {self.attention_description[:200]}",
            f"  expects: {json.dumps(self.expects)[:200]}",
            f"",
            f"WHY STUCK:",
            f"  stall_count = {self.stall_count} iters",
            f"  restip_count = {self.restip_count} re-stipulates already this task",
            f"  surprise dims (any_surprised={self.surprise_dims.get('any_surprised')}):",
            f"    {json.dumps(self.surprise_dims)[:600]}",
            f"",
            f"PLAN TREE:",
            self.plan_tree_outline[:1200],
            f"",
            f"RECENT TOOL ACTIVITY (last few iters):",
        ]
        for tc in self.recent_tool_calls[-5:]:
            lines.append(
                f"  - {tc.get('tool','?')}({str(tc.get('args',''))[:80]}) "
                f"→ {(tc.get('output_summary','') or '')[:120]}"
            )
            ar = tc.get("attestor_reason")
            if ar:
                lines.append(f"    attestor: {ar[:160]}")
        return "\n".join(lines)


class Resolver(ABC):
    """Interface for escalation policy. Implementations decide what to do
    when an attention step has stalled."""

    @abstractmethod
    def resolve(self, ctx: StallContext) -> ResolverDecision:
        """Return the next action to take for the stalled attention step."""
        ...


class StubResolver(Resolver):
    """Test helper — always returns a fixed decision."""

    def __init__(self, fixed: ResolverDecision = "forward"):
        self.fixed: ResolverDecision = fixed

    def resolve(self, ctx: StallContext) -> ResolverDecision:
        return self.fixed


class HeuristicResolver(Resolver):
    """Cheap no-LLM resolver. Used when LLM is unavailable or for cold-start.

    Rules:
      - if restip_count is already maxed (>= 5), go forward (give up replanning)
      - if attention intent is verify and verifier_match is False repeatedly,
        go backward (the previous mutate was probably wrong)
      - else restipulate
    """

    def resolve(self, ctx: StallContext) -> ResolverDecision:
        if ctx.restip_count >= 5:
            return "forward"
        if ctx.attention_intent == "verify":
            if not ctx.surprise_dims.get("verifier_match", True):
                return "backward"
        if ctx.stall_count >= 6:
            return "give_up"
        return "restipulate"


_LLM_RESOLVER_SYSTEM_PROMPT = """\
You are the RESOLVER for a contract-disciplined agent. The agent's inner \
loop reached a stall point: the current attention step has not progressed \
for several iterations.

You receive: the stuck attention step, the surprise dimensions, the plan \
tree, and recent tool activity.

Decide ONE of FOUR actions:

  forward      The step is essentially complete or close enough. COMMIT \
it and move on. Use this when the agent has gathered the needed information \
even if the attestor was strict, or when the step is investigate-type and \
further attempts won't add value.

  restipulate  Revise the plan to handle the divergence. Use this when the \
surprise reveals new information (e.g., the actual code is structured \
differently than the plan assumed) and the plan needs updating.

  backward     Abandon the current subtree and restore attention to the \
last completed mutate step. Use this when the current path looks like a \
dead end and a previous step's edit was likely wrong.

  give_up      Mark the contract FAILED. Use this only when the path \
forward is exhausted (max restipulates hit, multiple backwards already \
tried, attempting more iters would waste budget).

Output ONLY a JSON object with exactly this shape:
{"decision": "forward|restipulate|backward|give_up", "reason": "<one sentence>"}
"""


class LLMResolver(Resolver):
    """Default resolver — focused LLM call returning one of four decisions.

    Cost: ~500-800 tokens per invocation. Only fires at escalation points
    (stall_count >= threshold), not every iter.
    """

    def __init__(self, llm: Any, recorder: Any = None) -> None:
        self.llm = llm
        self.recorder = recorder

    def resolve(self, ctx: StallContext) -> ResolverDecision:
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
            if not result.text:
                return _safe_fallback(ctx)
            try:
                data = json.loads(result.text)
            except json.JSONDecodeError:
                return _safe_fallback(ctx)
            d = data.get("decision", "")
            if d not in ("forward", "restipulate", "backward", "give_up"):
                return _safe_fallback(ctx)
            return d  # type: ignore[return-value]
        except Exception:
            return _safe_fallback(ctx)


def _safe_fallback(ctx: StallContext) -> ResolverDecision:
    """When the LLMResolver fails for any reason, fall back to a conservative
    default that prevents infinite stall but doesn't fail the contract early."""
    if ctx.stall_count >= 5:
        return "forward"   # been here too long; move on
    return "restipulate"   # default — try to replan
