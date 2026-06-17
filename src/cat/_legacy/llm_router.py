"""LLMRouter — routes a model_id to its backend and dispatches the call.

Today every model routes through OpenRouter via the OpenAI SDK (cat.llm.LLM).
A future direct-provider backend (Anthropic/OpenAI native) slots in here behind
the same `dispatch(model_id, **kwargs) -> LLMResult` surface — callers
(LLMModelAdapter) never know which wire was used.

Not to be confused with OpenRouter (the gateway) or LiteLLM's Router — this is
*our* thin dispatch layer.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from cat.llm import LLM, LLMResult


class LLMRouter:
    def __init__(self, recorder: Any = None,
                 client_factory: Optional[Callable[[str], Any]] = None) -> None:
        self.recorder = recorder
        self._factory = client_factory or (lambda m: LLM(model=m, recorder=recorder))
        self._clients: dict[str, Any] = {}

    def _client(self, model_id: str) -> Any:
        if model_id not in self._clients:
            self._clients[model_id] = self._factory(model_id)
        return self._clients[model_id]

    def dispatch(self, model_id: str, **chat_kwargs) -> LLMResult:
        """Send one request to the backend for model_id. Returns an LLMResult."""
        return self._client(model_id).chat(**chat_kwargs)
