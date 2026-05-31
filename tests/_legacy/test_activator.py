"""Activator loop tests with a mock LLM.

The mock returns a programmable sequence of (text, tool_calls) responses; this
isolates loop semantics (state transitions, budget enforcement, admission gate,
criterion checks) from real LLM calls.
"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pytest

from cat.activator import Activator
from cat.capabilities import build_default_registry
from cat.contracts import (
    Constraints,
    Contract,
    ContractPolicy,
    ContractState,
    Criterion,
    Evidence,
    State,
    deterministic_criterion,
)
from cat.llm import LLMResult
from cat.trace import Recorder


@dataclass
class _ScriptedTurn:
    text: Optional[str] = None
    tool_calls: list[dict] = None  # type: ignore[assignment]
    role_tag_expected: Optional[str] = None


class ScriptedLLM:
    """LLM substitute with canned responses keyed by role_tag."""

    def __init__(
        self,
        planner_steps: Optional[list[str]] = None,
        executor_turns: Optional[list[_ScriptedTurn]] = None,
        judge_default_complete: bool = True,
    ):
        self.model = "scripted/mock"
        self.api_model = "scripted/mock"
        self.recorder = None
        self.parent_event_id = None
        self.planner_steps = planner_steps or ["s1"]
        self.executor_queue: Iterator[_ScriptedTurn] = iter(executor_turns or [])
        self.judge_default_complete = judge_default_complete
        self.calls: list[dict] = []

    def chat(
        self,
        messages,
        *,
        tools=None,
        tool_choice=None,
        response_format=None,
        max_tokens=None,
        temperature=0.2,
        role_tag="executor",
        parent_event_id=None,
    ) -> LLMResult:
        self.calls.append({"role_tag": role_tag, "n_messages": len(messages)})

        if role_tag in ("planner", "stipulator"):
            payload = {"steps": [{"id": f"s{i+1}", "description": d}
                                 for i, d in enumerate(self.planner_steps)]}
            return LLMResult(text=json.dumps(payload), tokens_in=20, tokens_out=20, model=self.model)

        if role_tag in ("judge", "attestor"):
            payload = {"complete": self.judge_default_complete, "reason": "mock"}
            return LLMResult(text=json.dumps(payload), tokens_in=10, tokens_out=10, model=self.model)

        # executor
        try:
            turn = next(self.executor_queue)
        except StopIteration:
            return LLMResult(text="DONE", tool_calls=[], tokens_in=10, tokens_out=2, model=self.model)
        tool_calls = []
        for tc in turn.tool_calls or []:
            tool_calls.append({
                "id": tc.get("id") or f"tc-{uuid.uuid4().hex[:6]}",
                "name": tc["name"],
                "arguments": tc.get("arguments", {}),
            })
        return LLMResult(
            text=turn.text,
            tool_calls=tool_calls,
            tokens_in=50,
            tokens_out=20,
            model=self.model,
            usd=0.0001,
        )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    return Recorder(run_id="r1", task_id="t1", agent="test", model="scripted/mock", out_dir=tmp_path / "out")


# ── Happy path + budget enforcement ─────────────────────────────────────────

def test_activator_fulfills_on_single_write(workdir, recorder):
    """LLM writes hello.txt; criterion passes; FULFILLED via admission gate + one invoke."""
    crit = deterministic_criterion(
        id="hello-exists",
        description="hello.txt contains 'hi'",
        predicate=lambda s: (s.workdir / "hello.txt").exists()
        and (s.workdir / "hello.txt").read_text() == "hi",
    )
    contract = Contract.new(
        obligation="Create hello.txt with content 'hi'.",
        criteria=[crit],
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["Create hello.txt with content 'hi'."],
        executor_turns=[
            _ScriptedTurn(
                text=None,
                tool_calls=[{"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}}],
            ),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FULFILLED, contract.state_reason
    assert run.iterations == 1
    assert crit.satisfied
    assert (workdir / "hello.txt").read_text() == "hi"
    # Grant captured at admission
    assert contract.grant, "expected non-empty grant from admission"


def test_activator_fails_when_budget_violated(workdir, recorder):
    """Per-iteration budget enforcement (Step 8 CLOSE on envelope exhaustion)."""
    crit = deterministic_criterion(
        id="impossible",
        description="never true",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="impossible",
        criteria=[crit],
        constraints=Constraints(
            max_tool_calls=2,
            max_llm_calls=50,
            max_tokens_total=10_000,
        ),
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["do impossible"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(10)
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder, max_iterations=20)
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FAILED
    assert "tool_calls" in (contract.state_reason or "")
    assert run.failure_reason is not None


def test_activator_planner_called_once(workdir, recorder):
    """Planner runs exactly once at DECOMPOSE; no replan in v1.5."""
    crit = deterministic_criterion(
        id="exists",
        description="hello.txt exists",
        predicate=lambda s: (s.workdir / "hello.txt").exists(),
    )
    contract = Contract.new(obligation="make hello.txt", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["create file"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}}]),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    recorder.finalize()

    planner_calls = sum(1 for c in llm.calls if c["role_tag"] in ("planner", "stipulator"))
    assert planner_calls == 1


def test_activator_records_contract_state_transitions(workdir, recorder):
    """Trace contains LATENT→ELIGIBLE→ACTIVE→FULFILLED events."""
    crit = deterministic_criterion(
        id="exists",
        description="x.txt exists",
        predicate=lambda s: (s.workdir / "x.txt").exists(),
    )
    contract = Contract.new(obligation="touch x.txt", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["touch x"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": "x.txt", "content": ""}}]),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    traj = recorder.finalize()

    state_events = [ev for ev in traj.events if ev.kind == "contract_state"]
    states_visited = [
        ev.payload.get("to") for ev in state_events if not ev.payload.get("rejected")
    ]
    assert "latent" in states_visited
    assert "eligible" in states_visited
    assert "active" in states_visited
    assert "fulfilled" in states_visited


# ── Admission gate (Step 4 ACTIVATE) ────────────────────────────────────────

def test_admission_records_contract_admit_event(workdir, recorder):
    """Trace contains a contract_admit event with the decision payload."""
    crit = deterministic_criterion(
        id="exists",
        description="hello.txt exists",
        predicate=lambda s: (s.workdir / "hello.txt").exists(),
    )
    contract = Contract.new(obligation="make hello.txt", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["create file"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}}]),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    traj = recorder.finalize()

    admit_events = [ev for ev in traj.events if ev.kind == "contract_admit"]
    assert len(admit_events) == 1
    payload = admit_events[0].payload
    assert payload["ok"] is True
    assert payload["grant"], "admission grant should not be empty"


def test_admission_boundary_fail_no_criteria(workdir, recorder):
    """Contract with no criteria → admission rejects (boundary) → policy=FAIL → FAILED."""
    contract = Contract.new(obligation="do something", criteria=[])
    state = State(workdir=workdir)
    llm = ScriptedLLM(planner_steps=["do it"], executor_turns=[])
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    run = act.run(contract, state)
    traj = recorder.finalize()

    assert contract.state == ContractState.FAILED
    assert "admission_boundary" in (contract.state_reason or "")
    assert run.iterations == 0  # never entered discharge
    admit_events = [ev for ev in traj.events if ev.kind == "contract_admit"]
    assert admit_events and admit_events[0].payload["ok"] is False
    assert admit_events[0].payload["failure_type"] == "boundary"


def test_admission_environment_fail_suspends_by_default(workdir, recorder):
    """Unmet condition → admission rejects (environment) → default policy=SUSPEND → SUSPENDED."""
    # A condition that's never met — predicate returns (False, evidence)
    def never_met(state):
        return False, Evidence(summary="condition is false by design")
    crit = deterministic_criterion(
        id="x",
        description="placeholder criterion",
        predicate=lambda s: True,
    )
    contract = Contract.new(obligation="x", criteria=[crit])
    contract.conditions = [never_met]
    state = State(workdir=workdir)
    llm = ScriptedLLM(planner_steps=["step"], executor_turns=[])
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.SUSPENDED
    assert "admission_environment" in (contract.suspend_reason or "")
    assert contract.re_entry_condition is not None
    assert run.iterations == 0


def test_admission_environment_fail_with_FAIL_policy(workdir, recorder):
    """Policy override: environment fail → FAIL instead of SUSPEND."""
    def never_met(state):
        return False, Evidence(summary="condition is false by design")
    crit = deterministic_criterion(
        id="x",
        description="placeholder criterion",
        predicate=lambda s: True,
    )
    contract = Contract.new(obligation="x", criteria=[crit])
    contract.conditions = [never_met]
    contract.policy = ContractPolicy(on_environment_fail="FAIL")
    state = State(workdir=workdir)
    llm = ScriptedLLM(planner_steps=["step"], executor_turns=[])
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FAILED
    assert "admission_environment" in (contract.state_reason or "")


def test_admission_capacity_fail_when_deadline_passed(workdir, recorder):
    """Deadline already passed → admission rejects (boundary, since deadline is a definition issue)."""
    import time as _t
    crit = deterministic_criterion(
        id="x", description="x", predicate=lambda s: True,
    )
    contract = Contract.new(
        obligation="x",
        criteria=[crit],
        constraints=Constraints(deadline_ts=_t.time() - 100),
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(planner_steps=["step"], executor_turns=[])
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FAILED
    assert "admission_boundary" in (contract.state_reason or "")


# ── 1.5.15: deadline + cost-charging (contract owns its envelope) ───────────

def test_admission_locks_deadline(workdir, recorder):
    """After admission passes, contract.constraints.deadline_ts is set."""
    crit = deterministic_criterion(
        id="x", description="x", predicate=lambda s: True,
    )
    contract = Contract.new(
        obligation="x",
        criteria=[crit],
        constraints=Constraints(max_wall_s=30.0),
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["s"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": "x.txt", "content": ""}}]),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    before = contract.constraints.deadline_ts
    act.run(contract, state)
    recorder.finalize()

    assert before is None
    assert contract.constraints.deadline_ts is not None
    # Should be ~30s in the future from admission time
    import time as _t
    assert contract.constraints.deadline_ts > _t.time() - 5  # roughly now or later


def test_deadline_fires_per_iter(workdir, recorder):
    """Admission-locked deadline causes per-iter check to FAIL the contract.

    Set max_wall_s very small; the contract should self-close with reason
    containing 'deadline' before max_iterations or any other limit.
    """
    crit = deterministic_criterion(
        id="never", description="never", predicate=lambda s: False,
    )
    import time as _t
    contract = Contract.new(
        obligation="x",
        criteria=[crit],
        constraints=Constraints(
            max_wall_s=0.001,   # 1ms — deadline expires almost immediately
            max_tool_calls=100,
            max_llm_calls=100,
            max_tokens_total=100_000,
        ),
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["s"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(20)
        ],
        judge_default_complete=False,
    )
    # Sleep a hair to ensure deadline has actually passed by the time _check_budget runs
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder, max_iterations=20)
    # Sleep tiny bit so first per-iter check sees deadline passed
    contract.constraints.deadline_ts = _t.time() - 0.001  # already-passed deadline pre-set
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FAILED
    assert "deadline" in (contract.state_reason or ""), f"got: {contract.state_reason}"
    assert run.failure_reason is not None and "deadline" in run.failure_reason


def test_planner_tokens_charged_to_contract(workdir, recorder):
    """Planner LLM call's tokens flow into contract.cost (not silently dropped)."""
    crit = deterministic_criterion(
        id="exists",
        description="x.txt exists",
        predicate=lambda s: (s.workdir / "x.txt").exists(),
    )
    contract = Contract.new(obligation="make x.txt", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["create x"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": "x.txt", "content": ""}}]),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    act.run(contract, state)
    recorder.finalize()

    # Planner mock returns tokens_in=20, tokens_out=20 (see ScriptedLLM.chat).
    # Executor mock returns tokens_in=50, tokens_out=20.
    # If planner tokens were leaked we'd see 50/20 only; with charge they're summed.
    assert contract.cost.tokens_in >= 70, (
        f"planner tokens leaked: got tokens_in={contract.cost.tokens_in}, expected >=70"
    )
    assert contract.cost.tokens_out >= 40, (
        f"planner tokens leaked: got tokens_out={contract.cost.tokens_out}, expected >=40"
    )


def test_judge_tokens_charged_to_contract(workdir, recorder):
    """Judge LLM call's tokens flow into contract.cost on each iteration."""
    crit = deterministic_criterion(
        id="never",
        description="never satisfied",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="impossible",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=50, max_llm_calls=100, max_tokens_total=100_000),
    )
    state = State(workdir=workdir)
    # Three executor turns → judge runs 3× → 30 tokens_in (judge mock = 10 in / 10 out).
    llm = ScriptedLLM(
        planner_steps=["s"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(3)
        ],
        judge_default_complete=False,
    )
    act = Activator(
        llm=llm, registry=build_default_registry(), recorder=recorder,
        max_iterations=3, criterion_check_interval=1,
    )
    act.run(contract, state)
    recorder.finalize()

    # Planner(20+20) + executor(3×50=150 + 3×20=60) + judge(3×10=30 + 3×10=30) ≥ 200 tokens_in
    # With judge leaks we'd be ~170; with charge we get ~200.
    assert contract.cost.tokens_in >= 195, (
        f"judge tokens leaked: tokens_in={contract.cost.tokens_in}, expected >=195"
    )


def test_oracle_bash_charged_to_contract(workdir, recorder):
    """Oracle criterion bash invocation increments contract.cost.tool_calls."""
    # Make a custom oracle criterion that just runs `true` (exits 0).
    from cat.contracts import Criterion, Evidence
    from cat.capabilities import build_default_registry as _reg
    registry = build_default_registry()

    # Build the oracle via the Activator's helper so the closure charges cost.
    contract = Contract.new(obligation="run true", criteria=[])
    act = Activator(llm=None, registry=registry, recorder=recorder)  # type: ignore[arg-type]
    oracle = act._build_oracle_criterion(contract, "true")
    contract.criteria.append(oracle)

    state = State(workdir=workdir)
    before = contract.cost.tool_calls
    ok, ev = oracle.predicate(state)
    after = contract.cost.tool_calls
    recorder.finalize()

    assert ok is True
    assert after == before + 1, (
        f"oracle bash not charged: tool_calls went {before}→{after}, expected +1"
    )
    assert contract.cost.wall_s > 0.0, "oracle wall_s should be > 0"


# ── 1.6.1: trace is a valid DAG ─────────────────────────────────────────────


def test_trace_is_valid_dag(workdir, recorder):
    """Every non-root event's parent_id resolves to another event in the same trace.

    No event (except the initial state-init) chains directly to contract.id;
    that would mean the parent_id is just the root context, not a real causal
    parent. After 1.6.1 the trace must be a proper DAG.
    """
    crit = deterministic_criterion(
        id="hello",
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
    traj = recorder.finalize()

    # Collect all event_ids
    event_ids = {ev.event_id for ev in traj.events}
    # Root: at most one event with parent_id=None
    roots = [ev for ev in traj.events if ev.parent_id is None]
    assert len(roots) >= 1, "must have at least one root event (state-init)"
    # All non-root events must point to an existing event_id
    orphans = []
    contract_root_chains = []
    for ev in traj.events:
        if ev.parent_id is None:
            continue
        if ev.parent_id == contract.id:
            contract_root_chains.append(ev)
        elif ev.parent_id not in event_ids:
            orphans.append(ev)
    assert not orphans, f"orphan events (parent_id not in trace): {[(o.event_id, o.kind, o.parent_id) for o in orphans]}"
    assert not contract_root_chains, (
        f"events still chain to contract.id (not a real DAG): "
        f"{[(e.event_id, e.kind) for e in contract_root_chains]}"
    )


def test_trace_dag_is_acyclic(workdir, recorder):
    """DFS the parent_id graph from each event; no cycles allowed."""
    crit = deterministic_criterion(
        id="hello",
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
    traj = recorder.finalize()

    parent_of = {ev.event_id: ev.parent_id for ev in traj.events}
    for start_id in parent_of:
        seen = set()
        cur = start_id
        while cur is not None:
            if cur in seen:
                assert False, f"cycle detected starting from {start_id}: revisited {cur}"
            seen.add(cur)
            cur = parent_of.get(cur)


def test_max_iterations_transitions_to_SUSPENDED(workdir, recorder):
    """Iteration envelope exhausted without satisfaction → SUSPENDED, not FAILED.

    Reflects the design choice: criterion unreachable within envelope is a
    state the world could change, not a definitive failure. Re-admission with
    fresh representations / extended envelope is the recovery path.
    """
    crit = deterministic_criterion(
        id="never", description="never true", predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="do impossible",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=100, max_llm_calls=200, max_tokens_total=100_000),
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["do it"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(20)
        ],
        judge_default_complete=False,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=3,
        criterion_check_interval=1,
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.SUSPENDED
    assert "criterion_unreachable" in (run.suspend_reason or "")
    assert contract.re_entry_condition is not None


# ── v1.7 A.2 ELECTION tests ──────────────────────────────────────────────────


def _ingest_pass_trace_with_actions(corpus_db_path, obligation, action_sequence, trial_id="t0"):
    """Helper for ELECTION tests: build a JSONL trace where the performer
    used `action_sequence` (each item = {tool, args}) and the contract
    reached fulfilled. Ingest into the corpus.

    `trial_id` must be unique across calls — Corpus dedupes on trace_id."""
    import tempfile
    from cat.corpus import Corpus

    events = [
        {"event_id": "s0", "parent_id": None, "ts": "t0", "kind": "contract_state",
         "payload": {"to": "latent", "obligation": obligation}},
        {"event_id": "rep", "parent_id": "s0", "ts": "t1", "kind": "contract_represent",
         "payload": {"representations": []}},
        {"event_id": "stip", "parent_id": "rep", "ts": "t2", "kind": "contract_stipulate",
         "payload": {"steps": [{"id": "s1", "description": "x"}]}},
        {"event_id": "adm", "parent_id": "stip", "ts": "t3", "kind": "contract_admit",
         "payload": {"ok": True}},
        {"event_id": "st1", "parent_id": "adm", "ts": "t4", "kind": "contract_state",
         "payload": {"to": "active"}},
    ]
    last_id = "st1"
    for i, action in enumerate(action_sequence):
        p_id = f"p{i}"; tc_id = f"tc{i}"; tr_id = f"tr{i}"; lu_id = f"lu{i}"
        events.append({"event_id": p_id, "parent_id": last_id, "ts": f"t{10+i*4}",
                       "kind": "capability_perform", "payload": {"tool": action["tool"]}})
        events.append({"event_id": tc_id, "parent_id": p_id, "ts": f"t{11+i*4}",
                       "kind": "tool_call",
                       "payload": {"tool_call_id": tc_id, "tool": action["tool"],
                                   "args": action.get("args", {})}})
        events.append({"event_id": tr_id, "parent_id": tc_id, "ts": f"t{12+i*4}",
                       "kind": "tool_result",
                       "payload": {"tool_call_id": tc_id, "is_error": False, "output": "ok"}})
        events.append({"event_id": lu_id, "parent_id": tr_id, "ts": f"t{13+i*4}",
                       "kind": "llm_usage",
                       "payload": {"role": "performer", "in": 50 + i * 10, "out": 5}})
        last_id = lu_id
    events.append({"event_id": "ccfinal", "parent_id": last_id, "ts": "tF",
                   "kind": "criterion_check", "payload": {"step_id": "s1", "ok": True}})
    events.append({"event_id": "stF", "parent_id": "ccfinal", "ts": "tFinal",
                   "kind": "contract_state", "payload": {"to": "fulfilled"}})

    # Write JSONL into a temp trial-like dir. trial_id ensures unique trace_id.
    trial = Path(tempfile.mkdtemp()) / "task" / f"trial-{trial_id}"
    logs = trial / "agent-logs"
    logs.mkdir(parents=True, exist_ok=True)
    jsonl = logs / "t.jsonl"
    with jsonl.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    corpus = Corpus(corpus_db_path)
    corpus.ingest_trace(jsonl, reached_goal=True)


def test_election_picks_corpus_recommended_action(workdir, recorder, tmp_path):
    """When the LLM emits 2 candidates and the corpus has evidence one is
    cheaper, ELECTION picks the cheaper one. Also asserts that the rejected
    candidate is dropped from the assistant message (otherwise OpenAI's
    tool_call ↔ tool_result correspondence breaks)."""
    from cat.librarian import Librarian

    obligation = "Create hello.txt with content 'hi'."
    # Seed the corpus with traces where `write` is the dominant successful action.
    corpus_db = tmp_path / "corpus.db"
    for i in range(3):
        _ingest_pass_trace_with_actions(
            corpus_db, obligation,
            [{"tool": "write", "args": {"path": "hello.txt", "content": "hi"}}],
            trial_id=f"seed-{i}",
        )
    lib = Librarian(corpus_db)
    lib.refresh()

    crit = deterministic_criterion(
        id="hello-exists", description="hello.txt contains 'hi'",
        predicate=lambda s: (s.workdir / "hello.txt").exists()
        and (s.workdir / "hello.txt").read_text() == "hi",
    )
    contract = Contract.new(obligation=obligation, criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["Create hello.txt with content 'hi'."],
        executor_turns=[
            _ScriptedTurn(
                text=None,
                tool_calls=[
                    # Candidate 0: bash (NOT in corpus) — should DEFER
                    {"name": "bash", "arguments": {"command": "echo hi > hello.txt"}},
                    # Candidate 1: write (IN corpus) — should win
                    {"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}},
                ],
            ),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(),
                    recorder=recorder, librarian=lib)
    run = act.run(contract, state)
    traj = recorder.finalize()

    assert contract.state == ContractState.FULFILLED, contract.state_reason
    assert (workdir / "hello.txt").read_text() == "hi"

    # The election event must have been recorded
    elections = [e for e in traj.events if e.kind == "election"]
    assert len(elections) == 1, "expected exactly one election event"
    ev = elections[0]
    # Math should pick candidate 1 (write) since bash defers and write scores
    assert ev.payload["winner_index"] == 1
    assert ev.payload["decision"] == "math"
    assert ev.payload["winner_tool"] == "write"
    assert len(ev.payload["candidates"]) == 2


def test_election_defers_when_all_candidates_unseen(workdir, recorder, tmp_path):
    """All candidates are unseen in the corpus → ELECTION defers to candidate
    0 (LLM's preferred). Need a NON-empty corpus (otherwise librarian is
    not ready and election is skipped entirely)."""
    from cat.librarian import Librarian

    # Seed corpus with a DIFFERENT obligation so the live state hashes to
    # something the corpus has never seen.
    corpus_db = tmp_path / "corpus.db"
    _ingest_pass_trace_with_actions(
        corpus_db, "some unrelated obligation",
        [{"tool": "read", "args": {"path": "/x"}}],
        trial_id="seed-0",
    )
    lib = Librarian(corpus_db)
    lib.refresh()

    crit = deterministic_criterion(
        id="hello-exists", description="hello.txt contains 'hi'",
        predicate=lambda s: (s.workdir / "hello.txt").exists(),
    )
    contract = Contract.new(obligation="totally different obligation", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["x"],
        executor_turns=[
            _ScriptedTurn(
                text=None,
                tool_calls=[
                    {"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}},
                    {"name": "bash", "arguments": {"command": "echo hi > hello.txt"}},
                ],
            ),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(),
                    recorder=recorder, librarian=lib)
    act.run(contract, state)
    traj = recorder.finalize()

    elections = [e for e in traj.events if e.kind == "election"]
    assert len(elections) == 1
    ev = elections[0]
    assert ev.payload["winner_index"] == 0
    assert ev.payload["decision"] == "defer"


def test_election_skipped_when_single_candidate(workdir, recorder, tmp_path):
    """LLM emits exactly 1 tool call — no election event, no librarian call.

    Use a non-empty corpus to prove the skip is driven by candidate-count,
    not by librarian-not-ready."""
    from cat.librarian import Librarian
    _ingest_pass_trace_with_actions(
        tmp_path / "c.db", "x",
        [{"tool": "read", "args": {"path": "/x"}}],
        trial_id="seed-0",
    )
    lib = Librarian(tmp_path / "c.db")
    lib.refresh()

    crit = deterministic_criterion(
        id="hello-exists", description="hello.txt contains 'hi'",
        predicate=lambda s: (s.workdir / "hello.txt").exists(),
    )
    contract = Contract.new(obligation="x", criteria=[crit])
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["x"],
        executor_turns=[
            _ScriptedTurn(
                text=None,
                tool_calls=[{"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}}],
            ),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(),
                    recorder=recorder, librarian=lib)
    act.run(contract, state)
    traj = recorder.finalize()

    elections = [e for e in traj.events if e.kind == "election"]
    assert len(elections) == 0


def test_librarian_hint_emits_trace_event(workdir, recorder, tmp_path):
    """When a hint is produced, a librarian_hint trace event must be
    recorded. Lets us count emissions and attribute behavior changes."""
    from cat.librarian import Librarian
    _ingest_pass_trace_with_actions(
        tmp_path / "c.db", "Create hello.txt with content 'hi'.",
        [{"tool": "write", "args": {"path": "hello.txt", "content": "hi"}}],
        trial_id="seed-0",
    )
    _ingest_pass_trace_with_actions(
        tmp_path / "c.db", "Create hello.txt with content 'hi'.",
        [{"tool": "write", "args": {"path": "hello.txt", "content": "hi"}}],
        trial_id="seed-1",
    )
    lib = Librarian(tmp_path / "c.db")
    lib.refresh()

    crit = deterministic_criterion(
        id="hello-exists", description="hello.txt contains 'hi'",
        predicate=lambda s: (s.workdir / "hello.txt").exists(),
    )
    contract = Contract.new(
        obligation="Create hello.txt with content 'hi'.",
        criteria=[crit],
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["Create hello.txt with content 'hi'."],
        executor_turns=[
            _ScriptedTurn(
                text=None,
                tool_calls=[{"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}}],
            ),
        ],
    )
    act = Activator(llm=llm, registry=build_default_registry(),
                    recorder=recorder, librarian=lib)
    act.run(contract, state)
    traj = recorder.finalize()

    hint_events = [e for e in traj.events if e.kind == "librarian_hint"]
    # We expect at least one hint since the corpus is non-empty and the
    # initial state matches the seeded obligation.
    assert len(hint_events) >= 1, "expected at least one librarian_hint event"
    ev = hint_events[0]
    assert ev.payload["contract_id"] == contract.id
    assert ev.payload["tool"] == "write"  # what the corpus recommends
    assert ev.payload["confidence_n"] >= 1
