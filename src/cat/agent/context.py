"""AgentContext and ContractExecution — session state and execution result."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cat.contract.model import ContractState, Cost, Representation


@dataclass
class ContractExecution:
    """Immutable result returned by Agent.execute_contract()."""
    contract_id: str = ""
    obligation:  str = ""
    outcome:     str = ""
    state:       ContractState = ContractState.FAILED
    cost:        Cost = field(default_factory=Cost)
    trace_id:    str = ""
    depth:       int = 0


@dataclass
class AgentContext:
    """Session-scoped context shared across turns."""
    workdir:           Path                   = field(default_factory=lambda: Path("."))
    workspace_reps:    list[Representation]   = field(default_factory=list)
    chat_history:      list[dict]             = field(default_factory=list)
    prior_executions:  list[ContractExecution] = field(default_factory=list)
