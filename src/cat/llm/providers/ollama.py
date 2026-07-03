"""Ollama provider — uses instructor for schema-guided structured output.

Ollama's /v1 endpoint does NOT support response_format. Instead:
  - instructor (Mode.JSON) for schema/toolcall: Pydantic-validated output with
    automatic reask on validation failure — critical for weak local models.
  - format=<json_schema_dict> as fallback if instructor is not available.
  - format="json" as last-resort fallback.

Ollama supports native tools= for models that support function calling
(e.g. qwen2.5-coder), but output format varies by model — so text()
uses native tools= with XML marker fallback.
"""
from __future__ import annotations

import json as _json
import re as _re
from typing import Any, Optional

from cat.llm.providers.base import ProviderABC, ProviderResult
from cat.utils.serialization import extract_json_object, build_tool_context, inject_tool_context

# instructor is optional at import time — graceful degradation
try:
    import instructor
    from instructor import Instructor, Mode
    _HAS_INSTRUCTOR = True
except ImportError:
    _HAS_INSTRUCTOR = False


def _build_instructor_client(llm: Any) -> Optional["Instructor"]:
    """Wrap the LLM's underlying OpenAI client with instructor.

    Returns None if instructor is not installed.
    """
    if not _HAS_INSTRUCTOR:
        return None
    base_client = getattr(llm, "client", None)
    if base_client is None:
        return None
    return instructor.from_openai(base_client, mode=Mode.JSON)


class OllamaProvider(ProviderABC):
    """Ollama — instructor-enhanced structured output for weak local models.

    For schema() and toolcall(), instructor provides:
      - Pydantic-validated response parsing
      - Automatic reask on validation failure (model gets a second chance
        with the error message — crucial for small models like 1.5B)
      - Schema injection into the system prompt

    Falls back to native Ollama format= if instructor is not available.
    """

    def schema(
        self,
        messages: list[dict],
        schema: Any,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        instructor_client = _build_instructor_client(self.llm)

        if instructor_client is not None:
            return self._schema_via_instructor(
                instructor_client, messages, schema, max_tokens, temperature, **kwargs
            )

        # Fallback: native Ollama GBNF grammar
        return self._schema_via_format(messages, schema, max_tokens, temperature, **kwargs)

    def toolcall(
        self,
        messages: list[dict],
        schema: Any,
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        instructor_client = _build_instructor_client(self.llm)

        if instructor_client is not None:
            return self._toolcall_via_instructor(
                instructor_client, messages, schema, tools, max_tokens, temperature, **kwargs
            )

        # Fallback: native Ollama format="json" with injected tool context
        return self._toolcall_via_format(messages, schema, tools, max_tokens, temperature, **kwargs)

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
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        tc = result.tool_calls or []
        # Ollama may return tool_calls as XML text for some models
        if not tc and result.text and "<tool_call>" in result.text:
            tc = self._parse_ollama_tool_markers(result.text)
        return self._to_result(result, tool_calls=tc)

    # ── instructor paths ───────────────────────────────────────────────

    def _schema_via_instructor(
        self,
        instructor_client: "Instructor",
        messages: list[dict],
        schema: Any,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Use instructor for Pydantic-validated schema output.

        Instructor handles:
        - Schema injection into the prompt
        - Response parsing into the Pydantic model
        - Automatic reask on validation failure (up to 2 retries)
        """
        from openai.types.chat import ChatCompletionUserMessageParam

        # Build a user message that instructs the model to produce the schema
        # Instructor works best when the last user message asks for the structured output
        last_msg = messages[-1] if messages else {"role": "user", "content": ""}
        msgs = list(messages)

        try:
            response = instructor_client.chat.completions.create(
                model=self.llm.api_model,
                response_model=schema,
                messages=msgs,
                max_tokens=max_tokens,
                temperature=temperature,
                max_retries=2,  # instructor internal reask on validation failure
            )
            # response is a validated Pydantic instance
            parsed = response.model_dump(mode="json")
            return ProviderResult(
                text=_json.dumps(parsed),
                parsed=parsed,
                tool_calls=[],
                finish_reason="stop",
                tokens_in=0,   # instructor doesn't expose usage directly
                tokens_out=0,
                usd=0.0,
                model=self.llm.model,
            )
        except Exception:
            # Instructor failed — fall through to native format
            return self._schema_via_format(
                messages, schema, max_tokens, temperature, **kwargs
            )

    def _toolcall_via_instructor(
        self,
        instructor_client: "Instructor",
        messages: list[dict],
        schema: Any,
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Use instructor for Pydantic-validated toolcall output.

        Injects tool context into the messages so the model knows what tools
        are available, then uses instructor to validate the structured response.
        """
        tools_block = build_tool_context(tools)
        msgs = inject_tool_context(messages, tools_block)

        try:
            response = instructor_client.chat.completions.create(
                model=self.llm.api_model,
                response_model=schema,
                messages=msgs,
                max_tokens=max_tokens,
                temperature=temperature,
                max_retries=2,
            )
            parsed = response.model_dump(mode="json")
            return ProviderResult(
                text=_json.dumps(parsed),
                parsed=parsed,
                tool_calls=[],
                finish_reason="stop",
                tokens_in=0,
                tokens_out=0,
                usd=0.0,
                model=self.llm.model,
            )
        except Exception:
            return self._toolcall_via_format(
                messages, schema, tools, max_tokens, temperature, **kwargs
            )

    # ── native Ollama fallback paths ───────────────────────────────────

    def _schema_via_format(
        self,
        messages: list[dict],
        schema: Any,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Native Ollama: GBNF grammar-based structured output."""
        schema_dict = schema.model_json_schema()
        # First attempt: grammar-based structured output
        result = self.llm.chat(
            messages=messages,
            extra_body={"format": schema_dict},
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        parsed = extract_json_object(result.text or "")
        if parsed is not None:
            return self._to_result(result, parsed=parsed)

        # Fallback: basic JSON mode
        result = self.llm.chat(
            messages=messages,
            extra_body={"format": "json"},
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        parsed = extract_json_object(result.text or "")
        return self._to_result(result, parsed=parsed)

    def _toolcall_via_format(
        self,
        messages: list[dict],
        schema: Any,
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Native Ollama: format="json" with injected tool context."""
        tools_block = build_tool_context(tools)
        msgs = inject_tool_context(messages, tools_block)
        result = self.llm.chat(
            messages=msgs,
            extra_body={"format": "json"},
            max_tokens=max_tokens,
            temperature=temperature,
            role_tag=kwargs.get("role_tag", "provider"),
            parent_event_id=kwargs.get("parent_event_id"),
        )
        parsed = extract_json_object(result.text or "")
        return self._to_result(result, parsed=parsed)

    # ── helpers ─────────────────────────────────────────────────────────

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

    @staticmethod
    def _parse_ollama_tool_markers(text: str) -> list[dict]:
        """Parse Ollama's <tool_call> XML markers as fallback."""
        out = []
        pattern = _re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", _re.DOTALL)
        for i, m in enumerate(pattern.finditer(text)):
            try:
                data = _json.loads(m.group(1))
                out.append({
                    "id": f"ollama-{i}",
                    "name": data.get("name", "?"),
                    "arguments": data.get("arguments", data),
                })
            except _json.JSONDecodeError:
                pass
        return out
