"""LLM layer — provider-agnostic model dispatch.

The llm package owns all model interaction:
  - client.py         LLM client (OpenAI wrapper, token harvest)
  - kernel.py         LLMKernel — self-healing, intelligence-addressed
  - stipulator.py     Stipulator prompt + call
  - attestor.py       Attestor prompt + call
  - performer_plan.py Continuation plan generation
  - adapters.py       Per-model adapters (Kimi, etc.)
  - router.py         Model→backend dispatch
  - card.py           Static model metadata

Design notes:
  - LLMKernel is the "brain/CPU" — self-heals (reask + escalate) on bad output
  - Adapters handle per-model request/response shaping
  - Router dispatches to the correct backend for a model_id
"""
from cat.llm.client import LLM, LLMResult, call_structured
from cat.llm.kernel import LLMKernel, KernelResult
from cat.llm.stipulator import call_stipulator_v3, build_stipulator_messages
from cat.llm.attestor import call_attestor_v3
from cat.llm.performer_plan import call_performer_plan, build_performer_plan_messages
from cat.llm.router import LLMRouter
from cat.llm.card import LLMModelCard, card_for

# backward-compat alias used by legacy tb_agents
call_judge = call_attestor_v3

__all__ = [
    "LLM",
    "LLMResult",
    "call_structured",
    "LLMKernel",
    "KernelResult",
    "call_stipulator_v3",
    "build_stipulator_messages",
    "call_attestor_v3",
    "call_judge",
    "call_performer_plan",
    "build_performer_plan_messages",
    "LLMRouter",
    "LLMModelCard",
    "card_for",
]
