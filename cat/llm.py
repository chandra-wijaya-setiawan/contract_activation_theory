"""LLM client: OpenAI-compatible (OpenRouter) chat + tool-calling, with token
harvest into TraceEvents.

Three contract-vocabulary roles used by the Activator (see PRD 1.6.2.9):
    1. PERFORMER (was "executor") — given obligation + history + tool schemas,
       emits one tool call (or a final text). Carries out the performance act.
    2. STIPULATOR (was "planner") — given obligation, articulates the
       performance terms as an ordered checklist (Plan). Strategic; does not
       bypass the criterion check.
    3. ATTESTOR (was "judge") — given a step description + state summary,
       attests whether the step appears complete and what is still missing.
       Advisory only — the final verdict is always a deterministic
       Criterion.predicate.

Pricing is OpenRouter usage when present; otherwise estimated from a small
table. Estimates exist so cost is non-zero even when the provider omits a
usage block — accuracy is reconciled against OpenRouter's billing endpoint
later if needed.
"""
from __future__ import annotations
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import OpenAI

from cat.trace import Recorder, TraceEvent, utcnow_iso


# Per-1M-token pricing (USD) for cost estimation. Reconcile via OpenRouter
# billing if used for anything beyond comparative analysis.
_PRICING: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-v4-flash": (0.20, 0.50),
    "deepseek/deepseek-v4-pro": (0.85, 2.50),
    "deepseek/deepseek-chat-v3.1": (0.27, 1.10),
    "deepseek/deepseek-chat-v3-0324": (0.27, 1.10),
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
}


def estimate_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pi, po = _PRICING.get(_strip_provider(model), (0.5, 2.0))
    return (tokens_in * pi + tokens_out * po) / 1_000_000.0


def _strip_provider(model: str) -> str:
    if model.startswith("openrouter/"):
        return model[len("openrouter/"):]
    return model


@dataclass
class LLMResult:
    text: Optional[str]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    model: str = ""
    raw: dict = field(default_factory=dict)


class LLM:
    """Thin OpenAI-compatible wrapper. One instance per agent run.

    All calls go through `chat()`; tool-calling, plain chat, JSON-mode chat
    all funnel here for uniform token harvesting.
    """

    def __init__(
        self,
        model: str = "openrouter/deepseek/deepseek-v4-flash",
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        recorder: Optional[Recorder] = None,
        parent_event_id: Optional[str] = None,
    ):
        self.model = model
        self.api_model = _strip_provider(model)
        self.recorder = recorder
        self.parent_event_id = parent_event_id
        self.client = OpenAI(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY"),
            base_url=base_url,
            default_headers={
                "HTTP-Referer": "https://github.com/cat-agent",
                "X-Title": "CAT Agent",
            },
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.2,
        role_tag: str = "executor",
        parent_event_id: Optional[str] = None,
    ) -> LLMResult:
        """One chat completion. Records llm_usage TraceEvent on success."""
        kwargs: dict[str, Any] = {
            "model": self.api_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        t0 = time.time()
        resp = self.client.chat.completions.create(**kwargs)
        wall_s = time.time() - t0

        choice = resp.choices[0]
        msg = choice.message
        tool_calls_list: list[dict[str, Any]] = []
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}
                tool_calls_list.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        usage = getattr(resp, "usage", None)
        t_in = int(getattr(usage, "prompt_tokens", 0)) if usage else 0
        t_out = int(getattr(usage, "completion_tokens", 0)) if usage else 0
        usd = estimate_usd(self.model, t_in, t_out)

        result = LLMResult(
            text=msg.content,
            tool_calls=tool_calls_list,
            finish_reason=choice.finish_reason or "",
            tokens_in=t_in,
            tokens_out=t_out,
            usd=usd,
            model=self.model,
            raw={"wall_s": wall_s, "role_tag": role_tag},
        )

        if self.recorder is not None:
            # parent_event_id (per-call) takes precedence over self.parent_event_id
            # (LLM-instance default). Each chat call chains to the event that
            # triggered it (e.g., a capability_invoke or contract_decompose).
            chat_parent = parent_event_id if parent_event_id is not None else self.parent_event_id
            self.recorder.append(TraceEvent(
                event_id=f"llm-{uuid.uuid4().hex[:8]}",
                parent_id=chat_parent,
                ts=utcnow_iso(),
                kind="llm_usage",
                payload={
                    "in": t_in,
                    "out": t_out,
                    "usd": usd,
                    "model": self.model,
                    "role": role_tag,
                    "wall_s": round(wall_s, 3),
                    "finish_reason": result.finish_reason,
                    "n_tool_calls": len(tool_calls_list),
                },
            ))

        return result


