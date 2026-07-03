"""Provider registry — maps model_id prefix to provider implementation.

No default, no fallback. Every provider that needs different behavior gets
its own explicit implementation. Adding a new provider = one file + one
entry here.
"""
from __future__ import annotations

from typing import Any

from cat.llm.providers.base import ProviderABC
from cat.llm.providers.openrouter import OpenRouterProvider
from cat.llm.providers.ollama import OllamaProvider

# ── Provider table ──────────────────────────────────────────────────────
# Prefix → Provider class. No default — unknown prefix = explicit error.
PROVIDER_TABLE: dict[str, type[ProviderABC]] = {
    "ollama/": OllamaProvider,
    "openrouter/": OpenRouterProvider,
}


def get_provider(model_id: str, **kwargs: Any) -> ProviderABC:
    """Resolve a model_id to its provider instance.

    Raises ValueError for unknown prefixes — no silent fallback.
    """
    for prefix, cls in PROVIDER_TABLE.items():
        if model_id.startswith(prefix):
            return cls(model_id=model_id, **kwargs)
    raise ValueError(
        f"No provider registered for model '{model_id}'. "
        f"Registered prefixes: {list(PROVIDER_TABLE.keys())}. "
        f"Add a new provider in cat/llm/providers/ for this model."
    )
