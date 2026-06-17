"""Backward-compatibility shim — imports from cat.engine.checkpoint_git."""
from cat.engine.checkpoint import Checkpointer, NullCheckpointer
from cat.engine.checkpoint_git import GitCheckpointer

__all__ = ["Checkpointer", "NullCheckpointer", "GitCheckpointer"]
