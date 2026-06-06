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
    "nvidia/nemotron-3-ultra-550b-a55b": (0.0, 0.0),   # free tier
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
You are the ATTESTOR in a contract-disciplined agent. You receive:
- PERFORMER RESULT: what the Performer did and its self-verdict
- CURRENT STEP: what was planned (intent, description, expected_result)
- PLAN CONTEXT: prior committed steps + upcoming planned steps + the overall goal
- TOOL OUTPUTS: what the tools returned

Your job: make a PLAN-LEVEL verdict — not just "is this step done?" but \
"is the overall plan still sound?". Output one of four verdicts:

  complete     → Step satisfied AND plan still makes sense going forward.
  patch_plan   → Step not done, but plan strategy is correct — a local fix \
                 to the current node will unblock (wrong target, wrong args).
  restipulate  → Plan PREMISE is wrong — the approach itself is off, not \
                 just the execution. Fresh plan needed with failure context.
  give_up      → Unrecoverable — the obligation is impossible or the \
                 environment is fundamentally broken.

━━━ COST HIERARCHY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
complete     — free. Prefer it when there is reasonable evidence.
patch_plan   — costs one PlanPatcher call. Use for local failures.
restipulate  — costs a full re-plan + resets all progress. Use ONLY when \
               the plan premise is demonstrably wrong. If restipulate_count \
               ≥ 1, the bar rises sharply — default to patch_plan instead.

━━━ HARD CONSTRAINTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. DURABLE CHANGE MADE → never restipulate. If the Performer successfully \
   modified something (durable_change=True), the approach reached the right \
   place. Choose complete or patch_plan.

2. ANALYSIS/DESIGN STEP WITH NO WORK → patch_plan. If intent is \
   investigation or design and Performer only signalled done() without \
   calling any information-gathering or modification tool first, the step \
   produced no evidence. Require actual work before accepting completion.

3. EXECUTION ERROR IN VERIFY → patch_plan, not restipulate. If the verify \
   tool call itself errored (crash, broken environment, malformed command) \
   rather than producing a logical failure (assertion fail, wrong output), \
   the plan premise is not wrong — the execution had a problem. Use \
   patch_plan to fix the execution, not restipulate to throw away the plan.

4. REASON MUST CITE SPECIFIC EVIDENCE. For any verdict, your `reason` must \
   reference a specific piece of tool output — a file name, an error \
   message, a line number, a value. "The step appears done" or "environment \
   issues" are not acceptable. If you cannot point to specific evidence, \
   vote patch_plan (conservative) rather than complete or restipulate.

━━━ DECISION FLOW ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Performer said "completed":
  → Does tool output satisfy expected_result? YES → complete
  → Did Performer make a durable change with no prior work tools? → patch_plan
  → Do upcoming plan steps align with what was actually done? NO → restipulate

