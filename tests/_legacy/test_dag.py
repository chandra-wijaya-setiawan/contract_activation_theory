"""Unit tests for cat/dag.py — RunDAG from a trace JSONL."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from cat.activator import Activator
from cat.capabilities import build_default_registry
from cat.contracts import (
    Contract,
    ContractState,
    State,
    deterministic_criterion,
)
from cat.dag import RunDAG
from cat.trace import Recorder

# Reuse the ScriptedLLM mock from the activator tests
from tests.test_activator import ScriptedLLM, _ScriptedTurn


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    return Recorder(
        run_id="r1", task_id="t1",
        agent="test", model="scripted/mock",
        out_dir=tmp_path / "out",
    )


def _run_simple_trace(workdir, recorder) -> Path:
    crit = deterministic_criterion(
        id="exists",
        description="hello.txt exists",
        predicate=lambda s: (s.workdir / "hello.txt").exists(),
    )
    contract = Contract.new(obligation="make hello.txt", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["create hello"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": "hello.txt", "content": "x"}}]),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    recorder.finalize()
    return recorder.jsonl_path


def test_dag_from_jsonl_acyclic(workdir, recorder):
    jsonl = _run_simple_trace(workdir, recorder)
    dag = RunDAG.from_jsonl(jsonl)
    assert len(dag.nodes) > 0
    assert dag.is_acyclic()


def test_dag_has_exactly_one_root(workdir, recorder):
    jsonl = _run_simple_trace(workdir, recorder)
    dag = RunDAG.from_jsonl(jsonl)
    # CAT runs have a single state-init root
    assert len(dag.roots) == 1, f"expected 1 root, got {dag.roots}"


def test_dag_longest_chain_includes_terminal(workdir, recorder):
    jsonl = _run_simple_trace(workdir, recorder)
    dag = RunDAG.from_jsonl(jsonl)
    chain = dag.longest_chain()
    # The longest path should pass through REPRESENT → DECOMPOSE → ADMIT →
    # at least one invocation. >= 5 nodes is conservative.
    assert len(chain) >= 5, f"chain too short: {[n.kind for n in chain]}"
    # First node should be the root (state-init)
    assert chain[0].is_root
    # Some node in the chain is the contract_admit (proves admission was on the path)
    kinds = {n.kind for n in chain}
    assert "contract_admit" in kinds


def test_dag_round_trips_to_json(workdir, recorder, tmp_path):
    jsonl = _run_simple_trace(workdir, recorder)
    dag = RunDAG.from_jsonl(jsonl)
    dag_path = tmp_path / "dag.json"
    dag.write_json(dag_path)

    # Reload as plain JSON and verify shape
    data = json.loads(dag_path.read_text())
    assert data["version"] == 1
    assert data["n_nodes"] == len(dag.nodes)
    assert data["roots"] == dag.roots
    # Every node entry must reference a parent that exists in the dump
    all_ids = {n["event_id"] for n in data["nodes"]}
    for n in data["nodes"]:
        if n["parent_id"] is not None:
            assert n["parent_id"] in all_ids


def test_dag_auto_emitted_next_to_trace(workdir, recorder):
    jsonl = _run_simple_trace(workdir, recorder)
    expected_dag = jsonl.with_suffix(".dag.json")
    assert expected_dag.exists(), f"dag.json not auto-emitted at {expected_dag}"


def test_dag_layers_present(workdir, recorder):
    """Every CAT layer should be represented in a normal trace."""
    jsonl = _run_simple_trace(workdir, recorder)
    dag = RunDAG.from_jsonl(jsonl)
    layers = {n.layer for n in dag.nodes.values()}
    # Expect at least these 4 to appear (post-1.6.2.9 rename)
    for layer in ("represent", "stipulate", "activate", "perform"):
        assert layer in layers, f"missing layer {layer}; got {layers}"