# ── Role helpers ────────────────────────────────────────────────────────────


STIPULATOR_SYSTEM_PROMPT = """\
You are the STIPULATOR role in a contract-disciplined agent. Your job is to \
stipulate the performance terms of a user obligation as an ordered checklist of \
concrete, verifiable steps. \

Rules:
- Each step MUST be testable by examining files, command outputs, or exit codes.
- Steps must be ordered: each step's success precondition is the previous step's completion.
- Prefer 3-8 steps. Fewer if the task is trivial. More only if truly necessary.
- Each step must describe a concrete state change or action: install X, write Y, run Z, \
edit file F. DO NOT include investigation or research steps (e.g. "identify", "check what \
version", "examine", "determine"). The executor will run any needed diagnostics without \
a dedicated plan step.
- Be SPECIFIC about WHAT to change. If fixing a function, name the function and \
the exact change required (e.g. "add PGSSLMODE env var to the ssl_params dict in \
django/db/backends/postgresql/client.py"), not just "fix SSL handling".
- If ENVIRONMENT facts include an INITIAL_TEST_FAILURE, use it to derive exact \
changes needed — name the specific values, fields, or code paths the test expects.
- If ENVIRONMENT facts provide an oracle test command, make your LAST step \
"Run <oracle command> and confirm it exits 0."
- If ENVIRONMENT facts are provided, your plan MUST be grounded in them. Use the actual \
workdir, actual test command, and actual file paths shown — do not assume or invent them.
- DO NOT execute anything. DO NOT include reasoning. Output ONLY the JSON object.

Output a JSON object with this exact shape:
{
  "steps": [
    {"id": "s1", "description": "<one short imperative sentence describing an action>"},
    {"id": "s2", "description": "..."}
  ]
}
"""


ATTESTOR_SYSTEM_PROMPT = """\
You are the ATTESTOR role in a contract-disciplined agent. You receive a STEP \
description and a STATE SUMMARY (recent tool outputs + workdir snapshot). \

Your job: decide whether the step appears COMPLETE based on the evidence.

Rules:
- Be strict. If evidence is missing or ambiguous, mark complete=false.
- Briefly state the single most important piece of evidence (or what is missing).
- DO NOT propose actions. The executor decides what to do next.

Output ONLY a JSON object:
{"complete": true|false, "reason": "<short>"}
"""


