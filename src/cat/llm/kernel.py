"""LLMKernel — the single entry point for all LLM calls.

A role *owns* a kernel:  stipulator.kernel = LLMKernel(...).

The kernel is the state object — it owns:
  - model selection (intelligence dial → model array)
  - provider routing (resolves model_id → provider)
  - self-healing (reask on bad output, escalate on exhaustion)
  - tracing (emits llm_input / llm_output)
  - per-call overrides (intelligence, temperature, max_tokens)

Three methods, no standalone functions:
  generate_schema(messages, *, intelligence=None, temperature=None) → KernelResult
  generate_text(messages, tools, *, intelligence=None, temperature=None) → KernelResult
  generate_toolcall(messages, schema, tools, *, intelligence=None, temperature=None) → KernelResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from cat.llm.client import LLM, LLMResult
from cat.llm.providers import get_provider
from cat.llm.providers.base import ProviderResult


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


@dataclass
class KernelResult:
    """Outcome of one LLMKernel call. Either ok with a value/text, or failed."""
    ok: bool
    value: Any = None                 # schema mode: validated Pydantic instance
    text: str = ""                    # text mode: assistant text
    tool_calls: list = field(default_factory=list)  # text mode
    parsed: Any = None                # toolcall mode: extracted JSON dict
    model: str = ""
    intelligence: float = 0.0
    attempts: int = 0                 # reask + escalation rounds taken
    raw: Optional[LLMResult] = None
    failure: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


class LLMKernel:
    """The brain. Stateful object — owns model selection, routing, self-heal, trace."""

    def __init__(
        self,
        models: list[str],
        *,
        mode: str = "schema",
        schema: Optional[type[BaseModel]] = None,
        temperature: float = 0.1,
        intelligence: float = 0.0,
        base_max_tokens: int = 1500,
        max_tokens_cap: int = 8192,
        reasoning_effort: str = "low",
        reask_max: int = 2,
        role_tag: str = "kernel",
        recorder: Any = None,
        llm_factory: Any = None,
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
        self._llm_factory = llm_factory or (lambda m: LLM(model=m, recorder=recorder))
        self._llm_cache: dict[str, LLM] = {}
        self.usage_records: list[dict] = []

    # ── state ──────────────────────────────────────────────────────────

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
        n = len(self.models)
        return 1.0 if n <= 1 else 1.0 / (n - 1)

    def _get_llm(self, model_id: str) -> LLM:
        if model_id not in self._llm_cache:
            self._llm_cache[model_id] = self._llm_factory(model_id)
        return self._llm_cache[model_id]

    def _get_provider(self, model_id: str):
        llm = self._get_llm(model_id)
        return get_provider(model_id, llm=llm, recorder=self.recorder)

    # ── tracing ────────────────────────────────────────────────────────

    def _trace_input(self, model: str, mode: str, schema_name: str,
                     intelligence: float, temperature: float, messages_count: int,
                     tools_count: int = 0) -> None:
        if self.recorder is None:
            return
        from cat.trace import TraceEvent
        from cat.utils.time import utcnow_iso
        import uuid
        self.recorder.append(TraceEvent(
            event_id=f"llm-in-{uuid.uuid4().hex[:8]}",
            parent_id=None, ts=utcnow_iso(), kind="llm_input",
            payload={
                "model": model, "mode": mode, "schema": schema_name,
                "intelligence": round(intelligence, 3),
                "temperature": temperature,
                "messages_count": messages_count,
                "tools_count": tools_count,
                "role": self.role_tag,
            },
        ))

    def _trace_output(self, model: str, mode: str, ok: bool,
                      finish_reason: str, tokens_in: int, tokens_out: int,
                      usd: float, attempts: int, has_tool_calls: bool = False,
                      tool_names: list | None = None) -> None:
        if self.recorder is None:
            return
        from cat.trace import TraceEvent
        from cat.utils.time import utcnow_iso
        import uuid
        self.recorder.append(TraceEvent(
            event_id=f"llm-out-{uuid.uuid4().hex[:8]}",
            parent_id=None, ts=utcnow_iso(), kind="llm_output",
            payload={
                "model": model, "mode": mode, "ok": ok,
                "finish_reason": finish_reason,
                "tokens_in": tokens_in, "tokens_out": tokens_out, "usd": usd,
                "attempts": attempts,
                "has_tool_calls": has_tool_calls,
                "tool_names": tool_names or [],
                "role": self.role_tag,
            },
        ))

    # ── usage recording ────────────────────────────────────────────────

    def _record_usage(self, model: str, ok: bool, ar: ProviderResult, mode: str) -> None:
        from cat.llm.card import card_for
        c = card_for(model)
        self.usage_records.append({
            "model": model, "producer": c.producer, "family": c.family,
            "variant": c.variant, "mode": mode, "role": self.role_tag,
            "intelligence": round(self._intel, 3), "ok": ok,
            "tokens_in": ar.tokens_in, "tokens_out": ar.tokens_out,
            "tokens_cached": ar.tokens_cached,
            "usd": ar.usd, "finish_reason": ar.finish_reason,
        })

    # ── three entry points ─────────────────────────────────────────────

    def generate_schema(
        self,
        messages: list[dict],
        *,
        schema: Optional[type[BaseModel]] = None,
        intelligence: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        parent_event_id: Optional[str] = None,
    ) -> KernelResult:
        """Structured JSON output — Pydantic-validated.

        Self-heals: reask on bad JSON (×2), escalate to next model if exhausted.
        """
        active_schema = schema if schema is not None else self.schema
        if active_schema is None:
            raise ValueError("generate_schema requires a Pydantic schema")
        return self._run_loop(
            messages, mode="schema",
            schema=active_schema,
            intelligence=intelligence, temperature=temperature,
            max_tokens=max_tokens or self.base_max_tokens,
            parent_event_id=parent_event_id,
        )

    def generate_text(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        intelligence: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        parent_event_id: Optional[str] = None,
    ) -> KernelResult:
        """Freeform text + native tool calls."""
        return self._run_loop(
            messages, mode="text",
            tools=tools,
            intelligence=intelligence, temperature=temperature,
            max_tokens=max_tokens or self.base_max_tokens,
            parent_event_id=parent_event_id,
        )

    def generate_toolcall(
        self,
        messages: list[dict],
        schema: Any,
        tools: list[dict],
        *,
        intelligence: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        parent_event_id: Optional[str] = None,
    ) -> KernelResult:
        """Structured PerformerStep — intent + tools + expectations."""
        return self._run_loop(
            messages, mode="toolcall",
            schema=schema, tools=tools,
            intelligence=intelligence, temperature=temperature,
            max_tokens=max_tokens or self.base_max_tokens,
            parent_event_id=parent_event_id,
        )

    # ── the self-heal loop ─────────────────────────────────────────────

    def _run_loop(
        self,
        messages: list[dict],
        *,
        mode: str,
        schema: Optional[type[BaseModel]] = None,
        tools: Optional[list[dict]] = None,
        intelligence: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 1500,
        parent_event_id: Optional[str] = None,
    ) -> KernelResult:
        # Apply per-call overrides
        if intelligence is not None:
            self._intel = _clamp01(intelligence)
        effective_temp = temperature if temperature is not None else self.temperature

        work = list(messages)
        max_tok = max_tokens
        reask = 0
        attempts = 0
        escalations = 0
        tok_in = tok_out = 0
        usd = 0.0
        max_escalations = len(self.models)
        schema_name = schema.__name__ if schema else "none"

        while True:
            attempts += 1
            model = self.current_model()
            provider = self._get_provider(model)

            self._trace_input(model, mode, schema_name, self._intel, effective_temp, len(work))

            try:
                if mode == "toolcall":
                    ar = provider.toolcall(
                        work, schema=schema, tools=tools or [],
                        max_tokens=max_tok, temperature=effective_temp,
                        role_tag=self.role_tag,
                        parent_event_id=parent_event_id,
                    )
                elif mode == "schema":
                    ar = provider.schema(
                        work, schema=schema,
                        max_tokens=max_tok, temperature=effective_temp,
                        role_tag=self.role_tag,
                        parent_event_id=parent_event_id,
                    )
                else:  # mode == "text"
                    ar = provider.text(
                        work, tools=tools or [],
                        max_tokens=max_tok, temperature=effective_temp,
                        role_tag=self.role_tag,
                        parent_event_id=parent_event_id,
                    )
            except Exception as exc:
                if self._intel < 1.0 and escalations < max_escalations:
                    self.increase_intelligence(self._step())
                    escalations += 1
                    work, max_tok, reask = list(messages), max_tokens, 0
                    continue
                return KernelResult(
                    ok=False, model=model, intelligence=self._intel,
                    attempts=attempts, failure=f"api_error: {exc}",
                    tokens_in=tok_in, tokens_out=tok_out, usd=usd,
                )

            tok_in += ar.tokens_in
            tok_out += ar.tokens_out
            usd += ar.usd

            # Truncation → bump output budget (×4), retry same model.
            if (ar.finish_reason == "length") and (max_tok < self.max_tokens_cap):
                max_tok = min(max_tok * 4, self.max_tokens_cap)
                continue

            # text mode — return immediately
            if mode == "text":
                has_tc = bool(ar.tool_calls)
                tool_names = [tc.get("name", "?") for tc in ar.tool_calls] if has_tc else []
                self._trace_output(model, mode, True, ar.finish_reason,
                                   ar.tokens_in, ar.tokens_out, ar.usd, attempts,
                                   has_tool_calls=has_tc, tool_names=tool_names)
                self._record_usage(model, True, ar, mode)
                return KernelResult(
                    ok=True, text=ar.text, tool_calls=ar.tool_calls,
                    model=model, intelligence=self._intel,
                    attempts=attempts, tokens_in=tok_in, tokens_out=tok_out, usd=usd,
                )

            # schema / toolcall — validate parsed dict with Pydantic
            if ar.parsed is not None:
                try:
                    value = schema(**ar.parsed)
                    self._trace_output(model, mode, True, ar.finish_reason,
                                       ar.tokens_in, ar.tokens_out, ar.usd, attempts)
                    self._record_usage(model, True, ar, mode)
                    return KernelResult(
                        ok=True, value=value, parsed=ar.parsed,
                        model=model, intelligence=self._intel,
                        attempts=attempts, tokens_in=tok_in, tokens_out=tok_out, usd=usd,
                    )
                except (ValidationError, TypeError) as e:
                    err = str(e)[:300]
            else:
                err = "output was not a valid JSON object for the schema"

            # reask with the error (Instructor pattern) at the same model
            if reask < self.reask_max:
                reask += 1
                work = work + [
                    {"role": "assistant", "content": ar.text or str(ar.parsed)},
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
                work, max_tok, reask = list(messages), max_tokens, 0
                continue

            self._trace_output(model, mode, False, ar.finish_reason,
                               ar.tokens_in, ar.tokens_out, ar.usd, attempts)
            self._record_usage(model, False, ar, mode)
            return KernelResult(
                ok=False, model=model, intelligence=self._intel,
                attempts=attempts, failure=err,
                tokens_in=tok_in, tokens_out=tok_out, usd=usd,
            )
