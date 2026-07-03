"""Mini-SWE-agent wrapper that supports OpenRouter model names.

terminal_bench's MiniSweAgent splits model_name on "/" expecting exactly
two parts (provider/model).  OpenRouter models have three parts
(openrouter/org/model).  This subclass fixes the split and routes the
API key via MSWEA_API_KEY so the agent picks it up inside the container.

Usage:
    tb runs create --agent-import-path benchmarks.mini_swe_openrouter:MiniSweOpenRouter \
                   --model openrouter/deepseek/deepseek-v4-flash ...
"""
from __future__ import annotations

import os
from pathlib import Path

from terminal_bench.agents.installed_agents.mini_swe_agent.mini_swe_agent import (
    MiniSweAgent,
)
import inspect as _inspect


class MiniSweOpenRouter(MiniSweAgent):
    """MiniSweAgent with OpenRouter support."""

    def __init__(self, model_name: str, *args, **kwargs) -> None:
        # Bypass parent __init__ to avoid the 2-part split assertion,
        # then call grandparent and set attributes manually.
        super(MiniSweAgent, self).__init__(*args, **kwargs)
        self._model_name = model_name
        self._provider = model_name.split("/")[0]

    @property
    def _env(self) -> dict[str, str]:
        env = {"MSWEA_CONFIGURED": "true"}
        if self._provider == "openrouter":
            key = os.environ.get("OPENROUTER_API_KEY", "")
            if not key:
                raise ValueError("OPENROUTER_API_KEY not set")
            env["MSWEA_API_KEY"] = key
        else:
            env.update(super()._env)
        return env

    @property
    def _install_agent_script_path(self) -> Path:
        # Point to the real template in the installed mini_swe_agent package
        real_dir = Path(_inspect.getfile(MiniSweAgent)).parent
        return real_dir / "mini-swe-setup.sh.j2"

    @staticmethod
    def name() -> str:
        return "mini-swe-openrouter"
