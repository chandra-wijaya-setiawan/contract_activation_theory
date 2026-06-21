"""LLM model adapters — per-model request/response shaping.

Most models need no subclass — they use the universal LLMModelAdapter base.
Subclass only when a model's request-shaping or output-parsing genuinely differs
(e.g. Kimi's reasoning-off + JSON prefill + <|tool_call|> markers).

Adapter resolution chain (most-specific → fallback):
    LLMModelAdapterKimi26   — k2.6 version params
    LLMModelAdapterKimiBase — Kimi-family shared behavior
    LLMModelAdapter         — universal fallback (unknown models)
"""
from cat.llm.adapters.adapters import (
    AdapterResult,
    LLMModelAdapter,
    LLMModelAdapterKimiBase,
    LLMModelAdapterKimi26,
    adapter_for,
)

__all__ = [
    "AdapterResult",
    "LLMModelAdapter",
    "LLMModelAdapterKimiBase",
    "LLMModelAdapterKimi26",
    "adapter_for",
]
