"""Domain adapters — benchmark-specific agent implementations.

Each adapter implements a standard interface for its benchmark:
  - make_agent(model, **kwargs) -> agent instance
  - load_tasks(path) -> list[TaskSpec]
  - evaluate(task, agent) -> Outcome

Adapters:
  - swe_bench.py       SWE-bench (TbSweAgent)
  - terminal_bench.py  Terminal-bench (TbCatAgent, TbReactAgent)
  - mini_swe.py        Mini-SWE (MiniSweOpenRouter)
  - tasks.py           Shared task schemas (TaskSpec, BudgetCaps, OracleSpec, Outcome)

Future: tau_bench.py, web_arena.py, crm_tasks.py
"""
from adapters.tasks import TaskSpec, BudgetCaps, OracleSpec, Outcome

__all__ = [
    "TaskSpec",
    "BudgetCaps",
    "OracleSpec",
    "Outcome",
]
