"""CAT agent package — agent loop, profiles, registry.

Public API:
    from cat.agent import AgentDef, LoopConfig, AgentRegistry
    from cat.agent import Activator, HypothesisAgent, ActivatorRun
    from cat.agent import Checkpointer, NullCheckpointer
"""
from cat.agent.base import Agent as BaseActivator, ActivatorRun
from cat.agent.hypothesis import HypothesisAgent, HypothesisLoopActivator
Activator = HypothesisLoopActivator
from cat.agent.definition import AgentDef, LoopConfig
from cat.agent.registry import AgentRegistry
from cat.agent.checkpoint import Checkpointer, NullCheckpointer

__all__ = [
    # Agent loop
    "BaseActivator",
    "Activator",  # alias for HypothesisLoopActivator
    "HypothesisAgent",
    "ActivatorRun",
    # Profile system
    "AgentDef",
    "LoopConfig",
    "AgentRegistry",
    # Checkpointing
    "Checkpointer",
    "NullCheckpointer",
]
