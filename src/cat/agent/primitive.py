"""PrimitiveAgent — single-handler execution, no hypothesis loop.

A primitive agent executes one tool call and returns immediately.
Defined by agent.yaml with executor: primitive + a handler file.
"""
from __future__ import annotations

# Placeholder — PrimitiveAgent execution is not yet wired.
# When agent.yaml specifies executor: primitive, the AgentRegistry
# returns the AgentDef and the caller decides how to invoke the handler.
