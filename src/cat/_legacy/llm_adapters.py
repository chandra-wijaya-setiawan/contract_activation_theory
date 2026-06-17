"""LLMModelAdapter — per-model behavior behind a common interface.

Interface (the two task verbs):
    generate_schema(router, model_id, messages, schema=…) -> AdapterResult
    generate_text(router, model_id, messages, tools=…)    -> AdapterResult

Each adapter does ONE model-specific call: shape the request → dispatch via the
router → parse the response. The self-heal LOOP (reask, ×4 tokens, escalate) and
Pydantic validation live in LLMKernel (model-agnostic) — adapters never loop.

Inheritance is shallow and version-pinned:
    LLMModelAdapter              # universal fallback (unknown models)
    └─ LLMModelAdapterKimiBase   # Kimi-family shared behavior
       └─ LLMModelAdapterKimi26  # k2.6 version params

Most models need NO subclass — they use the base. Subclass only when a model's
request-shaping or output-parsing genuinely differs (e.g. Kimi's reasoning-off +
JSON prefill + <|tool_call|> markers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from cat.llm import extract_json_object


@dataclass
class AdapterResult:
    text: str = ""
    parsed: Optional[dict] = None          # schema mode: extracted JSON dict (or None)
    tool_calls: list = field(default_factory=list)
    finish_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    model: str = ""

    @classmethod
    def from_llm(cls, resp, *, parsed=None, tool_calls=None) -> "AdapterResult":
        return cls(
            text=resp.text or "", parsed=parsed, tool_calls=tool_calls or [],
            finish_reason=resp.finish_reason or "",
            tokens_in=int(getattr(resp, "tokens_in", 0) or 0),
            tokens_out=int(getattr(resp, "tokens_out", 0) or 0),
            usd=float(getattr(resp, "usd", 0.0) or 0.0),
            model=getattr(resp, "model", ""),
        )


class LLMModelAdapter:
    """Universal base / fallback — standard OpenAI-compatible behavior."""

    def generate_schema(self, router, model_id, messages, *, schema,
                        max_tokens, temperature, reasoning=None,
                        parent_event_id=None) -> AdapterResult:
        resp = router.dispatch(
            model_id, messages=messages, response_format={"type": "json_object"},
            max_tokens=max_tokens, temperature=temperature, reasoning=reasoning,
            role_tag="stipulator", parent_event_id=parent_event_id,
        )
        return AdapterResult.from_llm(resp, parsed=extract_json_object(resp.text or ""))

    def generate_text(self, router, model_id, messages, *, tools,
                      max_tokens, temperature, tool_choice="auto",
                      parent_event_id=None) -> AdapterResult:
        resp = router.dispatch(
            model_id, messages=messages, tools=tools, tool_choice=tool_choice,
            max_tokens=max_tokens, temperature=temperature,
            role_tag="performer", parent_event_id=parent_event_id,
        )
        return AdapterResult.from_llm(resp, tool_calls=resp.tool_calls or [])

    def generate_toolcall(self, router, model_id, messages, *, schema,
                          tools, max_tokens, temperature,
                          parent_event_id=None) -> AdapterResult:
        """Generate a structured PerformerStep with intent + tools + expectations.

        Uses json_object response_format so the model outputs structured JSON
        text (not native tool_calls). The adapter extracts and validates the
        PerformerStep schema, then converts tools to the internal tool_calls
        format for the kernel.

        Args:
            tools: Full list of available tool schemas (for the system prompt
                   context — injected into the user message, not as API tools).
        """
        # Render each tool as a compact JSON example so the model copies argument
        # key names verbatim rather than paraphrasing them (e.g. "old_string" for
        # "old_str"). Models copy JSON keys exactly; they paraphrase prose labels.
        tool_desc_parts = []
        for t in tools:
            fn = t.get("function", {})
            name = fn.get("name", "?")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])
            # Build a compact JSON signature: "key": "..." for each arg (skip reason)
            sig_parts = []
            hint_parts = []
            for arg_name, arg_schema in props.items():
                if arg_name == "reason":
                    continue
                req_star = "*" if arg_name in required else ""
                sig_parts.append(f'"{arg_name}": "..."')
                hint = arg_schema.get("description", "")
                hint_parts.append(f'  "{arg_name}"{req_star}: {hint}')
            sig = "{" + ", ".join(sig_parts) + "}" if sig_parts else "{}"
            hints = "\n".join(hint_parts)
            tool_desc_parts.append(f'{name} — {desc}\n  arguments: {sig}\n{hints}')

        tools_block = "\n\n".join(tool_desc_parts)

        tool_context = (
            f"Available tools (* = required argument):\n\n{tools_block}\n\n"
            "Classify each tool call by intent:\n"
            "- look:   gather information (read, search, list, inspect)\n"
            "- change: modify state (write, edit, create, delete)\n"
            "- check:  verify against criterion (run test, validate)\n"
            "- done:   task complete — no tools, provide reason\n"
            "- give_up: cannot proceed — no tools, provide reason\n\n"
            "Use the EXACT key names shown above in your tools[].arguments JSON."
        )
        # Prepend tool context to last user message
        msgs = list(messages)
        if msgs and msgs[-1].get("role") == "user":
            msgs[-1] = {
                "role": "user",
                "content": msgs[-1]["content"] + "\n\n" + tool_context,
            }
        resp = router.dispatch(
            model_id, messages=msgs,
            response_format={"type": "json_object"},
            max_tokens=max_tokens, temperature=temperature,
            role_tag="performer", parent_event_id=parent_event_id,
        )
        parsed = extract_json_object(resp.text or "")
        return AdapterResult.from_llm(resp, parsed=parsed)


class LLMModelAdapterKimiBase(LLMModelAdapter):
    """Kimi-family behavior (stable across versions).

    schema: disable reasoning (Moonshot's own recommendation for agentic/tool
            use) + assistant-prefill "{" so the model emits JSON-first and skips
            the long chain-of-thought that otherwise truncates the output.
    text:   parse Kimi's native <|tool_call|> token markers when the provider
            leaks them as raw text instead of structured tool_calls.
    """
    _MARKER = re.compile(
        r"functions\.([A-Za-z0-9_]+):\d+\s*"
        r"<\|tool_call_argument_begin\|>\s*(\{.*?\})\s*<\|tool_call_end\|>",
        re.DOTALL,
    )

    def generate_schema(self, router, model_id, messages, *, schema,
                        max_tokens, temperature, reasoning=None,
                        parent_event_id=None) -> AdapterResult:
        # Prefill "{" — do NOT combine with json_object response_format.
        msgs = list(messages) + [{"role": "assistant", "content": "{"}]
        resp = router.dispatch(
            model_id, messages=msgs, response_format=None,
            max_tokens=max_tokens, temperature=temperature,
            reasoning={"enabled": False},
            role_tag="stipulator", parent_event_id=parent_event_id,
        )
        # Try as-is, then with the prefill prepended (OpenRouter may not echo it).
        body = resp.text or ""
        parsed = extract_json_object(body)
        if parsed is None:
            parsed = extract_json_object("{" + body)
        return AdapterResult.from_llm(resp, parsed=parsed)

    def generate_text(self, router, model_id, messages, *, tools,
                      max_tokens, temperature, tool_choice="auto",
                      parent_event_id=None) -> AdapterResult:
        resp = router.dispatch(
            model_id, messages=messages, tools=tools, tool_choice=tool_choice,
            max_tokens=max_tokens, temperature=temperature,
            reasoning={"enabled": False},
            role_tag="performer", parent_event_id=parent_event_id,
        )
        tc = resp.tool_calls or []
        if not tc and "<|tool_call" in (resp.text or ""):
            tc = self._parse_markers(resp.text)
        return AdapterResult.from_llm(resp, tool_calls=tc)

    def _parse_markers(self, text: str) -> list:
        out = []
        for i, m in enumerate(self._MARKER.finditer(text or "")):
            name, args = m.group(1), m.group(2)
            out.append({"id": f"kimi-{i}", "name": name, "arguments": args})
        return out


class LLMModelAdapterKimi26(LLMModelAdapterKimiBase):
    """Kimi k2.6: temperature fixed at 1.0; wants a generous output budget."""
    _FLOOR = 16000

    def generate_schema(self, router, model_id, messages, *, schema,
                        max_tokens, temperature, reasoning=None,
                        parent_event_id=None) -> AdapterResult:
        return super().generate_schema(
            router, model_id, messages, schema=schema,
            max_tokens=max(max_tokens, self._FLOOR), temperature=1.0,
            reasoning=reasoning, parent_event_id=parent_event_id)

    def generate_text(self, router, model_id, messages, *, tools,
                      max_tokens, temperature, tool_choice="auto",
                      parent_event_id=None) -> AdapterResult:
        return super().generate_text(
            router, model_id, messages, tools=tools,
            max_tokens=max(max_tokens, self._FLOOR), temperature=1.0,
            tool_choice=tool_choice, parent_event_id=parent_event_id)


# ── registry: most-specific (version) → family → universal fallback ────────
_BASE = LLMModelAdapter()
_KIMI_BASE = LLMModelAdapterKimiBase()
_KIMI26 = LLMModelAdapterKimi26()


def adapter_for(model_id: str) -> LLMModelAdapter:
    mid = (model_id or "").lower()
    if "kimi-k2.6" in mid:
        return _KIMI26
    if "kimi" in mid:               # other Kimi versions → family base (untuned but correct)
        return _KIMI_BASE
    return _BASE                     # universal fallback — new models never break
