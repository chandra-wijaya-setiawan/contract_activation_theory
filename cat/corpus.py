"""SQLite-backed corpus of agent trajectories for the CEGAR-shaped advisor.

Stores raw (state, action, weight, outcome) samples extracted from JSONL
traces. The LIBRARIAN (cat/librarian.py) builds a PredicateTree on top of
this corpus and answers state→action queries at runtime.

Schema:

  trajectory (
    id INTEGER PK,
    trace_id TEXT,
    obligation_text TEXT,
    obligation_hash TEXT,
    outcome TEXT,            -- pass | fail | suspended
    total_tokens INTEGER,
    n_iters INTEGER,
    model_id TEXT,           -- for token-stationarity tracking
    ingested_at TEXT
  )

  sample (
    trajectory_id INTEGER REFERENCES trajectory(id),
    iter_idx INTEGER,
    raw_features TEXT,         -- JSON of the snapshot
    next_raw_features TEXT,
    tool TEXT,
    action_args TEXT,           -- canonicalized args (JSON)
    action_klass TEXT,
    weight REAL,
    durable_change INTEGER,
    on_goal_path INTEGER,
    realized_cost_to_goal REAL,
    PRIMARY KEY (trajectory_id, iter_idx)
  )

The schema is small on purpose. The PredicateTree is rebuilt from samples
each time the LIBRARIAN loads — cheap given typical corpus sizes (≤ 10K
samples).
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from cat.state_class import (
    Sample, action_class, action_intent_of, bash_command_class,
    canonical_action_args, is_state_changing, short_hash,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trajectory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT NOT NULL,
    obligation_text TEXT,
    obligation_hash TEXT,
    outcome         TEXT NOT NULL,
    total_tokens    INTEGER,
    n_iters         INTEGER,
    model_id        TEXT,
    ingested_at     TEXT NOT NULL,
    UNIQUE (trace_id)
);

CREATE TABLE IF NOT EXISTS sample (
    trajectory_id          INTEGER NOT NULL REFERENCES trajectory(id) ON DELETE CASCADE,
    iter_idx               INTEGER NOT NULL,
    raw_features           TEXT NOT NULL,
    next_raw_features      TEXT NOT NULL,
    tool                   TEXT NOT NULL,
    action_args            TEXT NOT NULL,
    action_klass           TEXT NOT NULL,
    weight                 REAL NOT NULL,
    durable_change         INTEGER NOT NULL,
    on_goal_path           INTEGER NOT NULL,
    realized_cost_to_goal  REAL,
    PRIMARY KEY (trajectory_id, iter_idx)
);

CREATE INDEX IF NOT EXISTS idx_sample_obligation
    ON sample(trajectory_id);
"""


