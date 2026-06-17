"""Attestor — judges whether a hypothesis attempt succeeded or failed.

Called once per hypothesis cycle (only on oracle failure) to decide:
  - "new_hypothesis" → try again with updated knowledge
  - "give_up" → task is unrecoverable

See design.v3.md §"Attestor (called only on oracle failure)".
"""
from __future__ import annotations

import json
from typing import Any, Optional

from cat.contract.schemas import AttestorV3Schema
from cat.llm.client import LLM, LLMResult

ATTESTOR_V3_SYSTEM_PROMPT = """\
You are the ATTESTOR. The oracle failed after a hypothesis attempt.
Decide: should we try a new hypothesis, or give up entirely?

You receive:
- What the Performer tried (hypothesis + verdict + reason)
- What was learned (findings from tool outputs)
- What has been tried before (prior failed hypotheses)

Output ONE of two verdicts:
  new_hypothesis — a different approach could still work; this hypothesis
                   was wrong but the task is solvable.
  give_up        — the task is unrecoverable; the obligation cannot be
                   satisfied with the available tools and information.

Rules:
1. ALWAYS choose new_hypothesis if fewer than 3 hypotheses have been tried.
   A single failed hypothesis is not enough evidence to give up.
   "Target not found" or "modification failed" means the hypothesis was WRONG,
   not that the task is impossible. Try a different location or approach.
2. Prefer new_hypothesis unless ALL of these are true:
   - 3+ hypotheses have been tried
   - All targeted different locations (not just the same wrong location)
   - The task obligation itself seems unfulfillable (no plausible fix exists)
3. give_up only when the task is genuinely impossible (e.g. the obligation
   asks to fix something that is already correct, or requires capabilities
   that don't exist in the environment).
4. Your diagnosis must name a DIFFERENT target for the next hypothesis.

Output ONLY a JSON object:
{
  "verdict":   "new_hypothesis" | "give_up",
  "reason":    "<specific evidence for your verdict>",
  "diagnosis": "<what was wrong and what to try next>"
}
"""


def call_attestor_v3(
    llm: LLM,
    performer_result: Any,
    knowledge: Any = None,
    prior_attempts: Optional[list[dict]] = None,
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[AttestorV3Schema], LLMResult]:
    """Call the Attestor to judge a failed hypothesis attempt."""
    from pydantic import ValidationError

    verdict_str = getattr(performer_result, "verdict", "exhausted")
    reason_str = getattr(performer_result, "performer_reason", "") or ""
    findings = getattr(knowledge, "findings", {}) if knowledge else {}
    oracle_failures = getattr(knowledge, "oracle_failures", []) if knowledge else []

    prior_str = ""
    if prior_attempts:
        for i, pa in enumerate(prior_attempts[-3:], 1):
            h = pa.get("hypothesis")
            loc = getattr(h, "location", "?") if h else "?"
            r = pa.get("reason", "")[:150]
            prior_str += f"\n  Attempt {i}: location={loc[:60]}, verdict={pa.get('verdict','?')}\n  reason: {r}"

    findings_str = ""
    for loc, finding in list(findings.items())[:5]:
        findings_str += f"\n  {loc}: {str(finding)[:150]}"

    oracle_str = ""
    for f in oracle_failures[-2:]:
        oracle_str += f"\n  {str(f)[:200]}"

    user_content = (
        f"PERFORMER RESULT:\n"
        f"  verdict: {verdict_str}\n"
        f"  reason:  {reason_str[:400]}\n"
        f"\nFINDINGS FROM EXECUTION:{findings_str or ' (none)'}\n"
        f"\nORACLE FAILURES:{oracle_str or ' (none)'}\n"
        f"\nPRIOR FAILED ATTEMPTS:{prior_str or ' (none)'}"
    )
    messages = [
        {"role": "system", "content": ATTESTOR_V3_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="attestor",
        temperature=0.0,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return AttestorV3Schema(verdict="new_hypothesis", reason="attestor returned no text"), result
    try:
        data = json.loads(result.text)
        v = AttestorV3Schema(**data)
        return v, result
    except (json.JSONDecodeError, ValidationError, TypeError):
        return AttestorV3Schema(verdict="new_hypothesis", reason="attestor parse failed"), result
