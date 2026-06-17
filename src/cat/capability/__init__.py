"""Capability system — the executable side of a contract.

The capability package owns tool dispatch:
  - registry.py       CapabilityRegistry, Capability, ToolResult
  - tools/            Individual tool implementations (one per file)
  - git_checkpoint.py Git-backed checkpointer for hypothesis branching

Each capability is a named, schema-described action the activator can invoke.
Capabilities have no state of their own; they read/write the provided State
and return a structured ToolResult.

See design.v3.md §"Checkpoint Branching".
"""
from cat.capability.registry import CapabilityRegistry, Capability, ToolResult

__all__ = [
    "CapabilityRegistry",
    "Capability",
    "ToolResult",
]