class Corpus:
    """SQLite-backed trajectory store."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        try:
            yield conn
        finally:
            conn.close()

    # ── Ingestion ───────────────────────────────────────────────────────────

    def ingest_trace(
        self,
        jsonl_path: Path,
        reached_goal: bool,
        model_id: str = "",
    ) -> Optional[int]:
        """Walk a trace JSONL → samples → DB. Returns trajectory_id or None
        if the trace was already ingested (uniqueness on trace_id)."""
        from cat.state_class import _FeatureState, _lookup_tool_call

        jsonl_path = Path(jsonl_path)
        events = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
        if not events:
            return None

        # Extract obligation + plan
        obligation_text = ""
        for ev in events:
            if ev.get("kind") == "contract_state" and ev.get("payload", {}).get("obligation"):
                obligation_text = ev["payload"]["obligation"]
                break
        obligation_hash = short_hash(obligation_text)

        # Walk to build Sample list (single-pass with FeatureState)
        samples: list[Sample] = []
        fs = _FeatureState(obligation_hash=obligation_hash)
        trace_id = jsonl_path.parent.parent.name

        current = None
        total_tokens = 0

        for ev in events:
            k = ev.get("kind")
            p = ev.get("payload", {}) or {}

            if k == "capability_perform":
                if current is not None:
                    samples.append(self._finalize_sample(current, fs.snapshot(), trace_id, reached_goal))
                current = {"iter": len(samples) + 1, "raw": fs.snapshot(),
                           "tool": None, "args": None, "error": None,
                           "tokens": 0, "durable": False}

            elif k == "tool_call" and current is not None:
                tc = p.get("tool_call_id", "")
                if tc.startswith(("rep-", "oracle-")):
                    continue
                current["tool"] = p.get("tool")
                current["args"] = p.get("args", {}) or {}

            elif k == "tool_result" and current is not None:
                tc = p.get("tool_call_id", "")
                if tc.startswith(("rep-", "oracle-")):
                    continue
                err = bool(p.get("is_error"))
                current["error"] = err
                if current["tool"]:
                    fs.apply_tool_event(current["tool"], current["args"] or {}, err)
                    if is_state_changing(current["tool"], current["args"] or {}, err):
                        current["durable"] = True

            elif k == "criterion_check" and current is not None:
                if p.get("ok") is True and p.get("step_id"):
                    fs.apply_criterion_ok(p["step_id"])

            elif k == "llm_usage" and current is not None:
                tin = int(p.get("in", 0))
                current["tokens"] += tin
                total_tokens += tin

        if current is not None:
            samples.append(self._finalize_sample(current, fs.snapshot(), trace_id, reached_goal))

        # Compute realized_cost_to_goal (forward-suffix sum) for PASS trajectories
        if reached_goal and samples:
            suffix = 0.0
            for s in reversed(samples):
                suffix += s.weight
                s.realized_cost_to_goal = suffix

        outcome = "pass" if reached_goal else "fail"

        return self._persist(
            trace_id=trace_id,
            obligation_text=obligation_text,
            obligation_hash=obligation_hash,
            outcome=outcome,
            total_tokens=total_tokens,
            n_iters=len(samples),
            model_id=model_id,
            samples=samples,
        )

    def _finalize_sample(self, buf: dict, next_raw: dict, trace_id: str, reached_goal: bool) -> Sample:
        return Sample(
            trace_id=trace_id,
            iter_idx=buf["iter"],
            raw_features=buf["raw"],
            tool=buf["tool"] or "",
            action_args=canonical_action_args(buf["tool"] or "", buf["args"] or {}),
            action_klass=action_class(buf["tool"] or "", buf["args"] or {}),
            weight=float(buf["tokens"]),
            durable_change=buf["durable"],
            next_raw_features=next_raw,
            reached_goal=reached_goal,
            on_goal_path=reached_goal,
        )

    def _persist(self, trace_id, obligation_text, obligation_hash, outcome,
                 total_tokens, n_iters, model_id, samples) -> Optional[int]:
        with self._conn() as conn:
            # Try INSERT; if trace_id already exists, skip
            cur = conn.execute(
                "INSERT OR IGNORE INTO trajectory "
                "(trace_id, obligation_text, obligation_hash, outcome, total_tokens, n_iters, model_id, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trace_id, obligation_text, obligation_hash, outcome,
                 total_tokens, n_iters, model_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            if cur.rowcount == 0:
                return None  # already ingested
            traj_id = cur.lastrowid
            for s in samples:
                conn.execute(
                    "INSERT INTO sample "
                    "(trajectory_id, iter_idx, raw_features, next_raw_features, "
                    " tool, action_args, action_klass, weight, durable_change, "
                    " on_goal_path, realized_cost_to_goal) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (traj_id, s.iter_idx,
                     json.dumps(s.raw_features, default=str),
                     json.dumps(s.next_raw_features, default=str),
                     s.tool,
                     json.dumps(s.action_args, default=str),
                     s.action_klass, s.weight,
                     int(s.durable_change), int(s.on_goal_path),
                     None if s.realized_cost_to_goal == math.inf else s.realized_cost_to_goal),
                )
            return traj_id

    # ── Queries ─────────────────────────────────────────────────────────────

    def load_all_samples(self) -> list[Sample]:
        """Materialize every sample as a Sample dataclass."""
        out: list[Sample] = []
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT t.trace_id, s.iter_idx, s.raw_features, s.next_raw_features, "
                "       s.tool, s.action_args, s.action_klass, s.weight, "
                "       s.durable_change, s.on_goal_path, s.realized_cost_to_goal "
                "FROM sample s JOIN trajectory t ON s.trajectory_id = t.id"
            )
            for row in cur:
                (trace_id, iter_idx, raw_j, next_raw_j, tool, args_j, klass,
                 weight, durable, on_goal, rcg) = row
                out.append(Sample(
                    trace_id=trace_id,
                    iter_idx=iter_idx,
                    raw_features=json.loads(raw_j),
                    tool=tool,
                    action_args=json.loads(args_j),
                    action_klass=klass,
                    weight=weight,
                    durable_change=bool(durable),
                    next_raw_features=json.loads(next_raw_j),
                    reached_goal=bool(on_goal),
                    on_goal_path=bool(on_goal),
                    realized_cost_to_goal=rcg if rcg is not None else math.inf,
                ))
        return out

    def stats(self) -> dict:
        with self._conn() as conn:
            n_traj = conn.execute("SELECT COUNT(*) FROM trajectory").fetchone()[0]
            n_pass = conn.execute("SELECT COUNT(*) FROM trajectory WHERE outcome='pass'").fetchone()[0]
            n_samples = conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
            return {"n_trajectories": n_traj, "n_pass": n_pass, "n_samples": n_samples}
