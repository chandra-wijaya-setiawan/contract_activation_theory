"""Run metrics — per-task KPI aggregation.

Field names align with OTel GenAI semantic conventions where applicable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class RunMetrics:
    task_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    usd: float = 0.0
    wall_s: float = 0.0
    iterations: int = 0
    verdict: str = ""
    role_tokens: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def from_jsonl(path: str) -> "RunMetrics":
        ...

    def summary(self) -> dict[str, Any]:
        ...


@dataclass
class AggregateMetrics:
    n_runs: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tool_calls: int = 0
    total_llm_calls: int = 0
    total_usd: float = 0.0
    avg_pass_rate: float = 0.0

    @staticmethod
    def from_runs(runs: list[RunMetrics]) -> "AggregateMetrics":
        ...

    def table(self) -> str:
        ...


def compare(cat_run: RunMetrics, react_run: RunMetrics) -> dict[str, Any]:
    """Compare CAT vs ReAct metrics."""
    ...
