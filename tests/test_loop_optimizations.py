"""Tests for the 5 loop optimizations (F1, F2, F3, F4, P2).

Covers the three Phase 3 benchmark failure scenarios:
  - hello-world    : F1 (per-task test_cmd), P2 (output isolation)
  - fix-pandas     : F2 (re-plan on deadlock), F4 (criterion fail streak)
  - csv-to-parquet : P2 (criterion check interval)

All tests use ScriptedLLM (mock) and MockSession (mock tmux) — no real LLM or
Docker calls.
"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pytest

from cat.activator import Activator, _extract_turn_summary
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
from cat.llm import LLMResult, PLANNER_SYSTEM_PROMPT
from cat.tb_agents import TbCatAgent, _make_test_runner_criterion
from cat.trace import Recorder


# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

@dataclass
class _ScriptedTurn:
    text: Optional[str] = None
    tool_calls: list[dict] = None  # type: ignore[assignment]


class ScriptedLLM:
    """Programmable mock LLM for loop-mechanics tests."""

    def __init__(
        self,
        planner_steps: Optional[list[str]] = None,
        executor_turns: Optional[list[_ScriptedTurn]] = None,
        judge_verdicts: Optional[list[bool]] = None,
        judge_default: bool = True,
    ):
        self.model = "scripted/mock"
        self.api_model = "scripted/mock"
        self.recorder = None
        self.parent_event_id = None
        self.planner_steps = planner_steps or ["do the task"]
        self.executor_queue: Iterator[_ScriptedTurn] = iter(executor_turns or [])
        self._judge_verdicts: Iterator[bool] = iter(judge_verdicts or [])
        self._judge_default = judge_default
        self.calls: list[dict] = []

    def chat(self, messages, *, tools=None, tool_choice=None, response_format=None,
             max_tokens=None, temperature=0.2, role_tag="executor") -> LLMResult:
        self.calls.append({"role_tag": role_tag, "n_messages": len(messages)})

        if role_tag == "planner":
            payload = {"steps": [{"id": f"s{i+1}", "description": d}
                                  for i, d in enumerate(self.planner_steps)]}
            return LLMResult(text=json.dumps(payload), tokens_in=20, tokens_out=20,
                             model=self.model)

        if role_tag == "judge":
            try:
                verdict = next(self._judge_verdicts)
            except StopIteration:
                verdict = self._judge_default
            return LLMResult(
                text=json.dumps({"complete": verdict, "reason": "mock"}),
                tokens_in=10, tokens_out=10, model=self.model,
            )

        # executor
        try:
            turn = next(self.executor_queue)
        except StopIteration:
            return LLMResult(text="DONE", tool_calls=[], tokens_in=10, tokens_out=2,
                             model=self.model)
        tcs = []
        for tc in (turn.tool_calls or []):
            tcs.append({
                "id": tc.get("id") or f"tc-{uuid.uuid4().hex[:6]}",
                "name": tc["name"],
                "arguments": tc.get("arguments", {}),
            })
        return LLMResult(text=turn.text, tool_calls=tcs, tokens_in=50, tokens_out=20,
                         model=self.model, usd=0.0001)


class MockSession:
    """Fake TmuxSession for testing test-runner criterion."""

    def __init__(self, pane_outputs: list[str]):
        self._outputs: Iterator[str] = iter(pane_outputs)
        self.sent: list[list] = []

    def send_keys(self, keys, block=True, max_timeout_sec=90):
        self.sent.append(keys)

    def capture_pane(self) -> str:
        try:
            return next(self._outputs)
        except StopIteration:
            return "EXIT:1"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    return Recorder(run_id="r-opt", task_id="t1", agent="test", model="mock",
                    out_dir=tmp_path / "out")


# ---------------------------------------------------------------------------
# F3 — Planner prompt: no investigation steps
# ---------------------------------------------------------------------------

def test_planner_prompt_forbids_investigation_steps():
    """PLANNER_SYSTEM_PROMPT must include the rule banning research steps."""
    assert "DO NOT include investigation" in PLANNER_SYSTEM_PROMPT
    assert "identify" in PLANNER_SYSTEM_PROMPT
    assert "concrete state change" in PLANNER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# F1 — hello-world scenario: per-task test_cmd dict
# ---------------------------------------------------------------------------

class _FakeTbBase:
    """Minimal BaseAgent stub so TbCatAgent can be constructed without terminal_bench."""
    def __init__(self, **kwargs):
        pass


def _make_agent(test_cmd, monkeypatch) -> TbCatAgent:
    """Construct TbCatAgent with BaseAgent patched out."""
    import cat.tb_agents as tb_mod
    original_base = tb_mod.BaseAgent
    # Patch BaseAgent to a no-op so we don't need terminal_bench installed
    tb_mod.BaseAgent = _FakeTbBase  # type: ignore[attr-defined]
    try:
        agent = TbCatAgent.__new__(TbCatAgent)
        _FakeTbBase.__init__(agent)
        TbCatAgent.__init__(agent, test_cmd=test_cmd)
    finally:
        tb_mod.BaseAgent = original_base
    return agent


def test_f1_string_test_cmd_resolves_to_same_for_all_tasks(tmp_path):
    """str test_cmd applies to all tasks regardless of logging_dir."""
    import cat.tb_agents as tb_mod
    tb_mod.BaseAgent = _FakeTbBase  # type: ignore[attr-defined]

    agent = object.__new__(TbCatAgent)
    _FakeTbBase.__init__(agent)
    TbCatAgent.__init__(agent, test_cmd="python -m pytest tests/ -q")

    assert agent._resolve_test_cmd(None) == "python -m pytest tests/ -q"
    assert agent._resolve_test_cmd(tmp_path / "hello-world/trial/agent-logs") == \
        "python -m pytest tests/ -q"


def test_f1_dict_test_cmd_resolved_by_task_id(tmp_path):
    """dict test_cmd looks up task_id inferred from logging_dir path."""
    test_cmds = {
        "hello-world": "",
        "fix-pandas-version": "python -m pytest tests/ -q",
        "csv-to-parquet": "python -m pytest tests/ -q",
    }
    agent = object.__new__(TbCatAgent)
    _FakeTbBase.__init__(agent)
    TbCatAgent.__init__(agent, test_cmd=test_cmds)

    # Path: .../task-id/trial-name/agent-logs
    hw_dir = tmp_path / "hello-world" / "hello-world.1-of-3" / "agent-logs"
    fp_dir = tmp_path / "fix-pandas-version" / "fix-pandas-version.1-of-3" / "agent-logs"
    ct_dir = tmp_path / "csv-to-parquet" / "csv-to-parquet.1-of-3" / "agent-logs"

    assert agent._resolve_test_cmd(hw_dir) == ""
    assert agent._resolve_test_cmd(fp_dir) == "python -m pytest tests/ -q"
    assert agent._resolve_test_cmd(ct_dir) == "python -m pytest tests/ -q"


def test_f1_dict_missing_task_falls_back_to_empty(tmp_path):
    """dict test_cmd returns '' for unknown task_id → falls back to LLM judge."""
    agent = object.__new__(TbCatAgent)
    _FakeTbBase.__init__(agent)
    TbCatAgent.__init__(agent, test_cmd={"csv-to-parquet": "pytest -q"})

    unknown_dir = tmp_path / "new-task" / "new-task.1-of-3" / "agent-logs"
    assert agent._resolve_test_cmd(unknown_dir) == ""


def test_f1_json_string_decoded_to_dict():
    """JSON string starting with '{' is auto-decoded to dict in __init__."""
    json_str = '{"hello-world": "", "fix-pandas-version": "pytest -q"}'
    agent = object.__new__(TbCatAgent)
    _FakeTbBase.__init__(agent)
    TbCatAgent.__init__(agent, test_cmd=json_str)

    assert isinstance(agent._test_cmd, dict)
    assert agent._test_cmd["hello-world"] == ""
    assert agent._test_cmd["fix-pandas-version"] == "pytest -q"


def test_f1_hello_world_no_test_cmd_uses_judge_criterion(workdir, recorder):
    """hello-world with empty test_cmd → LLM judge closes contract when judge says done."""
    # Simulate: write hello.txt, judge says step complete, criterion (judge-based) closes
    wrote_file = workdir / "hello.txt"

    call_count = [0]
    def judge_criterion(state: State) -> tuple[bool, Evidence]:
        call_count[0] += 1
        return (wrote_file.exists(), Evidence(summary="judge mock"))

    crit = Criterion(id="judge", description="file written", predicate=judge_criterion)
    contract = Contract.new(obligation="Write hello.txt", criteria=[crit])
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["Write hello.txt with Hello, world!"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{
                "name": "write",
                "arguments": {"path": "hello.txt", "content": "Hello, world!\n"},
            }]),
        ],
        judge_default=True,
    )
    act = Activator(llm=llm, registry=build_default_registry(), recorder=recorder)
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FULFILLED
    assert wrote_file.read_text() == "Hello, world!\n"


# ---------------------------------------------------------------------------
# P2 — Criterion check interval + output isolation
# ---------------------------------------------------------------------------

def test_p2_criterion_only_checked_on_interval(workdir, recorder):
    """With criterion_check_interval=3, criterion is called on iterations 3, 6, ...
    not every iteration."""
    check_calls = [0]

    def counting_predicate(state: State) -> tuple[bool, Evidence]:
        check_calls[0] += 1
        return False, Evidence(summary="always fail")

    crit = Criterion(id="count", description="count checks", predicate=counting_predicate)
    contract = Contract.new(
        obligation="do work",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=6, max_llm_calls=50, max_tokens_total=50_000),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["step one"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(6)
        ],
        judge_default=False,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=20,
        criterion_check_interval=3,
    )
    act.run(contract, state)
    recorder.finalize()

    # With 6 tool calls and interval=3: checks at iterations 3 and 6 = 2 checks
    # (plus 1 check when executor stops at the end)
    assert check_calls[0] <= 3  # well under 6 (one-per-iteration baseline)


def test_p2_criterion_output_isolated_from_terminal_output():
    """test-runner criterion writes to last_criterion_output, not terminal_output."""
    pane_output = "some test output\nEXIT:0"
    session = MockSession([pane_output])

    state = State(workdir=Path("/tmp"))
    state.kv["terminal_output"] = "original executor output"

    crit = _make_test_runner_criterion(session, "pytest -q")
    ok, ev = crit.predicate(state)

    assert ok is True
    assert state.kv.get("terminal_output") == "original executor output"  # unchanged
    assert state.kv.get("last_criterion_output") == pane_output


def test_p2_csv_to_parquet_criterion_checks_reduced(workdir, recorder):
    """csv-to-parquet scenario: 9 tool calls with interval=3 → 3 criterion probes."""
    probe_count = [0]

    def probe(state: State) -> tuple[bool, Evidence]:
        probe_count[0] += 1
        return False, Evidence(summary="not done yet")

    crit = Criterion(id="parquet-check", description="parquet exists", predicate=probe)
    contract = Contract.new(
        obligation="Convert CSV to Parquet",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=9, max_llm_calls=60, max_tokens_total=100_000),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["read CSV", "convert", "write parquet"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": f"step{i}.txt", "content": "x"}}])
            for i in range(9)
        ],
        judge_default=False,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=20,
        criterion_check_interval=3,
    )
    act.run(contract, state)
    recorder.finalize()

    # 9 iterations with interval=3 → probes at 3, 6, 9 = 3 probes (+ final stop check)
    assert probe_count[0] <= 4   # vs 9 without interval


# ---------------------------------------------------------------------------
# F4 — fix-pandas scenario: criterion fail streak exits loop early
# ---------------------------------------------------------------------------

def test_f4_criterion_unreachable_exits_loop(workdir, recorder):
    """After max_criterion_fails consecutive FAILs, loop exits with criterion_unreachable."""
    crit = deterministic_criterion(
        id="never-pass",
        description="always false",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="fix environment",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=100, max_llm_calls=200, max_tokens_total=500_000),
    )
    state = State(workdir=workdir)

    # Executor keeps making tool calls indefinitely; streak should stop it
    llm = ScriptedLLM(
        planner_steps=["upgrade package"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(50)
        ],
        judge_default=True,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=50,
        max_criterion_fails=5,
        criterion_check_interval=1,
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FAILED
    assert run.failure_reason == "criterion_unreachable"
    # Should stop at iteration 5 (one per-iteration check × 5 streak), not 50
    assert run.iterations <= 6


def test_f4_streak_resets_on_pass(workdir, recorder):
    """Streak resets to 0 when criterion passes; no false criterion_unreachable.

    Uses judge_default=False so F2 (replan) never fires, isolating streak logic.
    Writes the target file on the second tool call; criterion passes on iter 2.
    """
    wrote = workdir / "done.txt"

    def predicate(state: State) -> tuple[bool, Evidence]:
        ok = wrote.exists()
        return ok, Evidence(summary="done" if ok else "not done")

    crit = Criterion(id="done", description="done.txt exists", predicate=predicate)
    contract = Contract.new(
        obligation="write done.txt",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=10, max_llm_calls=50, max_tokens_total=50_000),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["write done.txt"],
        executor_turns=[
            # iter 1: write a dummy file → criterion FAIL (streak=1)
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "tmp.txt", "content": "x"}}]),
            # iter 2: write done.txt → criterion PASS (streak resets to 0)
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "done.txt", "content": "done"}}]),
        ],
        judge_default=False,   # steps never complete → F2 never fires
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=10,
        max_criterion_fails=3,  # higher than failures (1) before pass
        criterion_check_interval=1,
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FULFILLED
    assert run.criterion_fail_streak == 0  # reset after pass


# ---------------------------------------------------------------------------
# F2 — fix-pandas scenario: re-plan on all-steps-complete + criterion FAIL
# ---------------------------------------------------------------------------

def test_f2_replan_injected_when_deadlocked(workdir, recorder):
    """When all steps are judge-complete but criterion still FAIL, a recovery message
    is appended and step flags are reset."""
    crit = deterministic_criterion(
        id="never",
        description="never satisfied",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="fix pandas",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=20, max_llm_calls=100, max_tokens_total=200_000),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["upgrade pandas", "verify version"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "step.txt", "content": "x"}}])
            for _ in range(10)
        ],
        judge_default=True,   # judge always says steps are complete → triggers deadlock
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=20,
        max_criterion_fails=20,   # high — don't let streak end it; we want F2 to fire
        criterion_check_interval=1,
    )
    run = act.run(contract, state)
    recorder.finalize()

    # F2 should have injected at least one recovery message (tracked via replan_count,
    # not via run.messages since P1 pruning may have dropped the raw recovery message)
    assert run.replan_count >= 1


def test_f2_replan_capped_at_two(workdir, recorder):
    """Re-plan is injected at most twice; after that, streak or budget ends the run."""
    crit = deterministic_criterion(
        id="impossible",
        description="never passes",
        predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="impossible",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=50, max_llm_calls=200, max_tokens_total=500_000),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["step one"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(40)
        ],
        judge_default=True,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=40,
        max_criterion_fails=50,  # don't let streak stop it — cap test is for F2
        criterion_check_interval=1,
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert run.replan_count <= 2


def test_f2_fix_pandas_full_scenario(workdir, recorder):
    """Fix-pandas scenario: planner emits action steps (F3), executor upgrades
    package, judge marks done, criterion FAIL triggers replan (F2), then streak
    fires (F4) — whole sequence stays within budget."""
    crit = deterministic_criterion(
        id="tests-pass",
        description="pytest exit 0",
        predicate=lambda s: False,   # simulate pytest always failing (tests/ missing)
    )
    contract = Contract.new(
        obligation="Upgrade pandas so dtype_backend is supported",
        criteria=[crit],
        constraints=Constraints(
            max_tool_calls=30,
            max_llm_calls=100,
            max_tokens_total=200_000,
            max_usd=5.0,
        ),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=[
            "Upgrade pandas to >=2.0 to support dtype_backend",
            "Upgrade pyarrow to >=7.0 to satisfy pandas 2.0 dependency",
            "Run the data processor script to confirm it works",
        ],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": "pip install pandas>=2.0"}}]),
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": "pip install pyarrow>=7.0"}}]),
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": "python -m src.data_processor"}}]),
        ] + [
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": f"diagnostic_{i}"}}])
            for i in range(15)
        ],
        judge_default=True,   # all steps immediately complete
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=30,
        max_criterion_fails=5,         # F4: exits after 5 consecutive criterion FAILs
        criterion_check_interval=1,
    )
    run = act.run(contract, state)
    recorder.finalize()

    # Should fail via criterion_unreachable or replan+streak, not budget
    assert contract.state == ContractState.FAILED
    # Must not have burned through all 30 iterations
    assert run.iterations < 20
    # At least one replan attempt
    assert run.replan_count >= 1


# ---------------------------------------------------------------------------
# P1 — Sliding window + prior attempts log
# ---------------------------------------------------------------------------

def test_p1_extract_turn_summary_bash_error():
    """_extract_turn_summary produces a compact line for a failed bash turn."""
    turn = [
        {
            "role": "assistant",
            "tool_calls": [{
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": "pip install pandas"}),
                }
            }],
        },
        {
            "role": "tool",
            "content": "[ERROR] pip: command not found",
        },
        {
            "role": "user",
            "content": "CHECKLIST UPDATE: s1=[ ]",
        },
    ]
    summary = _extract_turn_summary(turn, 1)
    assert "bash" in summary
    assert "pip install pandas" in summary
    assert "ERR" in summary


def test_p1_extract_turn_summary_write_success():
    """_extract_turn_summary produces compact line for a successful write."""
    turn = [
        {
            "role": "assistant",
            "tool_calls": [{
                "function": {
                    "name": "write",
                    "arguments": json.dumps({"path": "/app/convert.py", "content": "import pandas"}),
                }
            }],
        },
        {
            "role": "tool",
            "content": "wrote 13 chars to /app/convert.py",
        },
    ]
    summary = _extract_turn_summary(turn, 3)
    assert "write" in summary
    assert "convert.py" in summary
    assert "OK" in summary


def test_p1_extract_turn_summary_no_tool_call():
    """_extract_turn_summary returns '' for a turn with no tool call (executor said DONE)."""
    turn = [{"role": "assistant", "content": "DONE", "tool_calls": []}]
    assert _extract_turn_summary(turn, 1) == ""


def test_p1_window_prunes_oldest_turn(workdir, recorder):
    """With window_size=2, turn 3 causes turn 1 to be dropped to prior_attempts."""
    crit = deterministic_criterion(
        id="done",
        description="done.txt exists",
        predicate=lambda s: (s.workdir / "done.txt").exists(),
    )
    contract = Contract.new(
        obligation="write done.txt",
        criteria=[crit],
        constraints=Constraints(max_tool_calls=10, max_llm_calls=50, max_tokens_total=100_000),
    )
    state = State(workdir=workdir)

    # 4 turns: 3 distractors + 1 solve. With window=2, after iter 3 the prune fires
    # (3 turns > window 2), dropping turn 1 to prior_attempts. Iter 4 then solves.
    llm = ScriptedLLM(
        planner_steps=["write done.txt"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "tmp1.txt", "content": "x"}}]),
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "tmp2.txt", "content": "x"}}]),
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": "ls"}}]),
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "done.txt", "content": "done"}}]),
        ],
        judge_default=False,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=10,
        window_size=2,
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FULFILLED
    # Turn 1 (write tmp1) was dropped before the solve turn → prior_attempts has its summary
    assert len(run.prior_attempts) >= 1
    assert any("tmp1" in a or "write" in a for a in run.prior_attempts)


def test_p1_messages_bounded_by_window(workdir, recorder):
    """run.messages length stays bounded regardless of iteration count."""
    crit = deterministic_criterion(
        id="never", description="never", predicate=lambda s: False,
    )
    contract = Contract.new(
        obligation="do work",
        criteria=[crit],
        # REPRESENT uses 4 probes before the executor loop; give enough budget for
        # 12 executor iterations on top of those.
        constraints=Constraints(max_tool_calls=100, max_llm_calls=80, max_tokens_total=500_000),
    )
    state = State(workdir=workdir)

    llm = ScriptedLLM(
        planner_steps=["step one"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": f"f{i}.txt", "content": "x"}}])
            for i in range(12)
        ],
        judge_default=False,
    )
    window = 3
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=12,
        window_size=window,
        max_criterion_fails=50,  # don't let streak stop it
    )
    run = act.run(contract, state)
    recorder.finalize()

    # After 12 iters with window=3: messages = sys + user_fresh + prior_log + 3 turns*3msgs = 12
    # Allow some slack for the F2 replan or executor DONE message, but it must be << 12*3=36
    max_expected = 2 + 1 + window * 4 + 5   # generous upper bound
    assert len(run.messages) <= max_expected, (
        f"messages={len(run.messages)} exceeds window bound {max_expected}"
    )
    # prior_attempts should have summaries of dropped turns
    assert len(run.prior_attempts) >= (12 - window - 1)


def test_p1_task_still_solves_with_pruning(workdir, recorder):
    """hello-world scenario still solves correctly when window_size=2 forces pruning."""
    crit = deterministic_criterion(
        id="hello",
        description="hello.txt exists with correct content",
        predicate=lambda s: (s.workdir / "hello.txt").exists()
                            and (s.workdir / "hello.txt").read_text() == "Hello, world!\n",
    )
    contract = Contract.new(obligation="Write hello.txt", criteria=[crit])
    state = State(workdir=workdir)

    # 4 turns: 3 distractors (criterion FAIL) + 1 solve. window=2 forces a prune
    # after iter 3 (3 turns > 2), so turn 1 is in prior_attempts before iter 4 solves.
    llm = ScriptedLLM(
        planner_steps=["write hello.txt with Hello, world! and a newline"],
        executor_turns=[
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "wrong.txt", "content": "oops"}}]),
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": "ls"}}]),
            _ScriptedTurn(tool_calls=[{"name": "bash",
                                       "arguments": {"command": "pwd"}}]),
            _ScriptedTurn(tool_calls=[{"name": "write",
                                       "arguments": {"path": "hello.txt",
                                                     "content": "Hello, world!\n"}}]),
        ],
        judge_default=True,
    )
    act = Activator(
        llm=llm,
        registry=build_default_registry(),
        recorder=recorder,
        max_iterations=10,
        window_size=2,
    )
    run = act.run(contract, state)
    recorder.finalize()

    assert contract.state == ContractState.FULFILLED
    assert (workdir / "hello.txt").read_text() == "Hello, world!\n"
    assert len(run.prior_attempts) >= 1  # turn 1 was pruned before the solve
