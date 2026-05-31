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
    "moonshotai/kimi-k2": (0.30, 1.20),       # rough; verify against OpenRouter
    "moonshotai/kimi-k2-0905": (0.30, 1.20),
    "z-ai/glm-4.5-air": (0.20, 1.00),
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
        max_retries: int = 3,
        retry_base_delay_s: float = 2.0,
    ) -> LLMResult:
        """One chat completion. Records llm_usage TraceEvent on success.

        R11: retries with exponential backoff on:
          - 0/0 token "degenerate" responses (provider rate-limit signal that
            comes back as an empty completion with no usage)
          - openai-client exceptions (RateLimitError, APIError, APITimeoutError)
        Backoff: retry_base_delay_s * 2^attempt (so 2s, 4s, 8s by default).
        Max attempts = max_retries + 1 (so 4 total tries by default).
        """
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
        resp = None
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001 — catch broad to detect any API failure
                last_exc = e
                # If this is the last attempt, re-raise. Otherwise backoff + retry.
                if attempt >= max_retries:
                    raise
                delay = retry_base_delay_s * (2 ** attempt)
                time.sleep(delay)
                continue

            # Check for "degenerate" response: 0 prompt tokens AND no content/tool_calls.
            usage = getattr(resp, "usage", None)
            t_in_probe = int(getattr(usage, "prompt_tokens", 0)) if usage else 0
            choice = resp.choices[0] if resp.choices else None
            msg = getattr(choice, "message", None) if choice else None
            has_content = bool(getattr(msg, "content", None))
            has_tools = bool(getattr(msg, "tool_calls", None))
            degenerate = (t_in_probe == 0) and not has_content and not has_tools

            if degenerate and attempt < max_retries:
                # Rate-limit / empty completion — backoff + retry
                delay = retry_base_delay_s * (2 ** attempt)
                time.sleep(delay)
                continue

            # Good response (or last attempt anyway) → fall through to parsing
            break

        wall_s = time.time() - t0
        if resp is None and last_exc is not None:
            raise last_exc

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
(description + expected_result + intent) and a STATE SUMMARY (recent tool \
outputs).

Your job: decide whether `expected_result` HOLDS based on the evidence in \
STATE SUMMARY. The step is complete iff expected_result is satisfied.

KEY INTERPRETATION RULES:
- expected_result may include OR clauses with NEGATIVE-RESULT possibilities
  (e.g., "X is defined here OR confirmed not defined here, implying inherited").
  A search that returned 0 matches IS valid evidence for the negative branch.
  If the agent searched the right place and got 0 results, that satisfies the
  negative branch — mark complete=true.
- expected_result is the CONTRACT. Do not require evidence the contract did
  not ask for.
- If expected_result mentions a fact and the STATE SUMMARY confirms or denies
  that fact within the contract, the step is complete.
