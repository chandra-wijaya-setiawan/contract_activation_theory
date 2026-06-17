"""Checkpointer protocol + implementations.

Domain-agnostic snapshot/restore for hypothesis branching.

See design.v3.md §"Checkpoint Branching".
"""
from __future__ import annotations

from typing import Protocol


class Checkpointer(Protocol):
    """Snapshot/restore the mutable working state, keyed by string labels."""

    def snapshot(self, label: str) -> bool:
        """Capture the current working state under `label`. Returns success."""
        ...

    def restore(self, label: str) -> bool:
        """Reset the working state to a previously snapshotted `label`."""
        ...

    def labels(self) -> list[str]:
        """Return the labels that have been snapshotted so far."""
        ...


class NullCheckpointer:
    """No-op checkpointer — used when the environment has no snapshot support.

    The activator degrades gracefully to pure forward-editing.
    """

    def snapshot(self, label: str) -> bool:
        return False

    def restore(self, label: str) -> bool:
        return False

    def labels(self) -> list[str]:
        return []
