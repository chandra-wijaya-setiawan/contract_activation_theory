"""Terminal-bench adapters: TbCatAgent and TbReactAgent.

Both implement terminal_bench.agents.base_agent.BaseAgent and route all tool
execution through the provided TmuxSession.  The discipline logic (7-step
activator, pre-committed plan, judge-per-step, criterion gate) is identical to
the local CatAgent; only the bash backend changes.

Pass these to tb runs create via --agent-import-path:
    cat.tb_agents:TbCatAgent
    cat.tb_agents:TbReactAgent

Required env: OPENROUTER_API_KEY
"""
from __future__ import annotations
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Union, Optional

from terminal_bench.agents.base_agent import AgentResult as TbResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

from cat.activator import Activator
from cat.capabilities import build_tmux_registry
from cat.contracts import Constraints, Contract, Criterion, Evidence, State
from cat.llm import LLM, call_judge
from cat.trace import Recorder, TraceEvent, utcnow_iso


# ---------------------------------------------------------------------------
# Minimal in-memory recorder (no disk I/O) for when logging_dir is not set
# ---------------------------------------------------------------------------

class _MemRecorder:
    """Duck-typed Recorder substitute that only tracks token totals."""

    def __init__(self) -> None:
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.n_tool_calls: int = 0

    def append(self, event: TraceEvent) -> None:
        if event.kind == "llm_usage":
            self.tokens_in += int(event.payload.get("in", 0))
            self.tokens_out += int(event.payload.get("out", 0))
        elif event.kind == "tool_call":
            self.n_tool_calls += 1


def _make_recorder(logging_dir: Optional[Path], agent_name: str, model: str,
                   run_id: str) -> "_MemRecorder | Recorder":
    if logging_dir is None:
        return _MemRecorder()
    logging_dir.mkdir(parents=True, exist_ok=True)
    return Recorder(
        run_id=run_id,
        task_id="tb-task",
        agent=agent_name,
        model=model,
        out_dir=logging_dir,
    )


def _recorder_totals(recorder) -> tuple[int, int]:
    if isinstance(recorder, Recorder):
        return recorder.traj.tokens_in, recorder.traj.tokens_out
    return recorder.tokens_in, recorder.tokens_out


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

def _make_terminal_criterion(llm: LLM, instruction: str) -> Criterion:
    """LLM-judge criterion (fallback for tasks with no test command).

    Calls the judge after every tool call — expensive and imprecise.
    Prefer _make_test_runner_criterion when a test command is available.
    """
    def predicate(state: State) -> tuple[bool, Evidence]:
        terminal_output = state.kv.get("terminal_output", "(no terminal output yet)")
        summary = f"TERMINAL PANE (last capture):\n{terminal_output[:3000]}"
        complete, reason = call_judge(llm, instruction, summary)
        return complete, Evidence(summary=reason, detail={"judge": "terminal"})
    return Criterion(id="terminal-judge", description=instruction[:120], predicate=predicate)


def _make_test_runner_criterion(session: TmuxSession, test_cmd: str) -> Criterion:
    """Deterministic criterion: run a test command and check its exit code.

    Zero false-positive rate — the contract closes only when the tests
    actually pass, not when the model thinks they might.  Use this for any
    task that ships with a test suite (terminal-bench, SWE-bench, etc.).

    The test command exit code is captured by running it inside a subshell
    and echoing a sentinel:
        <test_cmd>; echo "EXIT:$?"
    Then we parse the last EXIT:N from the pane.

    Output is written to state.kv["last_criterion_output"] (not "terminal_output")
    so the judge's context is not polluted with pytest output.
    """
    import re

    def predicate(state: State) -> tuple[bool, Evidence]:
        probe = f"{test_cmd}; echo EXIT:$?"
        # Interrupt any long-running command in the pane (e.g. grep / find /)
        # before sending the oracle probe.  block=False + sleep avoids the
        # scenario where blocked output flushing eats into the oracle timeout:
        # we fire Ctrl+C, give the shell 2 s to settle, then run the probe.
        session.send_keys(["C-c"], block=False)
        time.sleep(2)
        try:
            session.send_keys([probe, "Enter"], block=True, max_timeout_sec=120)
        except TimeoutError:
            return False, Evidence(summary=f"test runner timed out: {test_cmd}")
        output = session.capture_pane()
        state.kv["last_criterion_output"] = output   # isolated — does not overwrite terminal_output
        matches = re.findall(r"EXIT:(\d+)", output)
        if not matches:
            return False, Evidence(summary="no EXIT code in pane", detail={"output": output[-300:]})
        exit_code = int(matches[-1])
        passed = (exit_code == 0)
        return passed, Evidence(
            summary=f"test exit_code={exit_code} {'PASS' if passed else 'FAIL'}",
            detail={"exit_code": exit_code, "test_cmd": test_cmd},
        )
    return Criterion(id="test-runner", description=f"exit_code==0 for: {test_cmd}", predicate=predicate, is_oracle=True)


# ---------------------------------------------------------------------------
# TbCatAgent
# ---------------------------------------------------------------------------

