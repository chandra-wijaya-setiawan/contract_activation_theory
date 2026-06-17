"""Performer plan — continuation plan generation for H2+.

When there are prior attempts, the Performer benefits from a plan that
anchors current_state to what was already tried so the model continues
rather than restarts.

See design.v3.md §"Inner Loop (Performer)".
"""
from __future__ import annotations

from typing import Any, Optional

from cat.contract.schemas import PerformerPlanSchema
from cat.llm.client import LLM, LLMResult, call_structured

PERFORMER_V3_SYSTEM_PROMPT = """\
You are the PERFORMER. You execute a specific hypothesis using tools.

On each step, output ONLY a valid JSON object with this exact schema:
{
  "thinking": "<your reasoning about what to do next>",
  "intent": "<look|change|check|done|give_up>",
  "tools": [{"name": "<tool_name>", "arguments": {<tool_args>}}],
  "reason": "<required for done/give_up, null otherwise>",
  "expect": "<what you expect after executing tools, null for done/give_up>",
  "then": "<look|change|check|done|give_up, null for done/give_up>"
}

Field rules:
- thinking: ALWAYS provide, explain your reasoning
- intent: ALWAYS provide, one of look/change/check/done/give_up
- tools: REQUIRED for look/change/check (1+ tool objects), EMPTY [] for done/give_up
- reason: REQUIRED for done/give_up (why complete or why giving up), null otherwise
- expect: REQUIRED for look/change/check (what you expect to see), null for done/give_up
- then: REQUIRED for look/change/check (what should happen next), null for done/give_up

Each tool object must have:
  - name: one of the available tools in the environment
  - input: object with tool-specific arguments

Intent meanings:
  look   — observe/explore (inspect the target, search, list)
  change — modify the target (apply the change the hypothesis describes)
  check  — verify with oracle (run the validation command)
  done   — task complete (provide reason)
  give_up — can't solve this hypothesis (provide reason)

Rules:
1. Output ONLY the JSON object, no other text.
2. For look/change/check: tools array must have 1+ entries.
3. For done/give_up: tools must be empty [], reason must be non-empty.
4. You have MAX 5 steps total — use them efficiently.
5. After ONE look, act. Do not keep re-inspecting.
6. If the target does not exist: intent=give_up with reason.
7. If the change succeeds and oracle passes: intent=done with reason.
8. Stay within the hypothesis scope.
9. Multiple tools allowed in one step for parallel exploration.
10. Every response MUST be valid JSON matching the schema above.
"""

PERFORMER_PLAN_PROMPT = """\
You are the PERFORMER. Before acting, output a 2-3 step plan.

Steps must follow this pattern exactly:
  Step 1: inspect — read the target to confirm its current state
  Step 2: apply   — make the change the hypothesis describes
  Step 3: verify  — run the oracle to confirm

If "RESULTS SO FAR" is provided, you are continuing from a prior attempt:
  - Set current_state to summarise what was already done and what failed.
  - Skip steps that are already complete. Start from the next pending step.
  - If the target was already inspected, skip step 1.
  - If a change was already applied but oracle failed, describe what to correct in step 2.

Keep each action to one sentence. Do NOT invent extra steps beyond inspect/apply/verify.

Output ONLY a JSON object:
{
  "current_state": "<already done / known — 'Not started' if first attempt>",
  "steps": [
    {"action": "<one sentence>", "rationale": "<why>", "status": "pending"},
    {"action": "apply the change the hypothesis describes", "rationale": "<why>", "status": "pending"},
    {"action": "verify with oracle", "rationale": "confirm the change satisfies the criterion", "status": "pending"}
  ],
  "confidence": "high" | "medium" | "low"
}
"""


def build_performer_plan_messages(
    task: Any,
    prior_results: str = "",
) -> list[dict]:
    """Build the message list for the continuation plan LLM call."""
    hypothesis = task.hypothesis if task else None
    task_content = (
        f"OBLIGATION:\n{task.obligation if task else '?'}\n\n"
        f"HYPOTHESIS:\n"
        f"  Location: {getattr(hypothesis, 'location', '?')}\n"
        f"  Change:   {getattr(hypothesis, 'change', '?')}\n"
        f"  Predicts: {getattr(hypothesis, 'predicts', '?')}\n\n"
        f"ORACLE: {task.oracle if task else '?'}\n"
    )
    if task and task.context:
        task_content += f"\nDOMAIN CONTEXT:\n{task.context}\n"
    if prior_results:
        task_content += f"\nRESULTS SO FAR:\n{prior_results[:600]}\n"
        task_content += "\nRevise the plan based on what was learned above.\n"
    return [
        {"role": "system", "content": PERFORMER_PLAN_PROMPT},
        {"role": "user", "content": task_content},
    ]


def call_performer_plan(
    llm: LLM,
    task: Any,
    prior_results: str = "",
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[PerformerPlanSchema], LLMResult]:
    """Generate a continuation plan for the Performer."""
    messages = build_performer_plan_messages(task, prior_results)
    return call_structured(
        llm, messages, PerformerPlanSchema,
        role_tag="performer", max_tokens=4096, parent_event_id=parent_event_id,
    )