- If the agent ran a tool with output suggesting "no match in requested
  location, found in broader scope" — this is also a complete answer to a
  "locate X" step (X is not at the asked location; we now know it's elsewhere).

Output ONLY a JSON object:
{"complete": true|false, "reason": "<single most important piece of evidence>"}
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

Each step has these fields:
  description     : imperative prose (what to do)
  intent          : "investigate" | "design" | "mutate" | "verify" | "other"
  suggested_tool  : ⭐ which tool the Performer should use for this step.
                    Pick from: bash, read, write, list, str_replace, multi_edit,
                    apply_patch, rg, fd, outline, goto_def, find_refs, git_diff,
                    git_status. Leave null only for pure design steps.
  suggested_args  : ⭐ exact args the Performer should pass. The Performer can
                    adapt if the world has changed, but a high-quality suggestion
                    SAVES iters because the Performer follows it directly.
                    Examples:
                      read: {"path": "/testbed/sympy/core/add.py"}
                      rg:   {"pattern": "def is_zero", "paths": ["/testbed/sympy/core/expr.py"]}
                      str_replace: {"path": "/testbed/foo.py",
                                    "old_str": "return False",
                                    "new_str": "return None"}
                      bash: {"command": "cd /testbed && python -m pytest --tb=short -q"}
  expects         : the planner's predictions for when the step is done:
    - durable_change   bool, true iff this step is expected to modify files
    - files_modified   list of expected modified file paths (only for mutate)
    - verifier_outcome "PASS" | "FAIL" | "N/A" (only meaningful for verify)
    - result           ⭐ prose statement of what should be true when this step
                       is done. The attestor uses 'result' as the done-criterion.

⭐ THE 'result' FIELD MUST INCLUDE NEGATIVE-RESULT POSSIBILITIES where they
apply. A search that returns 0 matches is VALID evidence — write the
'result' field so the attestor can recognize this. Examples:

  bad result:  "is_zero is defined in numbers.py"
  good result: "We have located is_zero in numbers.py, OR confirmed it is
                NOT defined there (which means it is inherited from a parent class)."

  bad result:  "all callers of foo() are identified"
  good result: "We have a list of caller files for foo(), OR confirmed that
                foo() has no callers outside its own module."

  for mutate (no negative case needed):
  good result: "settings.py has been modified: the SSL env vars (PGSSLMODE, ...)
                are appended to the env dict returned by settings_to_cmd_args_env."

Rules:
- If ENVIRONMENT facts provide an oracle test command, the LAST step of \
  VERIFY MUST be "Run <oracle command> and confirm it exits 0." with \
  verifier_outcome=PASS.
- If ENVIRONMENT provides an INITIAL_TEST_FAILURE, use it to make mutate \
  steps SPECIFIC: name the exact function, the exact field, the exact value.
- Plans should be 8-20 steps across 3-5 phases. Brief tasks smaller; do not pad.
- DO NOT execute anything. DO NOT include reasoning. Output ONLY the JSON.

Output a JSON object matching this exact shape:
{
  "phases": [
    {
      "name": "MAP",
      "steps": [
        {
          "description": "Read settings_to_cmd_args_env in postgresql/client.py",
          "intent": "investigate",
          "suggested_tool": "read",
          "suggested_args": {"path": "/testbed/django/db/backends/postgresql/client.py"},
          "expects": {
            "durable_change": false,
            "files_modified": [],
            "verifier_outcome": "N/A",
            "result": "We have read settings_to_cmd_args_env in postgresql/client.py and know its signature + body."
          }
        }
      ]
    },
    {
      "name": "IMPLEMENT",
      "steps": [
        {
          "description": "Add SSL env vars to settings_to_cmd_args_env",
          "intent": "mutate",
          "suggested_tool": "str_replace",
          "suggested_args": {
            "path": "/testbed/django/db/backends/postgresql/client.py",
            "old_str": "env = {}",
            "new_str": "env = {'PGSSLMODE': options.get('sslmode'), ...}"
          },
          "expects": {
            "durable_change": true,
            "files_modified": ["/testbed/django/db/backends/postgresql/client.py"],
            "verifier_outcome": "N/A",
            "result": "client.py has SSL env vars in the env dict returned by settings_to_cmd_args_env"
          }
        }
      ]
    },
    {
      "name": "VERIFY",
      "steps": [
        {
          "description": "Run the oracle test command",
          "intent": "verify",
          "suggested_tool": "bash",
          "suggested_args": {"command": "cd /testbed && python -m pytest --tb=short -q"},
          "expects": {
            "durable_change": false,
            "files_modified": [],
            "verifier_outcome": "PASS",
            "result": "pytest exits 0; targeted regression tests pass"
          }
        }
      ]
    }
  ]
}
"""


REVISOR_SYSTEM_PROMPT = """\
You are the REVISOR for a contract-disciplined agent.

The Stipulator already wrote the full plan tree (strategic, A-to-Z). Your job \
is NARROW: emit a small list of TREE-EDIT OPS that revise the existing tree \
in response to a stall. You DO NOT plan A-Z; you do not invent a new plan. \
You PATCH one region.

If a step is fine, do nothing to it. If a step turned out wrong or vague, \
emit ops to fix that step or its neighborhood.

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

# Legacy alias (do not remove until v1.7+; activator_v2 still references old name in trace events)
STIPULATOR_INLOOP_SYSTEM_PROMPT = REVISOR_SYSTEM_PROMPT


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
    """Validate via Pydantic and return a phased plan dict, or None on failure.

    R9: replaces ~50 lines of manual field coercion with PhasedPlan.model_validate_json.
    Pydantic enforces field types, normalizes intent / phase name enums, coerces
    Unknown intents → 'other', unknown phase names → 'OTHER'. Steps with empty
    descriptions are dropped post-validation.
    """
    from cat.plan_schema import PhasedPlan
    from pydantic import ValidationError

    try:
        plan = PhasedPlan.model_validate_json(text)
    except (ValidationError, ValueError):
        # Be lenient: try to coerce unknown enums down to allowed values, then retry
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("phases"), list):
            return None
        for p in data.get("phases", []):
            if not isinstance(p, dict):
                continue
            if p.get("name") not in ("MAP", "DESIGN", "IMPLEMENT", "VERIFY", "OTHER"):
                p["name"] = "OTHER"
            for s in p.get("steps", []) or []:
                if not isinstance(s, dict):
                    continue
                if s.get("intent") not in ("investigate", "design", "mutate", "verify", "other"):
                    s["intent"] = "other"
                exp = s.get("expects")
                if not isinstance(exp, dict):
                    s["expects"] = {}
        try:
            plan = PhasedPlan.model_validate(data)
        except ValidationError:
            return None

    # Drop steps with empty descriptions and empty phases
    cleaned_phases: list[dict[str, Any]] = []
    for phase in plan.phases:
        steps = [s for s in phase.steps if (s.description or "").strip()]
        if steps:
            cleaned_phases.append({
                "name": phase.name,
                "steps": [s.model_dump() for s in steps],
            })
    if not cleaned_phases:
        return None
    return {"phases": cleaned_phases}


def call_revisor(
    llm: LLM,
    obligation: str,
    plan_tree_outline: str,
    attention_id: Optional[str],
    state_class_summary: str,
    recent_trace: str,
    surprise_dims: dict[str, Any],
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[list[dict[str, Any]]], LLMResult]:
    """Revisor — local tree-edit ops in response to a stall.

    Distinct from Stipulator (which is strategic, runs once at start).
    Revisor revises a region of the existing plan tree based on observed
    surprise. Returns (ops_list_or_None, llm_result).
    """
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
        {"role": "system", "content": REVISOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="revisor",
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

# Backward-compat: previous name for the Revisor role; do not remove until v1.8+
call_stipulator_inloop = call_revisor
