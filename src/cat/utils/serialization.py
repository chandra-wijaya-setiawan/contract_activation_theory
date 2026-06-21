"""Serialization utilities for LLM responses."""
from __future__ import annotations

import json
from typing import Optional


def extract_json_object(text: str) -> Optional[dict]:
    """Robustly pull a JSON object out of an LLM response.

    Handles: pure JSON, markdown-fenced JSON, and JSON embedded in prose
    (some models — notably Opus — prepend reasoning even under json_object mode).
    Returns the parsed dict or None.
    """
    if not text:
        return None
    s = text.strip()
    # 1. Try as-is.
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except Exception:
        pass
    # 2. Strip a markdown fence if present.
    if "```" in s:
        try:
            body = s.split("```", 2)[1]
            if body.startswith("json"):
                body = body[4:]
            d = json.loads(body.strip())
            return d if isinstance(d, dict) else None
        except Exception:
            pass
    # 3. Find the first balanced {...} block anywhere in the text.
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = s[start:i + 1]
                        try:
                            d = json.loads(candidate)
                            if isinstance(d, dict):
                                return d
                        except Exception:
                            break  # try next '{'
        start = s.find("{", start + 1)
    return None


_PRICING: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-v4-flash": (0.20, 0.50),
    "nvidia/nemotron-3-ultra-550b-a55b": (0.0, 0.0),
    "deepseek/deepseek-v4-pro": (0.85, 2.50),
    "deepseek/deepseek-chat-v3.1": (0.27, 1.10),
    "deepseek/deepseek-chat-v3-0324": (0.27, 1.10),
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "moonshotai/kimi-k2": (0.30, 1.20),
    "moonshotai/kimi-k2-0905": (0.30, 1.20),
    "z-ai/glm-4.5-air": (0.20, 1.00),
}


def estimate_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate USD cost from model and token counts."""
    pi, po = _PRICING.get(_strip_provider(model), (0.5, 2.0))
    return (tokens_in * pi + tokens_out * po) / 1_000_000.0


def _strip_provider(model: str) -> str:
    """Strip provider prefix from model ID (openrouter/, ollama/, etc.)."""
    for prefix in ("openrouter/", "ollama/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model
