"""LLM client — OpenAI-compatible wrapper with token harvesting.

All LLM calls go through chat(); tool-calling, plain chat, JSON-mode chat
all funnel here for uniform token harvesting.

See design.v3.md §"Roles" for the three contract-vocabulary roles
(Performer, Stipulator, Attestor) and how they map to LLM calls.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import OpenAI

from cat.trace import Recorder, TraceEvent
from cat.utils.time import utcnow_iso
from cat.utils.serialization import extract_json_object, estimate_usd, _strip_provider


@dataclass
class LLMResult:
    """One LLM response with token accounting."""
    text: Optional[str]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    model: str = ""
    raw: dict = field(default_factory=dict)


def call_structured(
    llm: "LLM",
    messages: list,
    model_cls: Any,
    *,
    role_tag: str,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    parent_event_id: Optional[str] = None,
) -> tuple[Optional[Any], Any]:
    """Get a Pydantic-model-constrained response from the LLM.

    Strategy (best -> most compatible):
      1. Pydantic-derived json_schema strict mode
      2. Fallback to json_object
      3. extract_json_object() rescues JSON embedded in prose
      4. Pydantic validates the final dict

    Returns (model_instance | None, LLMResult).
    """
    from pydantic import ValidationError

    schema = model_cls.model_json_schema()
    json_schema_fmt = {
        "type": "json_schema",
        "json_schema": {"name": model_cls.__name__, "schema": schema, "strict": True},
    }

    result = None
    for fmt in (json_schema_fmt, {"type": "json_object"}):
        try:
            result = llm.chat(
                messages=messages, response_format=fmt,
                role_tag=role_tag, temperature=temperature,
                max_tokens=max_tokens, parent_event_id=parent_event_id,
            )
        except Exception:
            continue
        if not result or not result.text:
            continue
        data = extract_json_object(result.text)
        if data is None:
            continue
        try:
            return model_cls(**data), result
        except (ValidationError, TypeError):
            continue

    return None, result


class LLM:
    """Thin OpenAI-compatible wrapper. One instance per agent run.

    All calls go through chat(); tool-calling, plain chat, JSON-mode chat
    all funnel here for uniform token harvesting.

    R11: retries with exponential backoff on degenerate (0/0 token) responses
    and openai-client exceptions. Backoff: base_delay * 2^attempt.
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
        reasoning_effort: Optional[str] = None,
        reasoning: Optional[dict] = None,
        role_tag: str = "executor",
        parent_event_id: Optional[str] = None,
        max_retries: int = 3,
        retry_base_delay_s: float = 2.0,
    ) -> LLMResult:
        """One chat completion. Records llm_usage TraceEvent on success."""
        kwargs: dict[str, Any] = {
            "model": self.api_model, "messages": messages, "temperature": temperature,
        }
        if tools is not None:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        _reasoning = reasoning if reasoning is not None else (
            {"effort": reasoning_effort} if reasoning_effort is not None else None)
        if _reasoning is not None:
            kwargs["extra_body"] = {"reasoning": _reasoning}

        t0 = time.time()
        resp = None
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as e:
                last_exc = e
                if attempt >= max_retries:
                    raise
                delay = retry_base_delay_s * (2 ** attempt)
                time.sleep(delay)
                continue

            usage = getattr(resp, "usage", None)
            t_in_probe = int(getattr(usage, "prompt_tokens", 0)) if usage else 0
            choice = resp.choices[0] if resp.choices else None
            msg = getattr(choice, "message", None) if choice else None
            has_content = bool(getattr(msg, "content", None))
            has_tools = bool(getattr(msg, "tool_calls", None))
            degenerate = (t_in_probe == 0) and not has_content and not has_tools

            if degenerate and attempt < max_retries:
                delay = retry_base_delay_s * (2 ** attempt)
                time.sleep(delay)
                continue
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
                    "id": tc.id, "name": tc.function.name, "arguments": args,
                })

        usage = getattr(resp, "usage", None)
        t_in = int(getattr(usage, "prompt_tokens", 0)) if usage else 0
        t_out = int(getattr(usage, "completion_tokens", 0)) if usage else 0
        usd = estimate_usd(self.model, t_in, t_out)

        result = LLMResult(
            text=msg.content, tool_calls=tool_calls_list,
            finish_reason=choice.finish_reason or "",
            tokens_in=t_in, tokens_out=t_out, usd=usd, model=self.model,
            raw={"wall_s": wall_s, "role_tag": role_tag},
        )

        if self.recorder is not None:
            chat_parent = parent_event_id if parent_event_id is not None else self.parent_event_id
            self.recorder.append(TraceEvent(
                event_id=f"llm-{uuid.uuid4().hex[:8]}",
                parent_id=chat_parent, ts=utcnow_iso(), kind="llm_usage",
                payload={
                    "in": t_in, "out": t_out, "usd": usd, "model": self.model,
                    "role": role_tag, "wall_s": round(wall_s, 3),
                    "finish_reason": result.finish_reason,
                    "n_tool_calls": len(tool_calls_list),
                },
            ))

        return result
