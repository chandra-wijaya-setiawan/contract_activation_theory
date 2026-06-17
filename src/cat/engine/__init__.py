"""Agent loop engine.

The engine package owns the contract discharge loop:
  - base.py           BaseActivator — scaffolding (charge, transition, finalize, admit)
  - activator.py      HypothesisLoopActivator — v3 hypothesis search loop (standalone)
  - checkpoint.py     Checkpointer protocol + NullCheckpointer
  - knowledge.py      KnowledgeSchema accumulation across hypotheses

See design.v3.md §"The Contract Structure" and §"Outer Loop".
"""
from cat.engine.base import BaseActivator, ActivatorRun
from cat.engine.activator import HypothesisLoopActivator
from cat.engine.checkpoint import Checkpointer, NullCheckpointer

__all__ = [
    "BaseActivator",
    "ActivatorRun",
    "HypothesisLoopActivator",
    "Checkpointer",
    "NullCheckpointer",
]
