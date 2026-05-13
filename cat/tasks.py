"""Task / benchmark schemas + synthetic tasks.

Vendored from ai_math_base_resolver/pco/bench/{schema,synthetic}.py with one
addition: a longer synthetic task (`multi_step_refactor`) that stresses the
discipline difference between ReAct and CAT past Pi's empirical drift point
(~10 steps).
"""
from __future__ import annotations
from pathlib import Path
from typing import Callable, Literal, Optional, Tuple

from pydantic import BaseModel, Field


class BudgetCaps(BaseModel):
    wall_s: int = Field(600, ge=1)
    usd: float = Field(2.0, ge=0)
    tool_calls: int = Field(50, ge=1)
    tokens_total: int = Field(200_000, ge=1, description="hard ceiling on in+out tokens")


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
    agent: str
    verdict: Literal["solved", "failed", "error", "timeout", "over_budget"]
    score: float = 0.0
    wall_s: float
    usd: float
    tool_calls: int
    n_loops: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None
    meta: dict = Field(default_factory=dict)


SyntheticOracle = Callable[[Path], Tuple[bool, float]]
_ORACLES: dict[str, SyntheticOracle] = {}


def register_oracle(task_id: str):
    def deco(fn: SyntheticOracle) -> SyntheticOracle:
        _ORACLES[task_id] = fn
        return fn
    return deco


def grade(task_id: str, workdir: Path) -> tuple[bool, float]:
    fn = _ORACLES.get(task_id)
    if fn is None:
        raise KeyError(f"no synthetic oracle for {task_id!r}")
    return fn(workdir)


# ── Synthetic tasks ─────────────────────────────────────────────────────────

HELLO_FILE_INSTRUCTION = """\
Create a file called `hello.py` in the current directory whose only line is:

    print("hello cat")

Then run it with python (use the `bash` tool: `python hello.py`). After you see
the expected output, you are done.
"""


@register_oracle("hello-file")
def _grade_hello_file(workdir: Path) -> tuple[bool, float]:
    f = workdir / "hello.py"
    if not f.exists():
        return False, 0.0
    txt = f.read_text(encoding="utf-8").strip()
    return ('print("hello cat")' in txt or "print('hello cat')" in txt), 1.0


def hello_file_task() -> TaskSpec:
    return TaskSpec(
        task_id="hello-file",
        bench="synthetic",
        instruction=HELLO_FILE_INSTRUCTION,
        oracle=OracleSpec(kind="custom", task_id="hello-file"),
        budget=BudgetCaps(wall_s=120, usd=0.25, tool_calls=15, tokens_total=20_000),
        meta={"difficulty": "trivial", "purpose": "Phase-1 plumbing smoke"},
    )


MULTI_STEP_INSTRUCTION = """\
Build a small Python module in the current directory:

1. Create `mathutil.py` containing two functions:
     - `add(a, b)` returns a + b
     - `mul(a, b)` returns a * b
2. Create `tests/test_mathutil.py` with two pytest tests, one per function.
3. Create `tests/__init__.py` (empty).
4. Run `python -m pytest tests/ -q` and confirm both tests pass.
5. Reply with the final pytest summary line.
"""


@register_oracle("multi-step-refactor")
def _grade_multi_step(workdir: Path) -> tuple[bool, float]:
    util = workdir / "mathutil.py"
    test = workdir / "tests" / "test_mathutil.py"
    init = workdir / "tests" / "__init__.py"
    if not util.exists() or not test.exists() or not init.exists():
        return False, 0.0
    u = util.read_text(encoding="utf-8")
    t = test.read_text(encoding="utf-8")
    has_add = "def add" in u and "+" in u
    has_mul = "def mul" in u and "*" in u
    has_test_add = "add(" in t
    has_test_mul = "mul(" in t
    return (has_add and has_mul and has_test_add and has_test_mul), 1.0


def multi_step_refactor_task() -> TaskSpec:
    return TaskSpec(
        task_id="multi-step-refactor",
        bench="synthetic",
        instruction=MULTI_STEP_INSTRUCTION,
        oracle=OracleSpec(kind="custom", task_id="multi-step-refactor"),
        budget=BudgetCaps(wall_s=300, usd=0.50, tool_calls=30, tokens_total=80_000),
        meta={"difficulty": "moderate", "purpose": "stress discipline past 10 steps"},
    )


def resolve_task(task_id: str) -> TaskSpec:
    if task_id == "hello-file":
        return hello_file_task()
    if task_id == "multi-step-refactor":
        return multi_step_refactor_task()
    raise ValueError(f"unknown task {task_id!r}")
