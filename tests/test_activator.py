"""Activator loop tests with a mock LLM.

The mock returns a programmable sequence of (text, tool_calls) responses; this
isolates loop semantics (state transitions, budget enforcement, criterion
checks) from real LLM calls.
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
    role_tag_expected: Optional[str] = None  # if set, assert called with this role


class ScriptedLLM:
    """LLM substitute with a queue of canned responses keyed by role_tag.

    Behavior:
      - planner calls return JSON with steps
      - judge calls return JSON {"complete": true/false, "reason": "..."}
      - executor calls draw from a queue
    """

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
    ) -> LLMResult:
        self.calls.append({"role_tag": role_tag, "n_messages": len(messages)})

        if role_tag == "planner":
            payload = {"steps": [{"id": f"s{i+1}", "description": d}
                                 for i, d in enumerate(self.planner_steps)]}
            return LLMResult(text=json.dumps(payload), tokens_in=20, tokens_out=20, model=self.model)

        if role_tag == "judge":
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


def test_activator_fulfills_on_single_write(workdir, recorder):
    """LLM writes hello.txt with the right content; criterion passes; FULFILLED."""
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


def test_activator_fails_when_budget_violated(workdir, recorder):
    """Budget cap on tool_calls is hit before criterion satisfied → FAILED."""
    # Predicate that's never true, so we burn tool_calls until budget kills us.
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
    """Planner runs exactly once at decompose; judge may run multiple times."""
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

    planner_calls = sum(1 for c in llm.calls if c["role_tag"] == "planner")
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
    # We expect: init→latent, latent→eligible, eligible→active, active→fulfilled
    assert "latent" in states_visited
    assert "eligible" in states_visited
    assert "active" in states_visited
    assert "fulfilled" in states_visited


# ── Criterion history + feedback injection (new feature) ─────────────────────


def test_criterion_history_populated_on_fail(workdir, recorder):
    """Each real criterion FAIL appends (iter_num, evidence) to run.criterion_history."""
    crit = deterministic_criterion(
        id="never",
        description="file.txt must exist",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="create file.txt",
        criteria=[crit],
        # Representer now makes 6 tool calls (1 write + 5 reads including eval_command).
        # Budget must be large enough to survive REPRESENT plus a few executor turns.
        constraints=Constraints(max_tool_calls=20, max_llm_calls=50, max_tokens_total=10_000),
    )
    state = State(workdir=workdir)
    llm = ScriptedLLM(
        planner_steps=["create file.txt"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(5)
        ],
        judge_default_complete=False,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        criterion_check_interval=1,
        max_criterion_fails=100,  # prevent replan so we get clean history
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert len(run.criterion_history) >= 1
    for iter_num, evidence in run.criterion_history:
        assert isinstance(iter_num, int)
        assert "FAIL" in evidence
        assert "file.txt must exist" in evidence


def test_build_criterion_context_deduplicates():
    """Repeated identical evidence lines are collapsed; different lines are shown once each."""
    act = Activator.__new__(Activator)  # _build_criterion_context doesn't use self

    class _FakeRun:
        criterion_history = [
            (3,  "FAIL: file.txt must exist"),
            (6,  "FAIL: file.txt must exist"),
            (9,  "FAIL: file.txt must exist"),
            (12, "FAIL: wrong content in file"),
        ]

    ctx = act._build_criterion_context(_FakeRun())
    assert "CRITERION FEEDBACK" in ctx
    # Dedup: "file.txt must exist" appears once, rest collapsed
    assert ctx.count("FAIL: file.txt must exist") == 1
    assert "repeated 2x" in ctx
    # Different evidence is shown
    assert "FAIL: wrong content in file" in ctx
    # Ends with the instruction
    assert "Do not repeat" in ctx


def test_build_criterion_context_empty_when_no_history():
    """Returns empty string when no criterion failures have been recorded yet."""
    act = Activator.__new__(Activator)

    class _EmptyRun:
        criterion_history: list = []

    assert act._build_criterion_context(_EmptyRun()) == ""


def test_replan_receives_criterion_context(workdir, recorder):
    """When the replan fires, the planner call's user message contains CRITERION FEEDBACK."""
    planner_messages: list[list[dict]] = []

    class _CapturingLLM(ScriptedLLM):
        def chat(self, messages, *, role_tag="executor", **kwargs):
            if role_tag == "planner":
                planner_messages.append(list(messages))
            return super().chat(messages, role_tag=role_tag, **kwargs)

    crit = deterministic_criterion(
        id="never",
        description="impossible condition",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="do the impossible",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=30, max_llm_calls=200, max_tokens_total=100_000),
    )
    state = State(workdir=workdir)
    llm = _CapturingLLM(
        planner_steps=["step one"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(30)
        ],
        judge_default_complete=False,
    )
    # max_criterion_fails=4 → replan_threshold=2; replan fires after 2 consecutive fails
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        criterion_check_interval=1,
        max_criterion_fails=4,
        max_iterations=30,
    )
    act.run(contract, state)
    recorder.finalize()

    # At least two planner calls: initial decompose + the replan
    assert len(planner_messages) >= 2, (
        f"expected initial + replan planner call, got {len(planner_messages)}"
    )
    # The replan call's messages must include criterion feedback
    replan_content = " ".join(m.get("content", "") for m in planner_messages[1])
    assert "CRITERION FEEDBACK" in replan_content, (
        "replan prompt did not contain criterion feedback"
    )
    assert "impossible condition" in replan_content, (
        "replan prompt did not include the failing criterion description"
    )


def test_free_form_prompt_includes_criterion_context(workdir, recorder):
    """When entering free-form mode the executor prompt contains CRITERION FEEDBACK."""
    executor_messages: list[list[dict]] = []

    class _CapturingLLM(ScriptedLLM):
        def chat(self, messages, *, role_tag="executor", **kwargs):
            if role_tag == "executor":
                executor_messages.append(list(messages))
            return super().chat(messages, role_tag=role_tag, **kwargs)

    crit = deterministic_criterion(
        id="never",
        description="impossible condition",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="do the impossible",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=30, max_llm_calls=200, max_tokens_total=100_000),
    )
    state = State(workdir=workdir)
    llm = _CapturingLLM(
        planner_steps=["step one"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write", "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(30)
        ],
        judge_default_complete=False,
    )
    # With max_criterion_fails=4: replan fires at fail streak=2, free-form at streak=2 again
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        criterion_check_interval=1,
        max_criterion_fails=4,
        max_iterations=30,
    )
    act.run(contract, state)
    recorder.finalize()

    # Find the first executor call whose prompt contains FREE-FORM (the free-form entry message)
    free_form_calls = [
        msgs for msgs in executor_messages
        if any("FREE-FORM" in (m.get("content") or "") for m in msgs)
    ]
    assert free_form_calls, "expected at least one free-form executor prompt"
    all_content = " ".join(m.get("content", "") for m in free_form_calls[0])
    assert "CRITERION FEEDBACK" in all_content, (
        "free-form prompt did not contain criterion feedback"
    )
