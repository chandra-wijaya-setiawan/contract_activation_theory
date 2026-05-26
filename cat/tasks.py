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


# -- Composable oracle predicates -------------------------------------------
# Each returns a (workdir: Path) -> bool callable. Compose with all_of / any_of
# to build oracles for multi-criterion tasks.

Predicate = Callable[[Path], bool]


def file_exists(rel_path: str) -> Predicate:
    def _check(workdir: Path) -> bool:
        return (workdir / rel_path).exists()
    return _check


def file_contains(rel_path: str, needle: str) -> Predicate:
    def _check(workdir: Path) -> bool:
        p = workdir / rel_path
        if not p.exists():
            return False
        return needle in p.read_text(encoding="utf-8", errors="replace")
    return _check


def file_not_contains(rel_path: str, needle: str) -> Predicate:
    def _check(workdir: Path) -> bool:
        p = workdir / rel_path
        if not p.exists():
            return True
        return needle not in p.read_text(encoding="utf-8", errors="replace")
    return _check


def bash_succeeds(cmd: str, timeout_s: int = 60) -> Predicate:
    import subprocess
    def _check(workdir: Path) -> bool:
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(workdir),
                capture_output=True, text=True, timeout=timeout_s,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False
    return _check


def ast_has_function(rel_path: str, func_name: str) -> Predicate:
    import ast
    def _check(workdir: Path) -> bool:
        p = workdir / rel_path
        if not p.exists():
            return False
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            return False
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
            for node in ast.walk(tree)
        )
    return _check


def all_of(*predicates: Predicate) -> Predicate:
    def _check(workdir: Path) -> bool:
        return all(p(workdir) for p in predicates)
    return _check


def any_of(*predicates: Predicate) -> Predicate:
    def _check(workdir: Path) -> bool:
        return any(p(workdir) for p in predicates)
    return _check


def oracle_from_predicates(task_id: str, *predicates: Predicate) -> SyntheticOracle:
    combined = all_of(*predicates)
    def _oracle(workdir: Path) -> Tuple[bool, float]:
        ok = combined(workdir)
        return ok, (1.0 if ok else 0.0)
    _ORACLES[task_id] = _oracle
    return _oracle


# -- Synthetic tasks ---------------------------------------------------------

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


# -- Task: bug-fix (~8 steps) -----------------------------------------------
# Seed: a module with an off-by-one bug + a test that exposes it.

BUG_FIX_SEED = {
    "pagination.py": '''\
"""Pagination utilities."""


def paginate(items: list, page: int, per_page: int = 10) -> dict:
    """Return a page of items with metadata.

    pages are 1-indexed. Returns dict with keys:
      items, page, per_page, total_items, total_pages
    """
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    # BUG: off-by-one -- should be (page - 1) * per_page
    start = page * per_page
    end = start + per_page
    return {
        "items": items[start:end],
        "page": page,
        "per_page": per_page,
        "total_items": total,
        "total_pages": total_pages,
    }


def page_links(current: int, total_pages: int) -> dict:
    """Return prev/next page numbers (None at boundaries)."""
    return {
        "prev": current - 1 if current > 1 else None,
        "next": current + 1 if current < total_pages else None,
    }
''',
    "tests/__init__.py": "",
    "tests/test_pagination.py": '''\
from pagination import paginate, page_links


def test_first_page():
    items = list(range(25))
    result = paginate(items, page=1, per_page=10)
    assert result["items"] == list(range(10)), f"got {result['items']}"
    assert result["page"] == 1
    assert result["total_pages"] == 3


def test_last_page():
    items = list(range(25))
    result = paginate(items, page=3, per_page=10)
    assert result["items"] == [20, 21, 22, 23, 24]
    assert result["page"] == 3


def test_page_links_boundaries():
    assert page_links(1, 5) == {"prev": None, "next": 2}
    assert page_links(5, 5) == {"prev": 4, "next": None}
    assert page_links(3, 5) == {"prev": 2, "next": 4}
''',
}