Performer said "exhausted":
  → Is the approach right (right target, just couldn't finish)? → patch_plan
  → Is the approach fundamentally wrong (wrong target, wrong scope)? → restipulate
  → restipulate_count ≥ 1? → strongly prefer patch_plan

Performer said "give_up":
  → Can a fresh plan with different approach succeed? → restipulate
  → Is the task genuinely impossible? → give_up

Output ONLY a JSON object:
{
  "verdict": "complete" | "patch_plan" | "restipulate" | "give_up",
  "reason": "<specific evidence from tool output — file, error, value>",
  "diagnosis": "<for patch_plan/restipulate: exactly what is wrong and why>"
}
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
    llm: LLM,
    step_description: str,
    state_summary: str,
    performer_verdict: str = "completed",
    performer_reason: str = "",
    prev_steps_summary: str = "",
    next_steps_summary: str = "",
    obligation_brief: str = "",
    restipulate_count: int = 0,
    patch_plan_count: int = 0,
    performer_durable_change: bool = False,
    parent_event_id: Optional[str] = None,
) -> tuple[str, str, str, LLMResult]:
    """Plan-aware Attestor (R37). Returns (verdict, reason, diagnosis, result).

    `verdict` is one of: "complete", "patch_plan", "restipulate", "give_up".
    Conservative on parse failure (verdict="patch_plan") — never silently advances.
    """
    plan_context = ""
    if prev_steps_summary:
        plan_context += f"\nPRIOR COMMITTED STEPS (what was accomplished):\n{prev_steps_summary}"
    if next_steps_summary:
        plan_context += f"\nUPCOMING PLANNED STEPS (where plan is going):\n{next_steps_summary}"
    if obligation_brief:
        plan_context += f"\nOVERALL GOAL:\n{obligation_brief}"

    cost_context = (
        f"\nCOST COUNTERS (already paid this run):\n"
        f"  restipulate_count={restipulate_count}  patch_plan_count={patch_plan_count}\n"
        f"  performer_durable_change={performer_durable_change}  "
        f"{'(file was modified — do NOT choose restipulate)' if performer_durable_change else ''}"
    )
    user_content = (
        f"PERFORMER RESULT:\n"
        f"  verdict: {performer_verdict}\n"
        f"  reason: {performer_reason[:400]}\n\n"
        f"CURRENT STEP:\n{step_description}\n\n"
        f"TOOL OUTPUTS (recent):\n{state_summary}"
        f"{plan_context}"
        f"{cost_context}"
    )
    messages = [
        {"role": "system", "content": ATTESTOR_SYSTEM_PROMPT},
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
        return "patch_plan", "attestor returned no text", "", result
    from cat.plan_schema import AttestorVerdict
    from pydantic import ValidationError
    try:
        v = AttestorVerdict.model_validate_json(result.text)
        verdict_str = getattr(v, "verdict", "complete" if v.complete else "patch_plan")
        diagnosis = getattr(v, "diagnosis", "")

        # Hard guard: if Performer made a durable change, restipulate is
        # forbidden — the Performer found and modified the right place.
        # Override to patch_plan so progress is not discarded.
        if performer_durable_change and verdict_str == "restipulate":
            verdict_str = "patch_plan"
            diagnosis = (
                f"[GUARD: restipulate blocked — durable change was made] {diagnosis}"
            )[:500]

        return verdict_str, v.reason[:300], diagnosis[:500], result
    except ValidationError:
        return "patch_plan", "attestor schema validation failed", "", result


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


PLAN_PATCHER_SYSTEM_PROMPT = """\
You are the PLAN PATCHER for a contract-disciplined agent.

The Stipulator already wrote the full plan tree (strategic, A-to-Z). Your job \
is NARROW: emit a small list of TREE-EDIT OPS that patch the existing tree \
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
- ⭐ REFINE intent-preservation: if the parent step has intent='mutate', at
  least ONE substep MUST also have intent='mutate' (the actual edit). Don't
  refine a mutate step into only investigate substeps — you would lose the
  edit and the agent would never write the fix. Same for verify: if parent
  is intent='verify', at least one substep must remain verify.
- Output ONLY JSON. No prose around it.

Output shape:
{
  "ops": [
    { "op": "...", "reflection": "...", ...op-specific... },
    ...
  ]
}
"""

# Legacy alias kept for back-compat with older trace events.
STIPULATOR_INLOOP_SYSTEM_PROMPT = PLAN_PATCHER_SYSTEM_PROMPT


def call_stipulator_phased(
    llm: LLM,
    obligation: str,
    representations: "Optional[list[Any]]" = None,
    plan_templates: "Optional[list[dict[str,Any]]]" = None,
    anti_templates: "Optional[list[dict[str,Any]]]" = None,
    extra_context: str = "",      # R37: failure context for restipulate calls
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

    extra_section = f"\n\n{extra_context}" if extra_context else ""
    messages = [
        {"role": "system", "content": STIPULATOR_COLD_SYSTEM_PROMPT},
        {"role": "user", "content": f"OBLIGATION:\n{obligation}{env_section}{templates_section}{extra_section}"},
    ]

    # R12 follow-up: retry-on-schema-failure. Cheap "flash" LLMs sometimes
    # collapse the phased schema into a v1-style flat array even though the
    # response is valid JSON; the previous behaviour silently fell through
    # to v1 call_stipulator and burned tokens running naked. We now retry up
    # to MAX_PHASED_RETRIES times with an explicit correction message before
    # giving up — when the caller sees None it MUST fail the contract, not
    # synthesize a flat plan.
    MAX_PHASED_RETRIES = 2  # → 3 total attempts
    last_result: Optional[LLMResult] = None
    for attempt in range(MAX_PHASED_RETRIES + 1):
        result = llm.chat(
            messages=messages,
            response_format={"type": "json_object"},
            role_tag="stipulator",
            temperature=0.1,
            parent_event_id=parent_event_id,
        )
        last_result = result
        if not result.text:
            # Degenerate or empty — the underlying chat() already retried on
            # this in R11; no point asking again in our loop. Bail.
            return None, result
        parsed = _parse_phased_plan(result.text)
        if parsed is not None:
            return parsed, result
        # Schema mismatch — append a correction message and retry.
        if attempt < MAX_PHASED_RETRIES:
            messages = messages + [
                {"role": "assistant", "content": result.text[:2000]},
                {"role": "user", "content": (
                    "Your previous response did not match the PhasedPlan schema. "
                    "It must be a JSON object with a top-level `phases` array. "
                    "Each phase has a `name` (one of MAP, DESIGN, IMPLEMENT, VERIFY) "
                    "and a `steps` array. Each step has `description`, `intent` "
                    "(one of investigate, design, mutate, verify, other), and "
                    "an `expects` object. DO NOT emit a flat `[{id, description}]` "
                    "list. Try again, JSON only, no prose."
                )},
            ]
    # Exhausted retries — caller (activator) must fail the contract.
    return None, last_result  # type: ignore[return-value]


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
                # suggested_args may be null (e.g. design steps without an
                # associated tool) — Pydantic accepts None but we coerce here
                # so downstream code that calls .get() on it doesn't break.
                if s.get("suggested_args") is None:
                    s["suggested_args"] = {}
                # R19.9 lenient path: auto-fill suggested_tool by intent
                # heuristic for non-design steps. The strict path triggers a
                # retry-on-correction loop in call_stipulator_phased; this
                # path keeps legacy/test fixtures and partially-formed
                # plans parseable without losing intent coverage.
                if s.get("intent") != "design" and s.get("suggested_tool") in (None, ""):
                    _default_by_intent = {
                        "investigate": "read",
                        "mutate": "str_replace",
                        "verify": "bash",
                        "other": "bash",
                    }
                    s["suggested_tool"] = _default_by_intent.get(s.get("intent"), "bash")
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


def call_plan_patcher(
    llm: LLM,
    obligation: str,
    plan_tree_outline: str,
    attention_id: Optional[str],
    state_class_summary: str,
    recent_trace: str,
    surprise_dims: dict[str, Any],
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[list[dict[str, Any]]], LLMResult]:
    """PlanPatcher — local tree-edit ops in response to a stall.

    Distinct from Stipulator (which is strategic, runs once at start).
    PlanPatcher revises a region of the existing plan tree based on observed
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
        {"role": "system", "content": PLAN_PATCHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="plan_patcher",
        temperature=0.1,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return None, result
    ops = _parse_inloop_ops(result.text)
    return ops, result


def _parse_inloop_ops(text: str) -> Optional[list[dict[str, Any]]]:
    """Validate PlanPatcher ops via the Pydantic PlanPatcherOps schema (§6.4).

    Returns a clean list of op-dicts (each with `op` discriminator, required
    fields, and `reflection`) or None on validation failure. The Pydantic
    discriminated union handles per-op required-field enforcement; before
    this, ~30 lines of manual key checks did the same thing more loosely.
    """
    from cat.plan_schema import PlanPatcherOps
    from pydantic import ValidationError
    try:
        parsed = PlanPatcherOps.model_validate_json(text)
    except ValidationError:
        return None
    if not parsed.ops:
        return None
    # Re-serialize back to plain dicts so the activator's existing
    # tree-edit applier keeps its current interface.
    return [op.model_dump() for op in parsed.ops]

PLANNER_SYSTEM_PROMPT = STIPULATOR_SYSTEM_PROMPT
JUDGE_SYSTEM_PROMPT = ATTESTOR_SYSTEM_PROMPT

# Backward-compat: previous name for the PlanPatcher role; do not remove until v1.8+
call_stipulator_inloop = call_plan_patcher


# ──────────────────────────────────────────────────────────────────────────
# v3 — hypothesis-based engine (see design.v3.md)
# ──────────────────────────────────────────────────────────────────────────

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

Output ONLY a JSON object:
{
  "location": "<the specific target — as precise as possible>",
  "change":   "<what to change there — describe the intent, not the bytes>",
  "predicts": "<what will be verifiably true after the change>",
  "basis":    "<one sentence: why this will work>"
}
"""

PERFORMER_V3_SYSTEM_PROMPT = """\
You are the PERFORMER. You execute a specific hypothesis using a plan.

On your FIRST response: output a brief plan as plain text showing your steps,
then immediately call your first tool. Format:
  PLAN: [inspect target] → [apply change] → [verify oracle]
  (then call the first tool)

On subsequent responses: call the next tool based on prior results.

Rules:
1. EVERY response must call a tool. Never reply with text alone.
2. You have MAX 5 tool calls total — use them efficiently:
   - Step 1: inspect the target (one call only).
   - Step 2: apply the change.
   - Step 3: run oracle to verify.
   - Steps 4-5: fix or confirm.
3. After ONE inspection, act. Do not keep re-inspecting.
4. If the target does not exist: call give_up(reason) immediately.
5. If the change succeeds and oracle passes: call done(reason).
6. Stay within the hypothesis scope.

Signals:
  done(reason)     — change applied, oracle passed
  give_up(reason)  — target wrong or unresolvable, state what you found
"""

PERFORMER_PLAN_PROMPT = """\
You are the PERFORMER. Before taking any action, create a plan.

Given the hypothesis below, output a PerformerPlanSchema — an ordered list
of steps from your current position to a passing oracle verification.

Rules for the plan:
1. Be concrete but domain-agnostic. Describe WHAT to do, not which specific tool.
2. First step: confirm the target exists and understand its current state.
3. Middle steps: apply the changes the hypothesis describes.
4. Last step: ALWAYS "verify with oracle / run criterion check".
5. If the hypothesis already tells you exactly what to do and you are confident
   the target exists: the plan can be as short as 2 steps (apply + verify).
6. Set confidence based on how certain you are this path leads to oracle pass.

Output ONLY a JSON object:
{
  "current_state": "<what is known or done so far — 'Not started' if first attempt>",
  "steps": [
    {"action": "<what to do>", "rationale": "<why needed>", "status": "pending"},
    ...
    {"action": "verify with oracle", "rationale": "confirm the change satisfies the criterion", "status": "pending"}
  ],
  "confidence": "high" | "medium" | "low"
}
"""

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


def call_performer_plan(
    llm: "LLM",
    task: "Any",
    prior_results: str = "",
    parent_event_id: Optional[str] = None,
) -> "tuple[Optional[Any], LLMResult]":
    """Generate or revise the Performer's internal execution plan.

    Called ONCE at the start of the inner loop (prior_results="") and
    optionally again after a step fails (prior_results=accumulated outputs).
    Returns (PerformerPlanSchema | None, LLMResult).
    """
    from cat.schemas import PerformerPlanSchema
    from pydantic import ValidationError

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

    messages = [
        {"role": "system", "content": PERFORMER_PLAN_PROMPT},
        {"role": "user", "content": task_content},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="performer",
        temperature=0.1,
        parent_event_id=parent_event_id,
    )
    if not result.text:
        return None, result
    try:
        data = json.loads(result.text)
        plan = PerformerPlanSchema(**data)
        return plan, result
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None, result


def call_stipulator_v3(
    llm: "LLM",
    obligation: str,
    representations: "Optional[list[Any]]" = None,
    knowledge: "Optional[Any]" = None,
    prior_attempts: "Optional[list[dict]]" = None,
    parent_event_id: Optional[str] = None,
) -> "tuple[Optional[Any], LLMResult]":
    """v3 Stipulator: returns (HypothesisSchema | None, LLMResult).

    Generates a single, specific hypothesis. Passes knowledge from prior
    attempts so the Stipulator can avoid re-investigating known facts.
    """
    from cat.schemas import HypothesisSchema
    from pydantic import ValidationError

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

    prior_section = ""
    if prior_attempts:
        prior_section = "\n\nPRIOR HYPOTHESES THAT FAILED (do NOT repeat):\n"
        for i, pa in enumerate(prior_attempts[-3:], 1):
            h = pa.get("hypothesis")
            reason = pa.get("reason", "")[:200]
            if h:
                loc = getattr(h, "location", str(h))[:80]
                prior_section += f"  {i}. Location: {loc}\n     Why failed: {reason}\n"

    user_content = (
        f"OBLIGATION:\n{obligation}"
        f"{env_section}{knowledge_section}{prior_section}"
    )
    messages = [
        {"role": "system", "content": STIPULATOR_V3_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
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
    try:
        data = json.loads(result.text)
        hypothesis = HypothesisSchema(**data)
        return hypothesis, result
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None, result


def call_attestor_v3(
    llm: "LLM",
    performer_result: "Any",
    knowledge: "Optional[Any]" = None,
    prior_attempts: "Optional[list[dict]]" = None,
    parent_event_id: Optional[str] = None,
) -> "tuple[Any, LLMResult]":
    """v3 Attestor: returns (AttestorV3Schema, LLMResult).

    Called only when oracle fails. Decides: new_hypothesis or give_up.
    """
    from cat.schemas import AttestorV3Schema
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
        from cat.schemas import AttestorV3Schema
        return AttestorV3Schema(verdict="new_hypothesis", reason="attestor returned no text"), result
    try:
        data = json.loads(result.text)
        v = AttestorV3Schema(**data)
        return v, result
    except (json.JSONDecodeError, ValidationError, TypeError):
        from cat.schemas import AttestorV3Schema
        return AttestorV3Schema(verdict="new_hypothesis", reason="attestor parse failed"), result
