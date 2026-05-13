"""Trace schema + recorder. One normalized event stream regardless of agent.

Vendored from ai_math_base_resolver/pco/trace/{schema,recorder}.py (read-only
upstream). Extended here with CAT-specific event kinds: contract_state,
contract_decompose, criterion_check, capability_activate.

JSONL is the live ledger (resumable, tail-able). Parquet is the analysis
artifact (columnar, queryable across runs).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


EventKind = Literal[
    "session_start",
    "session_end",
    "user_message",
    "thinking",
    "assistant_message",
    "tool_call",
    "tool_result",
    "llm_usage",
    "step_end",
    # CAT-specific
    "contract_state",        # state transition: payload = {contract_id, from, to, reason}
    "contract_decompose",    # planner emitted criteria + sub-contracts
    "criterion_check",       # verifier ran a criterion: {criterion_id, satisfied, evidence}
    "capability_activate",   # capability invoked under a contract
]


class TraceEvent(BaseModel):
    """One event in a run. Append-only JSONL on disk; parquet on finalize."""
    event_id: str = Field(description="unique within run")
    parent_id: Optional[str] = Field(None, description="prior event in the causal chain")
    ts: str = Field(description="ISO-8601 UTC")
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)


class Trajectory(BaseModel):
    run_id: str
    task_id: str
    agent: str
    model: str
    started_at: str
    ended_at: Optional[str] = None
    events: list[TraceEvent] = Field(default_factory=list)
    n_tool_calls: int = 0
    n_steps: int = 0
    n_loops: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Recorder:
    """Append events live, build the Trajectory in memory, persist on close.

    Layout:
        out_dir/
          {run_id}.jsonl
          {run_id}.trajectory.parquet
          {run_id}.outcome.json
    """

    def __init__(
        self,
        run_id: str,
        task_id: str,
        agent: str,
        model: str,
        out_dir: Path,
    ):
        self.run_id = run_id
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.out_dir / f"{run_id}.jsonl"
        self.parquet_path = self.out_dir / f"{run_id}.trajectory.parquet"

        self.traj = Trajectory(
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            model=model,
            started_at=utcnow_iso(),
        )
        self._fh = self.jsonl_path.open("w", encoding="utf-8")

    def append(self, event: TraceEvent) -> None:
        self.traj.events.append(event)
        self._fh.write(event.model_dump_json() + "\n")
        self._fh.flush()
        if event.kind == "tool_call":
            self.traj.n_tool_calls += 1
        elif event.kind == "step_end":
            self.traj.n_steps += 1
        elif event.kind == "llm_usage":
            p = event.payload
            self.traj.tokens_in += int(p.get("in", 0))
            self.traj.tokens_out += int(p.get("out", 0))
            self.traj.usd += float(p.get("usd", 0.0))
        elif event.kind == "capability_activate":
            self.traj.n_loops += 1

    def finalize(self) -> Trajectory:
        self.traj.ended_at = utcnow_iso()
        self._fh.close()
        self._write_parquet()
        return self.traj

    def _write_parquet(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = [
            {
                "run_id": self.run_id,
                "task_id": self.traj.task_id,
                "event_id": ev.event_id,
                "parent_id": ev.parent_id or "",
                "ts": ev.ts,
                "kind": ev.kind,
                "payload_json": json.dumps(ev.payload, ensure_ascii=False),
            }
            for ev in self.traj.events
        ]
        if not rows:
            return
        pq.write_table(pa.Table.from_pylist(rows), self.parquet_path)


def replay(jsonl_path: Path) -> list[TraceEvent]:
    out: list[TraceEvent] = []
    with Path(jsonl_path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(TraceEvent.model_validate_json(line))
    return out