BUG_FIX_INSTRUCTION = """\
The file `pagination.py` has a bug: `paginate()` returns wrong items for
page 1 (the first page is empty instead of showing the first `per_page` items).

The test suite in `tests/test_pagination.py` already covers the expected behavior
but currently fails.

Your task:
1. Read the existing code and tests to understand the bug.
2. Fix the bug in `pagination.py`.
3. Run `python -m pytest tests/ -q` and confirm ALL tests pass.
4. Reply with the one-line diff description of what you changed.

Do NOT modify the tests -- only fix the source code.
"""


oracle_from_predicates(
    "bug-fix",
    file_exists("pagination.py"),
    file_exists("tests/test_pagination.py"),
    file_contains("pagination.py", "(page - 1)"),
    file_not_contains("pagination.py", "start = page * per_page"),
    bash_succeeds("python -m pytest tests/ -q"),
)


def bug_fix_task() -> TaskSpec:
    return TaskSpec(
        task_id="bug-fix",
        bench="synthetic",
        instruction=BUG_FIX_INSTRUCTION,
        oracle=OracleSpec(kind="custom", task_id="bug-fix"),
        budget=BudgetCaps(wall_s=180, usd=0.50, tool_calls=25, tokens_total=60_000),
        meta={"difficulty": "moderate", "steps_expected": 8, "seed": BUG_FIX_SEED},
    )


# -- Task: multi-file-rename (~10 steps) -----------------------------------
# Seed: 3-file module where a function must be renamed across all files.

RENAME_SEED = {
    "strutil.py": '''\
"""String utilities."""


def camelToSnake(text: str) -> str:
    """Convert camelCase to snake_case."""
    import re
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\\1_\\2", text)
    return re.sub(r"([a-z0-9])([A-Z])", r"\\1_\\2", s1).lower()


def snakeToCamel(text: str) -> str:
    """Convert snake_case to camelCase."""
    parts = text.split("_")
    return parts[0] + "".join(w.capitalize() for w in parts[1:])
''',
    "cli.py": '''\
"""CLI entry point for string conversion."""
import sys
from strutil import camelToSnake, snakeToCamel


def main():
    if len(sys.argv) < 3:
        print("Usage: cli.py <to_snake|to_camel> <text>")
        sys.exit(1)
    cmd, text = sys.argv[1], sys.argv[2]
    if cmd == "to_snake":
        print(camelToSnake(text))
    elif cmd == "to_camel":
        print(snakeToCamel(text))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
''',
    "tests/__init__.py": "",
    "tests/test_strutil.py": '''\
from strutil import camelToSnake, snakeToCamel


def test_camel_to_snake():
    assert camelToSnake("helloWorld") == "hello_world"
    assert camelToSnake("HTMLParser") == "html_parser"


def test_snake_to_camel():
    assert snakeToCamel("hello_world") == "helloWorld"
    assert snakeToCamel("html_parser") == "htmlParser"
''',
}


RENAME_INSTRUCTION = """\
The codebase has two functions with non-PEP-8 names that must be renamed:

  camelToSnake  ->  camel_to_snake
  snakeToCamel  ->  snake_to_camel

The rename must be applied consistently across ALL files:
  - `strutil.py` (definitions)
  - `cli.py` (imports and calls)
  - `tests/test_strutil.py` (imports and calls)

Your task:
1. Read all three files to understand the current usage.
2. Rename both functions in `strutil.py`.
3. Update imports and calls in `cli.py`.
4. Update imports and calls in `tests/test_strutil.py`.
5. Run `python -m pytest tests/ -q` and confirm all tests pass.
6. Run `python cli.py to_snake helloWorld` and confirm output is `hello_world`.
7. Reply with a summary of files changed.
"""


oracle_from_predicates(
    "multi-file-rename",
    # Definitions renamed
    ast_has_function("strutil.py", "camel_to_snake"),
    ast_has_function("strutil.py", "snake_to_camel"),
    file_not_contains("strutil.py", "camelToSnake"),
    file_not_contains("strutil.py", "snakeToCamel"),
    # CLI updated
    file_contains("cli.py", "camel_to_snake"),
    file_not_contains("cli.py", "camelToSnake"),
    # Tests updated
    file_contains("tests/test_strutil.py", "camel_to_snake"),
    file_not_contains("tests/test_strutil.py", "camelToSnake"),
    # Tests pass
    bash_succeeds("python -m pytest tests/ -q"),
)


