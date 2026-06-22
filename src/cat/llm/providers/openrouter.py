"""OpenRouter provider — OpenAI-compatible structured output.

Uses response_format=json_schema strict mode for structured output,
response_format=json_object as fallback. Native tools= for tool-calling.
"""
from __future__ import annotations

from typing import Any

from cat.llm.providers.base import ProviderABC, ProviderResult
from cat.utils.serialization import extract_json_object, estimate_usd


class OpenRouterProvider(ProviderABC):
    """OpenRouter / DeepSeek / Groq / Together — OpenAI API compatible."""

    def schema(
        self,
        messages: list[dict],
        schema: Any,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        # Try json_schema strict mode first
        json_schema = schema.model_json_schema()
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": True},
        }
        result = self.llm.chat(
            messages=messages,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        parsed = extract_json_object(result.text or "")
        if parsed is not None:
            return self._to_result(result, parsed=parsed)

        # Fallback: json_object
        result = self.llm.chat(
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        parsed = extract_json_object(result.text or "")
        return self._to_result(result, parsed=parsed)

    def text(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        result = self.llm.chat(
            messages=messages,
            tools=tools,
            tool_choice=kwargs.get("tool_choice", "auto"),
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        return self._to_result(result, tool_calls=result.tool_calls or [])

    def toolcall(
        self,
        messages: list[dict],
        schema: Any,
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        # OpenRouter toolcall: use json_object, inject tool context
        from cat.utils.serialization import build_tool_context, inject_tool_context
        tools_block = build_tool_context(tools)
        msgs = inject_tool_context(messages, tools_block)
        result = self.llm.chat(
            messages=msgs,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        parsed = extract_json_object(result.text or "")
        return self._to_result(result, parsed=parsed)

    def _to_result(self, llm_result, *, parsed=None, tool_calls=None) -> ProviderResult:
        return ProviderResult(
            text=llm_result.text or "",
            parsed=parsed,
            tool_calls=tool_calls or llm_result.tool_calls or [],
            finish_reason=llm_result.finish_reason or "",
            tokens_in=llm_result.tokens_in,
            tokens_out=llm_result.tokens_out,
            tokens_cached=getattr(llm_result, "tokens_cached", 0),
            usd=llm_result.usd,
            model=llm_result.model,
        )