def call_stipulator(
    llm: LLM,
    obligation: str,
    representations: "Optional[list[Any]]" = None,
    parent_event_id: Optional[str] = None,
) -> tuple[list[dict[str, str]], LLMResult]:
    """Plan + raw LLMResult so the caller can charge full cost to the contract.

    Returns (steps, result). `steps` is a list of {"id", "description"} dicts
    (empty on parse failure). `result` carries tokens, usd, wall_s — the caller
    is responsible for charging them to contract.cost via _charge_llm().
    """
    env_section = ""
    if representations:
        lines: list[str] = []
        for r in representations:
            evidence_lines = r.evidence.strip().splitlines()
            evidence_preview = "\n    ".join(ln for ln in evidence_lines[:12] if ln.strip())
            lines.append(f"  {r.key}:\n    {evidence_preview}")
        env_section = "\n\nENVIRONMENT (verified facts — ground your plan in these):\n" + "\n\n".join(lines)

    messages = [
        {"role": "system", "content": STIPULATOR_SYSTEM_PROMPT},
        {"role": "user", "content": f"OBLIGATION:\n{obligation}{env_section}"},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="stipulator",
        temperature=0.1,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return [], result
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return [], result
    raw_steps = data.get("steps") or []
    steps = [
        {"id": str(s.get("id", f"s{i+1}")), "description": str(s.get("description", ""))}
        for i, s in enumerate(raw_steps)
        if isinstance(s, dict) and s.get("description")
    ]
    return steps, result


def call_attestor(
    llm: LLM, step_description: str, state_summary: str,
    parent_event_id: Optional[str] = None,
) -> tuple[bool, str, LLMResult]:
    """Verdict + raw LLMResult so the caller can charge full cost to the contract.

    Returns (complete, reason, result). Conservative on parse failure
    (complete=False). The caller is responsible for charging result's tokens,
    usd, and wall_s to contract.cost via _charge_llm().
    """
    messages = [
        {"role": "system", "content": ATTESTOR_SYSTEM_PROMPT},
        {"role": "user", "content": f"STEP: {step_description}\n\nSTATE SUMMARY:\n{state_summary}"},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="attestor",
        temperature=0.0,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return False, "attestor returned no text", result
    try:
        data = json.loads(result.text)
        return (
            bool(data.get("complete", False)),
            str(data.get("reason", ""))[:300],
            result,
        )
    except json.JSONDecodeError:
        return False, "attestor JSON parse error", result


# Backward-compat aliases (read-only — DO NOT remove until v1.7+)
# Allow tests and external code referencing the old names to keep working.
call_planner = call_stipulator
call_judge = call_attestor


# ──────────────────────────────────────────────────────────────────────────
# v2 (iteration_loop_redesign) — phased Stipulator
# Cold-start: phased PlanTree per docs/iteration.md §6.1
# In-loop:    list of tree-edit ops per §6.2
# ──────────────────────────────────────────────────────────────────────────

STIPULATOR_COLD_SYSTEM_PROMPT = """\
You are the STIPULATOR for a contract-disciplined agent (v2 design). \
You decompose a user obligation into a working PLAN TREE organized by phases.

INVESTIGATION is a first-class part of planning — do NOT skip it. A good \
plan articulates what you need to learn before mutating, then what to \
mutate, then how to verify.

Phases (use this fixed vocabulary):
  - MAP       — investigation: read files, understand contracts, identify call sites
  - DESIGN    — decisions: return-type choice, signature change strategy
  - IMPLEMENT — mutate: edit code, write tests
  - VERIFY    — run tests / confirm criteria

Each step has:
  - description: prose imperative
  - intent ∈ {investigate, design, mutate, verify}
  - expects: structured prediction the activator can mechanically verify:
      - durable_change: bool      — does this step modify files?
      - files_modified: [paths]   — which files (empty if not a mutate)
      - verifier_outcome: "PASS"|"FAIL"|"N/A"  — what test outcome to expect
      - knowledge_gained: str (only for investigate steps; optional)

Rules:
- If ENVIRONMENT facts provide an oracle test command, the LAST step of \
  VERIFY MUST be "Run <oracle command> and confirm it exits 0." with \
  verifier_outcome=PASS.
- If ENVIRONMENT provides an INITIAL_TEST_FAILURE, use it to make mutate \
  steps SPECIFIC: name the exact function, the exact field, the exact value.
- Plans should be 8-20 steps across 3-5 phases (typically 4-6 MAP + 1-2 \
  DESIGN + 2-5 IMPLEMENT + 1-2 VERIFY). Tasks like "add SSL env vars to \
  postgres dbshell client" need at minimum: read the client.py, read the \
  base client.py, read the test_postgresql.py, identify the env-var \
  mapping, decide return-type, edit the function, run the test. That's \
  7+ steps. Brief tasks may be smaller; do not pad.
- DO NOT execute anything. DO NOT include reasoning. Output ONLY the JSON.

Output a JSON object with this exact shape:
{
  "phases": [
    {
      "name": "MAP",
      "steps": [
        {
          "description": "<imperative>",
          "intent": "investigate",
          "expects": {
            "durable_change": false,
            "files_modified": [],
            "verifier_outcome": "N/A",
            "knowledge_gained": "<what you'll know after this>"
          }
        }
      ]
    },
    { "name": "DESIGN", "steps": [...] },
    { "name": "IMPLEMENT", "steps": [...] },
    { "name": "VERIFY", "steps": [...] }
  ]
}
"""


STIPULATOR_INLOOP_SYSTEM_PROMPT = """\
You are the STIPULATOR for a contract-disciplined agent (v2 design), \
in IN-LOOP mode. The plan tree exists; you are emitting a small list of \
TREE-EDIT OPS to react to a surprise.

DO NOT regenerate the full plan. Only emit ops that address the surprise.

Available ops (each MUST include a `reflection: str` explaining the choice):

  COMMIT     {"op":"COMMIT","node_id":"X","reason":"...","reflection":"..."}
  CHECKOUT   {"op":"CHECKOUT","node_id":"X","reflection":"..."}
  DROP       {"op":"DROP","node_id":"X","reason":"...","reflection":"..."}
  COMMIT_NEW {"op":"COMMIT_NEW","parent_id":"PHASE_ID","new_step":{description,intent,expects},"reflection":"..."}
  REFINE     {"op":"REFINE","node_id":"X","substeps":[...],"reflection":"..."}
  BRANCH     {"op":"BRANCH","node_id":"X","alt_steps":[...],"reflection":"..."}
  MERGE     {"op":"MERGE","branch_id":"X-alt-N","target_id":"X","reflection":"..."}

When to use which (surprise → op):
- step seemed to do nothing → DROP current; COMMIT_NEW with different action
- step partially worked but is bigger than expected → REFINE into substeps
- repeated failure on same approach → BRANCH with alternative approach
- discovered missing work → COMMIT_NEW
- finished and ready to move on → COMMIT + CHECKOUT next

Rules:
- Every op MUST include a `reflection` field (a short sentence on WHY this op).
- Output ONLY JSON. No prose around it.

Output shape:
{
  "ops": [
    { "op": "...", "reflection": "...", ...op-specific... },
    ...
  ]
}
"""


def call_stipulator_phased(
    llm: LLM,
    obligation: str,
    representations: "Optional[list[Any]]" = None,
    plan_templates: "Optional[list[dict[str,Any]]]" = None,
    anti_templates: "Optional[list[dict[str,Any]]]" = None,
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], LLMResult]:
    """Cold-start phased Stipulate. Returns (phased_plan_dict_or_None, llm_result).

    phased_plan_dict shape: {"phases": [{"name", "steps": [...]}, ...]}
    None if JSON unparseable or schema invalid — caller should fall back.
    """
    env_section = ""
    if representations:
        lines: list[str] = []
        for r in representations:
            evidence_lines = (r.evidence or "").strip().splitlines()
            evidence_preview = "\n    ".join(ln for ln in evidence_lines[:12] if ln.strip())
            lines.append(f"  {r.key}:\n    {evidence_preview}")
        env_section = "\n\nENVIRONMENT (verified facts — ground your plan in these):\n" + "\n\n".join(lines)

    templates_section = ""
    if plan_templates:
        templates_section = "\n\nPRIOR PASS PLAN TEMPLATES (similar obligation_class):\n"
        for i, t in enumerate(plan_templates[:3], 1):
            templates_section += f"  template {i}: {json.dumps(t, indent=2)[:800]}\n"
    if anti_templates:
        templates_section += "\n\nPRIOR FAIL PATTERNS (avoid these subtrees):\n"
        for i, t in enumerate(anti_templates[:3], 1):
            templates_section += f"  anti-template {i}: {json.dumps(t, indent=2)[:600]}\n"

    messages = [
        {"role": "system", "content": STIPULATOR_COLD_SYSTEM_PROMPT},
        {"role": "user", "content": f"OBLIGATION:\n{obligation}{env_section}{templates_section}"},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="stipulator",
        temperature=0.1,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return None, result
    parsed = _parse_phased_plan(result.text)
    return parsed, result


def _parse_phased_plan(text: str) -> Optional[dict[str, Any]]:
    """Validate and return a phased plan dict, or None on failure."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "phases" not in data:
        return None
    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        return None
    cleaned_phases: list[dict[str, Any]] = []
    for p in phases:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "OTHER")
        if name not in ("MAP", "DESIGN", "IMPLEMENT", "VERIFY", "OTHER"):
            name = "OTHER"
        steps = p.get("steps") or []
        if not isinstance(steps, list):
            steps = []
        cleaned_steps: list[dict[str, Any]] = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            desc = str(s.get("description", "")).strip()
            if not desc:
                continue
            intent = s.get("intent", "other")
            if intent not in ("investigate", "design", "mutate", "verify"):
                intent = "other"
            expects = s.get("expects") or {}
            if not isinstance(expects, dict):
                expects = {}
            # Coerce expects fields
            ec = {
                "durable_change": bool(expects.get("durable_change", False)),
                "files_modified": list(expects.get("files_modified", []) or []),
                "verifier_outcome": str(expects.get("verifier_outcome", "N/A")),
            }
            if "knowledge_gained" in expects:
                ec["knowledge_gained"] = str(expects.get("knowledge_gained", ""))
            cleaned_steps.append({"description": desc, "intent": intent, "expects": ec})
        if cleaned_steps:
            cleaned_phases.append({"name": name, "steps": cleaned_steps})
    if not cleaned_phases:
        return None
    return {"phases": cleaned_phases}


def call_stipulator_inloop(
    llm: LLM,
    obligation: str,
    plan_tree_outline: str,
    attention_id: Optional[str],
    state_class_summary: str,
    recent_trace: str,
    surprise_dims: dict[str, Any],
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[list[dict[str, Any]]], LLMResult]:
    """In-loop Stipulate. Returns (ops_list_or_None, llm_result)."""
    user_msg = (
        f"OBLIGATION (brief):\n{obligation[:500]}\n\n"
        f"CURRENT PLAN TREE:\n{plan_tree_outline}\n\n"
        f"ATTENTION: {attention_id}\n\n"
        f"STATE_CLASS SUMMARY: {state_class_summary}\n\n"
        f"RECENT TRACE (last few iters):\n{recent_trace[:2000]}\n\n"
        f"SURPRISE DIMENSIONS (what mismatched):\n{json.dumps(surprise_dims, indent=2)}\n\n"
        f"Emit a minimal list of tree-edit ops to react. Include `reflection` on every op."
    )
    messages = [
        {"role": "system", "content": STIPULATOR_INLOOP_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="stipulator",
        temperature=0.1,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return None, result
    ops = _parse_inloop_ops(result.text)
    return ops, result


def _parse_inloop_ops(text: str) -> Optional[list[dict[str, Any]]]:
    """Validate per docs/iteration.md §6.4. Returns clean ops list or None."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ops = data.get("ops")
    if not isinstance(ops, list):
        return None
    valid_ops = {"COMMIT", "CHECKOUT", "DROP", "COMMIT_NEW", "REFINE", "BRANCH", "MERGE"}
    required_fields = {
        "COMMIT": {"node_id", "reason"},
        "CHECKOUT": {"node_id"},
        "DROP": {"node_id", "reason"},
        "COMMIT_NEW": {"parent_id", "new_step"},
        "REFINE": {"node_id", "substeps"},
        "BRANCH": {"node_id", "alt_steps"},
        "MERGE": {"branch_id", "target_id"},
    }
    out: list[dict[str, Any]] = []
    for o in ops:
        if not isinstance(o, dict):
            continue
        kind = o.get("op")
        if kind not in valid_ops:
            continue
        req = required_fields[kind]
        if not all(k in o for k in req):
            continue
        if not o.get("reflection"):
            # require reflection per §6.3
            continue
        out.append(o)
    return out or None

PLANNER_SYSTEM_PROMPT = STIPULATOR_SYSTEM_PROMPT
JUDGE_SYSTEM_PROMPT = ATTESTOR_SYSTEM_PROMPT
