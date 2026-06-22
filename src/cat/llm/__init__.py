"""LLM layer — provider-agnostic model dispatch.

The llm package owns all model interaction:
  - client.py         LLM client (OpenAI wrapper, token harvest, no provider logic)
  - kernel.py         LLMKernel — single entry point (self-healing, intelligence-addressed)
  - providers/        Provider implementations (OpenRouter, Ollama, ...)
  - stipulator.py     Stipulator prompt builder
  - attestor.py       Attestor prompt builder
  - performer_plan.py Performer plan prompt builder
  - card.py           Static model metadata

Design:
  - LLMKernel is the single entry point — all LLM calls go through it
  - Providers shape requests per provider (no default, no fallback)
  - LLM is a dumb HTTP wrapper — provider already shaped the right kwargs
"""
from cat.llm.client import LLM, LLMResult
from cat.llm.kernel import LLMKernel, KernelResult
from cat.llm.card import LLMModelCard, card_for
from cat.llm.chat_judge import call_chat_judge
from cat.llm.stipulator import build_stipulator_messages
from cat.llm.attestor import build_attestor_messages
from cat.llm.performer_plan import build_performer_plan_messages

# backward-compat alias used by legacy tb_agents
call_judge = call_chat_judge

__all__ = [
    "LLM",
    "LLMResult",
    "LLMKernel",
    "KernelResult",
    "LLMModelCard",
    "card_for",
    "call_chat_judge",
    "call_judge",
    "build_stipulator_messages",
    "build_attestor_messages",
    "build_performer_plan_messages",
]
