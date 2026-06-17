"""Terminal-bench adapter — TbCatAgent + TbReactAgent."""
from __future__ import annotations


class TbCatAgent:
    """CAT agent for terminal-bench."""

    def __init__(self, model_name: str, **kwargs):
        ...

    def run(self, task: Any) -> Any:
        ...


class TbReactAgent:
    """ReAct baseline agent for terminal-bench."""

    def __init__(self, model_name: str, **kwargs):
        ...

    def run(self, task: Any) -> Any:
        ...
