"""Task/benchmark schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class BudgetCaps(BaseModel):
    wall_s: int = Field(600, ge=1)
    usd: float = Field(2.0, ge=0)
    tool_calls: int = Field(50, ge=1)
    tokens_total: int = Field(200_000, ge=1)


class OracleSpec(BaseModel):
    kind: Literal["terminal_bench", "swebench", "custom"]
    task_id: str
    grader_args: dict = Field(default_factory=dict)


class TaskSpec(BaseModel):
    task_id: str
    bench: Literal["terminal_bench", "swebench", "synthetic"]
    instruction: str
    workdir_hint: Optional[str] = None
    oracle: OracleSpec
    budget: BudgetCaps = Field(default_factory=BudgetCaps)
    meta: dict = Field(default_factory=dict)


class Outcome(BaseModel):
    task_id: str
    run_id: str
    verdict: str = ""
    iterations: int = 0
    cost_usd: float = 0.0
