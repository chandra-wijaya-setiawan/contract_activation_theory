"""Individual tool implementations.

Each tool is one module with a consistent interface:
  - create_xxx_capability() -> Capability

Core tools (domain-agnostic, local execution):
  bash, read, write, list, str_replace, done, give_up

Docker-only tools (require exec_fn closure bound to container.exec_run):
  multi_edit, apply_patch, rg, fd, outline, goto_def, find_refs,
  git_diff, git_status

Domain-specific tools are registered by the task adapter (e.g., swe_bench.py).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from cat.capability.registry import Capability

from cat.capability.tools.bash import create_bash_capability
from cat.capability.tools.read import create_read_capability
from cat.capability.tools.write import create_write_capability
from cat.capability.tools.list import create_list_capability
from cat.capability.tools.str_replace import create_str_replace_capability
from cat.capability.tools.done import create_done_capability
from cat.capability.tools.give_up import create_give_up_capability

# Docker-only tools (imported lazily by create_docker_toolset)
from cat.capability.tools.rg import create_rg_capability
from cat.capability.tools.fd import create_fd_capability
from cat.capability.tools.outline import create_outline_capability
from cat.capability.tools.goto_def import create_goto_def_capability
from cat.capability.tools.find_refs import create_find_refs_capability
from cat.capability.tools.git_diff import create_git_diff_capability
from cat.capability.tools.git_status import create_git_status_capability
from cat.capability.tools.multi_edit import create_multi_edit_capability
from cat.capability.tools.apply_patch import create_apply_patch_capability


def create_default_toolset() -> list[Capability]:
    """Return the core domain-agnostic capabilities (local execution only)."""
    return [
        create_bash_capability(),
        create_read_capability(),
        create_write_capability(),
        create_list_capability(),
        create_str_replace_capability(),
        create_done_capability(),
        create_give_up_capability(),
    ]


def create_docker_toolset(exec_fn: Callable, default_workdir: str = "/testbed") -> list[Capability]:
    """Return all capabilities for docker-backed execution (SWE-bench etc.).

    exec_fn(argv, workdir) -> (exit_code, output_str) must run commands inside
    the container (e.g. wrapping container.exec_run from the docker SDK).
    """
    return [
        # Core tools — docker variants (richer read with offset/limit etc.)
        create_bash_capability(),
        create_read_capability(),
        create_write_capability(),
        create_list_capability(),
        create_str_replace_capability(),
        # Docker-only tools
        create_rg_capability(exec_fn, default_workdir),
        create_fd_capability(exec_fn, default_workdir),
        create_outline_capability(exec_fn, default_workdir),
        create_goto_def_capability(exec_fn, default_workdir),
        create_find_refs_capability(exec_fn, default_workdir),
        create_git_diff_capability(exec_fn, default_workdir),
        create_git_status_capability(exec_fn, default_workdir),
        create_multi_edit_capability(exec_fn, default_workdir),
        create_apply_patch_capability(exec_fn, default_workdir),
        # Exit signals
        create_done_capability(),
        create_give_up_capability(),
    ]


__all__ = [
    "create_default_toolset",
    "create_docker_toolset",
    "create_bash_capability",
    "create_read_capability",
    "create_write_capability",
    "create_list_capability",
    "create_str_replace_capability",
    "create_done_capability",
    "create_give_up_capability",
    "create_rg_capability",
    "create_fd_capability",
    "create_outline_capability",
    "create_goto_def_capability",
    "create_find_refs_capability",
    "create_git_diff_capability",
    "create_git_status_capability",
    "create_multi_edit_capability",
    "create_apply_patch_capability",
]
