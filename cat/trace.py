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
    # CAT-specific — new contract-vocabulary names (per PRD 1.6.2.9):
    "contract_state",          # state transition: payload = {contract_id, from, to, reason}
    "contract_represent",      # surveyor ran probes: payload = {contract_id, representations}
    "contract_stipulate",      # stipulator emitted plan (was: contract_decompose)
    "contract_admit",          # Step 4 ACTIVATE: admission gate decision
    "criterion_check",         # verifier ran a criterion
    "capability_perform",      # Step 6 PERFORM: one performance act (was: capability_invoke)
    "election",                # v1.7 A.2 ADVISOR+ELECTION: math arbitrated between candidates
    "librarian_hint",          # v1.7 A.1 advisory hint was injected into performer context
    # v2 (iteration loop redesign) — plan tree events
    "plan_commit",
    "plan_checkout",
    "plan_drop",
    "plan_commit_new",
    "plan_refine",
    "plan_branch",
    "plan_merge",
    "surprise",                # surprise vector at end-of-iter
    "plan_patch",                  # PlanPatcher emitted tree-edit ops (was: stipulate_inloop)
    "stipulate_inloop",        # legacy alias for "plan_patch" — pre-rename traces
    "performer_response",      # full Performer text + tools called (added for LLM-reasoning visibility)
    "performer_give_up",       # R38: Performer gave up on a step
    "plan_restipulate",        # R37: full re-stipulate with failure context
    "plan_restipulate_failed", # R37: restipulate attempted but failed
    # Legacy aliases (kept readable for pre-rename traces):
    "contract_decompose",      # legacy alias for contract_stipulate
    "capability_invoke",       # legacy alias for capability_perform
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
        elif event.kind == "capability_perform" or event.kind == "capability_invoke":
            # accept both for backward-compat with legacy traces
            self.traj.n_loops += 1

    def finalize(self) -> Trajectory:
        self.traj.ended_at = utcnow_iso()
        self._fh.close()
        self._write_parquet()
        self._write_dag()
        return self.traj

    def _write_dag(self) -> None:
        """1.6.2: emit dag.json next to the JSONL trace.

        Imported lazily to avoid a cyclic import (cat.dag → cat.trace).
        Best-effort: failures are swallowed so a malformed trace never breaks
        the run.
        """
        try:
            from cat.dag import RunDAG
            dag = RunDAG.from_jsonl(self.jsonl_path)
            dag_path = self.jsonl_path.with_suffix(".dag.json")
            dag.write_json(dag_path)
        except Exception:
            pass

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
