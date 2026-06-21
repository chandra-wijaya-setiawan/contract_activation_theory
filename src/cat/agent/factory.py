"""AgentFactory — maps executor_type to Agent implementation."""
from __future__ import annotations

from cat.agent.registry import AgentRegistry


def build_agent(name_or_path: str):
    """Load an AgentDef by name and return the appropriate agent class instance.

    Currently returns the AgentDef itself — the HypothesisAgent is instantiated
    by the caller (cat_cli, benchmark harness) which owns the LLM + registry.
    This factory will grow as executor types multiply.
    """
    return AgentRegistry().load(name_or_path)
