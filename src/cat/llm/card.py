"""LLMModelCard — static metadata for a model (analytics, not runtime control).

A card describes WHO made the model and WHAT family/variant it is, so the
analytics layer can aggregate by producer/family later (written to a separate
model_usage parquet — never the operational trace).

The runtime model array is a list of card `model_id`s; cards are looked up for
the analytics dimensions. producer/family are declared (not prefix-derived),
because family isn't reliably in the id (e.g. claude-opus-4-5 → family "Opus").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LLMModelCard:
    model_id: str            # "openrouter/moonshotai/kimi-k2.6"
    producer: str            # manufacturer org: "Moonshot AI"
    family: str              # "Kimi K2"
    variant: str = ""        # "k2.6" / "thinking" vs "instruct"


# Known cards (extend as models are adopted). Keyed by a substring of model_id.
_CARDS: list[LLMModelCard] = [
    LLMModelCard("openrouter/deepseek/deepseek-v4-flash", "DeepSeek", "DeepSeek V4", "flash"),
    LLMModelCard("openrouter/deepseek/deepseek-v4-pro",   "DeepSeek", "DeepSeek V4", "pro"),
    LLMModelCard("openrouter/owl-alpha",                  "OpenRouter", "Owl Alpha", "alpha"),
    LLMModelCard("openrouter/moonshotai/kimi-k2.6",       "Moonshot AI", "Kimi K2", "k2.6"),
    LLMModelCard("openrouter/anthropic/claude-opus-4.5",  "Anthropic", "Claude Opus", "4.5"),
    LLMModelCard("openrouter/openai/gpt-5.3-codex",       "OpenAI", "GPT-5 Codex", "5.3"),
    LLMModelCard("openrouter/minimax/minimax-m3",         "MiniMax", "MiniMax M3", "m3"),
    LLMModelCard("openrouter/qwen/qwen3.7-plus",          "Alibaba", "Qwen3", "3.7-plus"),
    LLMModelCard("openrouter/z-ai/glm-5",                 "Z.ai", "GLM", "5"),
]


def card_for(model_id: str) -> LLMModelCard:
    """Most-specific card match; unknown models get a derived best-effort card."""
    best: Optional[LLMModelCard] = None
    for c in _CARDS:
        # match on the model path (ignore the openrouter/ transport prefix)
        key = c.model_id.split("/", 1)[-1]
        if key in model_id or c.model_id in model_id:
            if best is None or len(c.model_id) > len(best.model_id):
                best = c
    if best is not None:
        return best
    # Unknown: derive producer from the path's vendor segment.
    parts = model_id.replace("openrouter/", "").split("/")
    producer = parts[0] if parts else "unknown"
    return LLMModelCard(model_id=model_id, producer=producer, family="unknown", variant="")
