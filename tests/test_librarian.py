"""Tests for cat.corpus + cat.librarian (1.7 Phase A.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cat.corpus import Corpus
from cat.librarian import Librarian


def _write_trace(trial_dir: Path, events: list[dict]):
    logs = trial_dir / "agent-logs"
    logs.mkdir(parents=True, exist_ok=True)
    jsonl = logs / "test-trace.jsonl"
    with jsonl.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return jsonl


def _basic_events(obligation: str):
    """A minimal trace: obligation → represent → stipulate → admit →
    active → perform(read) → result(ok) → perform(str_replace) → result(ok)
    → criterion_check ok → state fulfilled."""
    return [
        {"event_id": "s0", "parent_id": None, "ts": "t0", "kind": "contract_state",
         "payload": {"to": "latent", "obligation": obligation}},
        {"event_id": "rep", "parent_id": "s0", "ts": "t1", "kind": "contract_represent",
         "payload": {"representations": []}},
        {"event_id": "stip", "parent_id": "rep", "ts": "t2", "kind": "contract_stipulate",
         "payload": {"steps": [{"id": "s1", "description": "do thing"}]}},
        {"event_id": "adm", "parent_id": "stip", "ts": "t3", "kind": "contract_admit",
         "payload": {"ok": True}},
        {"event_id": "st1", "parent_id": "adm", "ts": "t4", "kind": "contract_state",
         "payload": {"to": "active"}},
        # iter 1: read
        {"event_id": "p1", "parent_id": "st1", "ts": "t5", "kind": "capability_perform",
         "payload": {"tool": "read"}},
        {"event_id": "tc1", "parent_id": "p1", "ts": "t6", "kind": "tool_call",
         "payload": {"tool_call_id": "tc1", "tool": "read", "args": {"path": "/testbed/file.py"}}},
        {"event_id": "tr1", "parent_id": "tc1", "ts": "t7", "kind": "tool_result",
         "payload": {"tool_call_id": "tc1", "is_error": False, "output": "lines"}},
        {"event_id": "lu1", "parent_id": "tr1", "ts": "t8", "kind": "llm_usage",
         "payload": {"role": "performer", "in": 100, "out": 20}},
        # iter 2: str_replace (durable)
        {"event_id": "p2", "parent_id": "lu1", "ts": "t9", "kind": "capability_perform",
         "payload": {"tool": "str_replace"}},
        {"event_id": "tc2", "parent_id": "p2", "ts": "t10", "kind": "tool_call",
         "payload": {"tool_call_id": "tc2", "tool": "str_replace",
                     "args": {"path": "/testbed/file.py", "old_str": "x", "new_str": "y"}}},
        {"event_id": "tr2", "parent_id": "tc2", "ts": "t11", "kind": "tool_result",
         "payload": {"tool_call_id": "tc2", "is_error": False, "output": "OK"}},
        {"event_id": "lu2", "parent_id": "tr2", "ts": "t12", "kind": "llm_usage",
         "payload": {"role": "performer", "in": 200, "out": 50}},
        {"event_id": "cc1", "parent_id": "lu2", "ts": "t13", "kind": "criterion_check",
         "payload": {"step_id": "s1", "ok": True}},
        {"event_id": "st2", "parent_id": "cc1", "ts": "t14", "kind": "contract_state",
         "payload": {"to": "fulfilled"}},
    ]


def test_corpus_init_creates_schema(tmp_path: Path):
    db = tmp_path / "corpus.db"
    corpus = Corpus(db)
    assert db.exists()
    stats = corpus.stats()
    assert stats == {"n_trajectories": 0, "n_pass": 0, "n_samples": 0}


def test_corpus_ingests_pass_trace(tmp_path: Path):
    db = tmp_path / "corpus.db"
    trial_dir = tmp_path / "task-abc" / "trial-1"
    jsonl = _write_trace(trial_dir, _basic_events("fix the thing"))
    corpus = Corpus(db)
    traj_id = corpus.ingest_trace(jsonl, reached_goal=True)
    assert traj_id is not None
    stats = corpus.stats()
    assert stats["n_trajectories"] == 1
    assert stats["n_pass"] == 1
    assert stats["n_samples"] == 2  # iter 1 (read) + iter 2 (str_replace)


def test_corpus_ignores_duplicate_traces(tmp_path: Path):
    db = tmp_path / "corpus.db"
    trial_dir = tmp_path / "task-abc" / "trial-1"
    jsonl = _write_trace(trial_dir, _basic_events("fix the thing"))
    corpus = Corpus(db)
    first = corpus.ingest_trace(jsonl, reached_goal=True)
    second = corpus.ingest_trace(jsonl, reached_goal=True)
    assert first is not None
    assert second is None  # duplicate
    assert corpus.stats()["n_trajectories"] == 1


def test_librarian_returns_none_on_empty_corpus(tmp_path: Path):
    db = tmp_path / "corpus.db"
    lib = Librarian(db)
    lib.refresh()
    rec = lib.recommend({"obligation_hash": "abc", "plan_idx": 0})
    assert rec is None


def test_librarian_recommends_when_corpus_has_matching_state(tmp_path: Path):
    """End-to-end: seed two passing traces of the same obligation; query the
    initial state; expect a non-None recommendation."""
    db = tmp_path / "corpus.db"
    corpus = Corpus(db)
    obligation = "fix the thing in models.py"
    # Seed two PASS traces for the same task
    for i in range(2):
        trial = tmp_path / "task-abc" / f"trial-{i}"
        jsonl = _write_trace(trial, _basic_events(obligation))
        # Re-rename trace_id by moving the trial; the trace_id is taken from
        # parent.parent.name (i.e. task dir); each iteration uses a fresh
        # parent dir so each is "unique" as trace_id
        new_task = tmp_path / f"task-abc-{i}" / f"trial-{i}"
        new_task.mkdir(parents=True, exist_ok=True)
        for f in (trial / "agent-logs").iterdir():
            f.rename(new_task / f.name if not (new_task / "agent-logs").exists()
                     else (new_task / "agent-logs" / f.name))
        # easiest: just write a fresh trace and ingest it
        logs2 = new_task / "agent-logs"
        logs2.mkdir(parents=True, exist_ok=True)
        jsonl2 = logs2 / "trace.jsonl"
        with jsonl2.open("w") as f:
            for ev in _basic_events(obligation):
                f.write(json.dumps(ev) + "\n")
        corpus.ingest_trace(jsonl2, reached_goal=True)

    lib = Librarian(db)
    lib.refresh()
    # Query the INITIAL state (matches sample iter 1 from each trace)
    from cat.state_class import short_hash
    raw = {
        "obligation_hash": short_hash(obligation),
        "plan_idx": 0,
        "last_outcome": None,
        "last_action_intent": None,
        "durable_change_count": 0,
        "iter_since_durable_change": 0,
        "verify_attempt_count": 0,
        "investigate_attempt_count": 0,
        "mutate_attempt_count": 0,
        "n_unique_actions": 0,
        "n_unique_errors": 0,
        "attempted_actions": [],
        "env_facts": [],
        "last_tool": None,
        "last_bash_class": None,
        "attempted_bash_classes": [],
    }
    rec = lib.recommend(raw)
    assert rec is not None
    assert rec.tool in ("read", "str_replace")  # one of the actions seen
    assert rec.confidence_n >= 1
    assert rec.format_hint()  # produces non-empty string


def test_librarian_recommend_format_hint_is_human_readable(tmp_path: Path):
    db = tmp_path / "corpus.db"
    corpus = Corpus(db)
    new_task = tmp_path / "task" / "trial"
    new_task.mkdir(parents=True, exist_ok=True)
    logs = new_task / "agent-logs"
    logs.mkdir(parents=True, exist_ok=True)
    jsonl = logs / "trace.jsonl"
    with jsonl.open("w") as f:
        for ev in _basic_events("oblig"):
            f.write(json.dumps(ev) + "\n")
    corpus.ingest_trace(jsonl, reached_goal=True)
    lib = Librarian(db)
    lib.refresh()
    from cat.state_class import short_hash
    raw = {"obligation_hash": short_hash("oblig"), "plan_idx": 0,
           "last_outcome": None, "last_action_intent": None,
           "durable_change_count": 0, "iter_since_durable_change": 0,
           "verify_attempt_count": 0, "investigate_attempt_count": 0,
           "mutate_attempt_count": 0, "n_unique_actions": 0,
           "n_unique_errors": 0, "attempted_actions": [], "env_facts": [],
           "last_tool": None, "last_bash_class": None,
           "attempted_bash_classes": []}
    rec = lib.recommend(raw)
    if rec is not None:
        h = rec.format_hint()
        assert "prior runs at this state suggest" in h
        assert "expected cost" in h
        assert "n=" in h