def multi_file_rename_task() -> TaskSpec:
    return TaskSpec(
        task_id="multi-file-rename",
        bench="synthetic",
        instruction=RENAME_INSTRUCTION,
        oracle=OracleSpec(kind="custom", task_id="multi-file-rename"),
        budget=BudgetCaps(wall_s=240, usd=0.75, tool_calls=35, tokens_total=80_000),
        meta={"difficulty": "moderate", "steps_expected": 10, "seed": RENAME_SEED},
    )


# -- Task: test-generation (~12 steps) -------------------------------------
# Seed: a utility module with 4 functions, no tests. Agent writes tests.

TESTGEN_SEED = {
    "mathlib.py": '''\
"""Math utility library with edge cases worth testing."""
import math


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Division that returns default on zero denominator or non-finite result."""
    if b == 0:
        return default
    result = a / b
    if not math.isfinite(result):
        return default
    return result


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value to [lo, hi]. Raises ValueError if lo > hi."""
    if lo > hi:
        raise ValueError(f"lo ({lo}) > hi ({hi})")
    return max(lo, min(hi, value))


def running_mean(values: list[float]) -> list[float]:
    """Return the cumulative running mean. Empty input returns empty output."""
    if not values:
        return []
    means = []
    total = 0.0
    for i, v in enumerate(values, 1):
        total += v
        means.append(total / i)
    return means


def fizzbuzz(n: int) -> str:
    """Classic fizzbuzz for a single integer."""
    if n <= 0:
        raise ValueError("n must be positive")
    if n % 15 == 0:
        return "fizzbuzz"
    if n % 3 == 0:
        return "fizz"
    if n % 5 == 0:
        return "buzz"
    return str(n)
''',
    "tests/__init__.py": "",
}


TESTGEN_INSTRUCTION = """\
The file `mathlib.py` contains four functions: `safe_div`, `clamp`,
`running_mean`, and `fizzbuzz`. There are currently NO tests.

Your task:
1. Read `mathlib.py` to understand the functions and their edge cases.
2. Create `tests/test_mathlib.py` with pytest tests covering:
   - Normal cases for each function
   - Edge cases: zero division, non-finite, lo > hi ValueError, empty list,
     fizzbuzz multiples of 3, 5, 15, and boundary (n=0)
3. Run `python -m pytest tests/ -q` and confirm all tests pass.
4. Run `python -m pytest tests/ --tb=short -q` to verify no failures.
5. Reply with the count of test functions you wrote.

Aim for thorough coverage -- test the happy path AND the documented edge cases.
"""


oracle_from_predicates(
    "test-generation",
    file_exists("tests/test_mathlib.py"),
    file_contains("tests/test_mathlib.py", "safe_div"),
    file_contains("tests/test_mathlib.py", "clamp"),
    file_contains("tests/test_mathlib.py", "running_mean"),
    file_contains("tests/test_mathlib.py", "fizzbuzz"),
    # At least 6 test functions
    file_contains("tests/test_mathlib.py", "def test_"),
    bash_succeeds("python -m pytest tests/ -q"),
)


def test_generation_task() -> TaskSpec:
    return TaskSpec(
        task_id="test-generation",
        bench="synthetic",
        instruction=TESTGEN_INSTRUCTION,
        oracle=OracleSpec(kind="custom", task_id="test-generation"),
        budget=BudgetCaps(wall_s=300, usd=1.00, tool_calls=40, tokens_total=100_000),
        meta={"difficulty": "hard", "steps_expected": 12, "seed": TESTGEN_SEED},
    )


# -- Task registry ---------------------------------------------------------

_TASK_REGISTRY: dict[str, Callable[[], TaskSpec]] = {
    "hello-file": hello_file_task,
    "multi-step-refactor": multi_step_refactor_task,
    "bug-fix": bug_fix_task,
    "multi-file-rename": multi_file_rename_task,
    "test-generation": test_generation_task,
}


def resolve_task(task_id: str) -> TaskSpec:
    factory = _TASK_REGISTRY.get(task_id)
    if factory is None:
        raise ValueError(f"unknown task {task_id!r}; available: {sorted(_TASK_REGISTRY)}")
    return factory()


def all_task_ids() -> list[str]:
    return sorted(_TASK_REGISTRY)
