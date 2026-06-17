"""Backward-compatibility shim — imports from cat.capability.full_registry."""
from cat.capability.full_registry import (
    OUTPUT_CAP_BYTES,
    ToolResult,
    Capability,
    CapabilityRegistry,
    build_default_registry,
    build_docker_registry,
    build_tmux_registry,
    workdir_snapshot,
    build_git_checkpointer,
)

__all__ = [
    "OUTPUT_CAP_BYTES",
    "ToolResult",
    "Capability",
    "CapabilityRegistry",
    "build_default_registry",
    "build_docker_registry",
    "build_tmux_registry",
    "workdir_snapshot",
    "build_git_checkpointer",
]
