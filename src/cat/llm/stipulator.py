"""Stipulator — generates hypotheses about how to satisfy the obligation.

The Stipulator is called once per hypothesis cycle. It receives:
  - The original obligation
  - Verified representations of the environment
  - Accumulated knowledge from prior attempts
  - Prior attempt summaries

It emits one HypothesisSchema: a specific, testable prediction.

See design.v3.md §"Stipulator" and §"HypothesisSchema".
"""
from __future__ import annotations

from typing import Any, Optional

from cat.contract.schemas import HypothesisSchema
from cat.llm.client import LLM, LLMResult, call_structured

STIPULATOR_V3_SYSTEM_PROMPT = """\
You are the STIPULATOR. Given an obligation and what is already known from
prior attempts, generate ONE specific hypothesis that will satisfy it.

A hypothesis is a single, testable prediction:
WHERE to act, WHAT to change semantically, WHAT will be true afterwards.

Rules:
1. Be specific. Name the exact target — a location, record, endpoint, or
   resource — that the change must be applied to.
2. Describe WHAT to change semantically. Do not prescribe exact execution
   steps or byte-level content. The Performer determines how to implement.
3. If prior knowledge identifies the target — use it directly.
4. If prior attempts failed — choose a DIFFERENT target or approach.
   Do not repeat a hypothesis that was already tried.
5. One hypothesis, one target, one semantic change.
6. CHOOSE YOUR STARTING POINT via 'start_from'. If "AVAILABLE STARTING POINTS"
   are listed, pick which prior state to build on:
   - Use "base" when prior changes were wrong/misdirected — discard them and
     start clean. This is the safe default.
   - Use a prior checkpoint (e.g. "h1") ONLY when that attempt's changes were
     partially correct and your new hypothesis extends them. Avoid stacking
     onto a checkpoint whose changes were clearly wrong — that compounds errors.

Output ONLY a JSON object:
{
  "location":   "<the specific target — as precise as possible>",
  "change":     "<what to change there — describe the intent, not the bytes>",
  "predicts":   "<what will be verifiably true after the change>",
  "basis":      "<one sentence: why this will work>",
  "start_from": "<'base' or a prior checkpoint label like 'h1'>"
}
"""


def build_stipulator_messages(
    obligation: str,
    representations: Optional[list[Any]] = None,
    knowledge: Any = None,
    prior_attempts: Optional[list[dict]] = None,
) -> list[dict]:
    """Build the message list for the Stipulator LLM call."""
    env_section = ""
    if representations:
        lines: list[str] = []
        for r in representations:
            evidence_lines = (r.evidence or "").strip().splitlines()
            # Show more lines for high-signal reps (initial_test_failure)
            n_lines = 30 if "initial_test" in (r.key or "") else 12
            preview = "\n    ".join(ln for ln in evidence_lines[:n_lines] if ln.strip())
            lines.append(f"  {r.key}:\n    {preview}")
        env_section = "\n\nENVIRONMENT (verified facts):\n" + "\n\n".join(lines)

    knowledge_section = ""
    if knowledge:
        findings = getattr(knowledge, "findings", {}) or {}
        if findings:
            knowledge_section += "\n\nKNOWLEDGE ALREADY ESTABLISHED (do NOT re-investigate):\n"
            for loc, finding in list(findings.items())[:10]:
                knowledge_section += f"  {loc}: {str(finding)[:200]}\n"
        oracle_failures = getattr(knowledge, "oracle_failures", []) or []
        if oracle_failures:
            knowledge_section += "\n\nORACLE FAILURES (most recent test run output — use this to diagnose):\n"
            for failure in oracle_failures[-2:]:
                knowledge_section += f"  {str(failure)[:800]}\n"

    prior_section = ""
    checkpoint_section = ""
    if prior_attempts:
        prior_section = "\n\nPRIOR HYPOTHESES THAT FAILED (do NOT repeat):\n"
        for i, pa in enumerate(prior_attempts[-3:], 1):
            h = pa.get("hypothesis")
            reason = pa.get("reason", "")[:200]
            cp = pa.get("checkpoint", "")
            if h:
                loc = getattr(h, "location", str(h))[:80]
                chg = getattr(h, "change", "")[:100]
                cp_tag = f" [checkpoint: {cp}]" if cp else ""
                prior_section += (
                    f"  {i}. Location: {loc}{cp_tag}\n"
                    f"     Tried: {chg}\n"
                    f"     Why failed: {reason}\n"
                )

        # Available checkpoints the next hypothesis can branch from.
        cp_labels = ["base"]
        for pa in prior_attempts:
            c = pa.get("checkpoint", "")
            if c and c not in cp_labels:
                cp_labels.append(c)
        checkpoint_section = (
            "\n\nAVAILABLE STARTING POINTS (set 'start_from' to one of these):\n"
            "  base — the original unmodified state (discard all prior changes)\n"
            + "".join(
                f"  {c} — state after that attempt's changes (build on them)\n"
                for c in cp_labels if c != "base"
            )
            + "Choose 'base' if prior changes were wrong; choose a prior checkpoint "
            "ONLY if its changes were partially correct and this hypothesis extends them.\n"
        )

    user_content = (
        f"OBLIGATION:\n{obligation}"
        f"{env_section}{knowledge_section}{prior_section}{checkpoint_section}"
    )
    return [
        {"role": "system", "content": STIPULATOR_V3_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def call_stipulator_v3(
    llm: LLM,
    obligation: str,
    representations: Optional[list[Any]] = None,
    knowledge: Any = None,
    prior_attempts: Optional[list[dict]] = None,
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[HypothesisSchema], LLMResult]:
    """Call the Stipulator to generate one hypothesis."""
    messages = build_stipulator_messages(obligation, representations, knowledge, prior_attempts)
    return call_structured(
        llm, messages, HypothesisSchema,
        role_tag="stipulator", max_tokens=4096, parent_event_id=parent_event_id,
    )
