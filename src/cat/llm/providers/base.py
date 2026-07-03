"""ProviderABC — the interface every provider implements.

Three methods, no defaults. Each provider explicitly implements:
  .schema()    — structured JSON output (Pydantic-validated)
  .text()      — freeform text + native tool calls
  .toolcall()  — structured PerformerStep with intent + tools + expectations

If a provider can't do one natively, it implements the fallback explicitly
inside its own file (e.g. text extraction instead of native structured output).
No shared base implementation — surgical precision per provider.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ProviderResult:
    """One provider call result — uniform across all providers."""
    text: str = ""
    parsed: Optional[dict] = None           # schema/toolcall: extracted JSON dict
    tool_calls: list = field(default_factory=list)  # text: native tool calls
    finish_reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    usd: float = 0.0
    model: str = ""
    raw: dict = field(default_factory=dict)


class ProviderABC(ABC):
    """Interface every provider implements. No default behavior."""

    def __init__(self, model_id: str, *, llm: Any = None, recorder: Any = None) -> None:
        self.model_id = model_id
        self.llm = llm
        self.recorder = recorder

    @abstractmethod
    def schema(
        self,
        messages: list[dict],
        schema: Any,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Structured JSON output — Pydantic-validated response."""
        ...

    @abstractmethod
    def text(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Freeform text + native tool calls."""
        ...

    @abstractmethod
    def toolcall(
        self,
        messages: list[dict],
        schema: Any,
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> ProviderResult:
        """Structured PerformerStep — intent + tools + expectations."""
        ...
