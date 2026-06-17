"""LLMKernel — the "brain/CPU" that powers a role (Stipulator, Performer, …).

A role *owns* a kernel:  stipulator.llmkernel = LLMKernel(...).
The kernel is intelligence-addressed: the caller dials a normalized 0..1
`intelligence` level, and the kernel resolves it to a concrete model from a
configurable array (weakest → strongest). The loop never names a model.

Design (see design.v3.md "LLMKernel"):

  intelligence (0..1)  → model = models[round(intelligence × (len-1))]
  set_intelligence(x)  → clamp to [0,1]   (set_intelligence(10) → 1.0)
  increase_intelligence(d) → clamp(intel + d)  (increase(+2) → 1.0)

  mode = "schema" → Pydantic-validated structured output
  mode = "text"   → freeform / tool-calling

  run(messages) self-heals (battle-tested two-branch policy):
    finish_reason == "length"     → ×4 max_tokens, retry        (Anthropic pattern)
    invalid JSON / ValidationError→ reask with the error        (Instructor pattern)
    reask exhausted at this model → increase_intelligence(1 step), reset
  → returns KernelResult (valid-or-explicit-failure; never a silently-bad object).

Light: built on the existing cat.llm.LLM (OpenAI SDK + OpenRouter). No LiteLLM /
Instructor dependency — we own the ~self-heal loop. A future direct-provider
backend can slot in behind `client_factory` without touching callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError

from cat.llm import LLMResult


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


@dataclass
class KernelResult:
    """Outcome of one LLMKernel.run(). Either ok with a value/text, or failed."""
    ok: bool
    value: Any = None                 # schema mode: validated Pydantic instance
    text: str = ""                    # text mode: assistant text
    tool_calls: list = field(default_factory=list)  # text mode
    model: str = ""
    intelligence: float = 0.0
    attempts: int = 0                 # reask + escalation rounds taken
    raw: Optional[LLMResult] = None
    failure: str = ""
    # aggregate usage across ALL internal calls (reask/escalation) — for charging
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


class LLMKernel:
    def __init__(
        self,
        models: list[str],
        *,
        mode: str = "schema",                 # "schema" | "text" | "toolcall"
        schema: Optional[type[BaseModel]] = None,
        temperature: float = 0.1,
        intelligence: float = 0.0,
        base_max_tokens: int = 1500,
        max_tokens_cap: int = 8192,
        reasoning_effort: str = "low",
        reask_max: int = 2,
        role_tag: str = "kernel",
        recorder: Any = None,
        router: Any = None,
        client_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        if not models:
            raise ValueError("LLMKernel needs a non-empty model array")
        if mode == "schema" and schema is None:
            raise ValueError("schema mode requires a Pydantic schema")
        self.models = list(models)
        self.mode = mode
        self.schema = schema
        self.temperature = temperature
        self._intel = _clamp01(intelligence)
        self.base_max_tokens = base_max_tokens
        self.max_tokens_cap = max_tokens_cap
        self.reasoning_effort = reasoning_effort
        self.reask_max = reask_max
        self.role_tag = role_tag
        self.recorder = recorder
        # The router is the wire; adapters (resolved per model) shape/parse calls.
        from cat.llm_router import LLMRouter
        self.router = router or LLMRouter(recorder=recorder, client_factory=client_factory)
        # per-call analytics rows: (model, producer, family, mode, intelligence, tokens, ok)
        self.usage_records: list[dict] = []

    # ── intelligence dial ────────────────────────────────────────────────
    @property
    def intelligence(self) -> float:
        return self._intel

    def set_intelligence(self, x: float) -> "LLMKernel":
        self._intel = _clamp01(x)
        return self

    def increase_intelligence(self, delta: float) -> "LLMKernel":
        self._intel = _clamp01(self._intel + delta)
        return self

    def _model_index(self) -> int:
        n = len(self.models)
        if n == 1:
            return 0
        return round(self._intel * (n - 1))

    def current_model(self) -> str:
        return self.models[self._model_index()]

    def _step(self) -> float:
        """One model-position as a normalized increment."""
        n = len(self.models)
        return 1.0 if n <= 1 else 1.0 / (n - 1)

    def _record_usage(self, model: str, ok: bool, ar: Any) -> None:
        from cat.llm_card import card_for
        c = card_for(model)
        self.usage_records.append({
            "model": model, "producer": c.producer, "family": c.family,
            "variant": c.variant, "mode": self.mode, "role": self.role_tag,
            "intelligence": round(self._intel, 3), "ok": ok,
            "tokens_in": ar.tokens_in, "tokens_out": ar.tokens_out,
            "usd": ar.usd, "finish_reason": ar.finish_reason,
        })

    # ── the call + self-heal loop (model-agnostic; per-model HOW is in adapter) ──
    def run(
        self,
        messages: list[dict],
        *,
        schema: Optional[type[BaseModel]] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
        parent_event_id: Optional[str] = None,
    ) -> KernelResult:
        from cat.llm_adapters import adapter_for
        active_schema = schema if schema is not None else self.schema
        work = list(messages)
        max_tok = self.base_max_tokens
        reask = 0
        attempts = 0
        escalations = 0
        tok_in = tok_out = 0
        usd = 0.0
        max_escalations = len(self.models)

        def _acc(ar) -> None:
            nonlocal tok_in, tok_out, usd
            tok_in += ar.tokens_in
            tok_out += ar.tokens_out
            usd += ar.usd

        while True:
            attempts += 1
            model = self.current_model()
            adapter = adapter_for(model)
            try:
                if self.mode == "toolcall":
                    # Structured PerformerStep: intent + tools + expectations
                    ar = adapter.generate_toolcall(
                        self.router, model, work, schema=active_schema,
                        tools=tools or [],
                        max_tokens=max_tok, temperature=self.temperature,
                        parent_event_id=parent_event_id)
                elif self.mode == "schema":
                    ar = adapter.generate_schema(
                        self.router, model, work, schema=active_schema,
                        max_tokens=max_tok, temperature=self.temperature,
                        reasoning={"effort": self.reasoning_effort},
                        parent_event_id=parent_event_id)
                else:  # mode == "text"
                    ar = adapter.generate_text(
                        self.router, model, work, tools=tools,
                        max_tokens=max_tok, temperature=self.temperature,
                        tool_choice=tool_choice, parent_event_id=parent_event_id)
                _acc(ar)
            except Exception as exc:  # noqa: BLE001 — API/transport failure
                if self._intel < 1.0 and escalations < max_escalations:
                    self.increase_intelligence(self._step())
                    escalations += 1
                    work, max_tok, reask = list(messages), self.base_max_tokens, 0
                    continue
                return KernelResult(ok=False, model=model, intelligence=self._intel,
                                    attempts=attempts, failure=f"api_error: {exc}",
                                    tokens_in=tok_in, tokens_out=tok_out, usd=usd)

            # Truncation → bump output budget (×4), retry same model.
            if (ar.finish_reason == "length") and (max_tok < self.max_tokens_cap):
                max_tok = min(max_tok * 4, self.max_tokens_cap)
                continue

            if self.mode == "text":
                self._record_usage(model, True, ar)
                return KernelResult(ok=True, text=ar.text, tool_calls=ar.tool_calls,
                                    model=model, intelligence=self._intel,
                                    attempts=attempts, tokens_in=tok_in,
                                    tokens_out=tok_out, usd=usd)

            # schema and toolcall modes — adapter extracted the dict; kernel validates (Pydantic).
            if ar.parsed is not None:
                try:
                    value = active_schema(**ar.parsed)
                    self._record_usage(model, True, ar)
                    return KernelResult(ok=True, value=value, model=model,
                                        intelligence=self._intel, attempts=attempts,
                                        tokens_in=tok_in, tokens_out=tok_out, usd=usd)
                except (ValidationError, TypeError) as e:
                    err = str(e)[:300]
            else:
                err = "output was not a valid JSON object for the schema"

            # reask with the error (Instructor pattern) at the same model
            if reask < self.reask_max:
                reask += 1
                work = work + [
                    {"role": "assistant", "content": ar.text},
                    {"role": "user", "content": (
                        f"Your previous output failed: {err}. "
                        f"Return ONLY a single valid JSON object matching the required schema."
                    )},
                ]
                continue

            # reask exhausted → escalate intelligence (next model), reset
            if self._intel < 1.0 and escalations < max_escalations:
                self.increase_intelligence(self._step())
                escalations += 1
                work, max_tok, reask = list(messages), self.base_max_tokens, 0
                continue

            self._record_usage(model, False, ar)
            return KernelResult(ok=False, model=model, intelligence=self._intel,
                                attempts=attempts, failure=err,
                                tokens_in=tok_in, tokens_out=tok_out, usd=usd)