class TbCatAgent(BaseAgent):
    """CAT (contract-disciplined) agent for terminal-bench.

    Uses the 7-step Activator loop: planner decomposes the instruction into an
    ordered checklist, executor takes one tool call per turn, judge checks each
    step, criterion gate closes the contract when the terminal state satisfies
    the instruction.
    """

    @staticmethod
    def name() -> str:
        return "cat-tb"

    def __init__(
        self,
        model_name: str = "openrouter/deepseek/deepseek-v4-flash",
        max_iterations: int = 30,
        test_cmd: "Union[str, dict[str, str]]" = "",
        criterion_check_interval: int = 3,
        max_criterion_fails: int = 12,
        window_size: int = 4,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model_name = model_name
        self._max_iterations = max_iterations
        self._criterion_check_interval = criterion_check_interval
        self._max_criterion_fails = max_criterion_fails
        self._window_size = window_size
        # test_cmd may be:
        #   str  — same command for all tasks (or "" to fall back to LLM judge)
        #   dict — maps task_id → cmd; task_id inferred from logging_dir at run time
        #   JSON str starting with "{" — decoded to dict automatically (CLI-friendly)
        if isinstance(test_cmd, str) and test_cmd.startswith("{"):
            try:
                test_cmd = json.loads(test_cmd)
            except json.JSONDecodeError:
                pass
        self._test_cmd: Union[str, dict] = test_cmd

    def _resolve_test_cmd(self, logging_dir: Optional[Path]) -> str:
        """Return the test command for the current task.

        When test_cmd is a dict, the task_id is inferred from the logging_dir path:
            .../task-id/trial-name/agent-logs/
        task_id = logging_dir.parent.parent.name
        Falls back to "" (LLM judge) if the key is absent or logging_dir is None.
        """
        if isinstance(self._test_cmd, str):
            return self._test_cmd
        if logging_dir is not None:
            task_id = logging_dir.parent.parent.name
            return self._test_cmd.get(task_id, "")
        return ""

    def _initial_representations(
        self,
        session: TmuxSession,
        criterion: "Criterion",
        state: "State",
    ) -> "list":
        """Return Representation objects to inject before the REPRESENT step.

        Subclasses override this to prepend task-specific context (e.g.
        initial test failure output) so the planner sees it before generating
        its decomposition.  Default returns [].
        """
        return []

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Optional[Path] = None,
    ) -> TbResult:
        run_id = f"cat-tb-{uuid.uuid4().hex[:8]}"
        recorder = _make_recorder(logging_dir, self.name(), self._model_name, run_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            llm = LLM(model=self._model_name, recorder=recorder)
            registry = build_tmux_registry(session)
            resolved_cmd = self._resolve_test_cmd(logging_dir)
            if resolved_cmd:
                criterion = _make_test_runner_criterion(session, resolved_cmd)
            else:
                criterion = _make_terminal_criterion(llm, instruction)

            constraints = Constraints(
                max_tokens_total=500_000,
                max_tool_calls=self._max_iterations,
                max_llm_calls=3 * self._max_iterations,
                max_wall_s=900.0,
                max_usd=5.0,
            )
            contract = Contract.new(
                obligation=instruction,
                criteria=[criterion],
                constraints=constraints,
            )
            state = State(workdir=workdir)

            # Inject task-specific context before REPRESENT (subclass hook).
            pre_reps = self._initial_representations(session, criterion, state)
            if pre_reps:
                contract.representations = pre_reps

            activator = Activator(
                llm=llm,
                registry=registry,
                recorder=recorder,
                max_iterations=self._max_iterations,
                criterion_check_interval=self._criterion_check_interval,
                max_criterion_fails=self._max_criterion_fails,
                window_size=self._window_size,
            )
            activator.run(contract, state)

        if isinstance(recorder, Recorder):
            recorder.finalize()

        tokens_in, tokens_out = _recorder_totals(recorder)
        return TbResult(
            total_input_tokens=tokens_in,
            total_output_tokens=tokens_out,
            failure_mode=FailureMode.NONE,
        )


# ---------------------------------------------------------------------------
# TbReactAgent
# ---------------------------------------------------------------------------

_REACT_SYSTEM = (
    "You are a coding agent with bash, write, read, and list tools running inside "
    "a Docker container. Complete the user's task using the tools, then reply with "
    "a short confirmation when done. Be terse."
)


class TbReactAgent(BaseAgent):
    """ReAct control agent for terminal-bench.

    Vanilla tool-use loop: no pre-committed plan, no judge, no criterion gate.
    Terminates when the model emits a text response without any tool call, or
    when max_iterations is reached.  Apples-to-apples control for TbCatAgent.
    """

    @staticmethod
    def name() -> str:
        return "react-tb"

    def __init__(
        self,
        model_name: str = "openrouter/deepseek/deepseek-v4-flash",
        max_iterations: int = 30,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model_name = model_name
        self._max_iterations = max_iterations

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Optional[Path] = None,
    ) -> TbResult:
        run_id = f"react-tb-{uuid.uuid4().hex[:8]}"
        recorder = _make_recorder(logging_dir, self.name(), self._model_name, run_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            llm = LLM(model=self._model_name, recorder=recorder)
            registry = build_tmux_registry(session)
            state = State(workdir=workdir)
            tools = registry.tool_schemas()

            messages: list[dict] = [
                {"role": "system", "content": _REACT_SYSTEM},
                {"role": "user", "content": instruction},
            ]

            for _ in range(self._max_iterations):
                result = llm.chat(
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    role_tag="react",
                    temperature=0.2,
                )

                if result.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": result.text or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"]),
                                },
                            }
                            for tc in result.tool_calls
                        ],
                    })
                    for tc in result.tool_calls:
                        tr = registry.invoke(
                            name=tc["name"],
                            args=tc["arguments"],
                            state=state,
                            recorder=recorder,
                            tool_call_id=tc["id"],
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tr.output if not tr.is_error
                            else f"[ERROR] {tr.output}",
                        })
                else:
                    messages.append({"role": "assistant", "content": result.text or ""})
                    break  # model signalled done

        if isinstance(recorder, Recorder):
            recorder.finalize()

        tokens_in, tokens_out = _recorder_totals(recorder)
        return TbResult(
            total_input_tokens=tokens_in,
            total_output_tokens=tokens_out,
            failure_mode=FailureMode.NONE,
        )
