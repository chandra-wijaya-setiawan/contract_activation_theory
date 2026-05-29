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
PLANNER_SYSTEM_PROMPT = STIPULATOR_SYSTEM_PROMPT
JUDGE_SYSTEM_PROMPT = ATTESTOR_SYSTEM_PROMPT
