"""Unit tests for Recorder + JSONL/parquet persistence."""
from __future__ import annotations
from pathlib import Path

from cat.trace import Recorder, TraceEvent, replay, utcnow_iso


def test_recorder_writes_jsonl(tmp_path: Path):
    rec = Recorder(run_id="r1", task_id="t1", agent="x", model="m", out_dir=tmp_path)
    rec.append(TraceEvent(event_id="e1", ts=utcnow_iso(), kind="user_message", payload={"text": "hi"}))
    rec.append(TraceEvent(event_id="e2", ts=utcnow_iso(), kind="tool_call", payload={"tool": "bash"}))
    traj = rec.finalize()
    assert traj.n_tool_calls == 1
    # JSONL file present and replayable
    events = replay(rec.jsonl_path)
    assert len(events) == 2
    assert events[0].kind == "user_message"


def test_recorder_aggregates_tokens(tmp_path: Path):
    rec = Recorder(run_id="r2", task_id="t1", agent="x", model="m", out_dir=tmp_path)
    rec.append(TraceEvent(event_id="u1", ts=utcnow_iso(), kind="llm_usage",
                          payload={"in": 100, "out": 50, "usd": 0.001}))
    rec.append(TraceEvent(event_id="u2", ts=utcnow_iso(), kind="llm_usage",
                          payload={"in": 200, "out": 30, "usd": 0.002}))
    traj = rec.finalize()
    assert traj.tokens_in == 300
    assert traj.tokens_out == 80
    assert abs(traj.usd - 0.003) < 1e-9


def test_recorder_parquet_emitted(tmp_path: Path):
    rec = Recorder(run_id="r3", task_id="t1", agent="x", model="m", out_dir=tmp_path)
    rec.append(TraceEvent(event_id="e1", ts=utcnow_iso(), kind="step_end", payload={}))
    rec.finalize()
    assert rec.parquet_path.exists()
