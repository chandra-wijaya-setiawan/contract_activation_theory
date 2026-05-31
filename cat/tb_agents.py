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
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Union, Optional

from terminal_bench.agents.base_agent import AgentResult as TbResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

from cat.activator import Activator
from cat.capabilities import build_docker_registry, build_tmux_registry
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
    """Deterministic criterion: run a test command via docker exec; check exit code.

    Post-1.6.6.1: bypasses tmux entirely. Goes through `container.exec_run`
    for direct subprocess execution with structured exit code and output.
    No sentinel scraping, no pane viewport limits, no `send_keys` round-trips.

    Output is written to state.kv["last_criterion_output"] so the attestor's
    context is not polluted with pytest output.
    """
    def predicate(state: State) -> tuple[bool, Evidence]:
        try:
            # Use `bash -lc` (login shell) so PATH/conda-env is sourced;
            # some SWE-bench testbeds keep pytest in a conda env not on the
            # default docker-exec PATH. See 1.6.6.1 follow-up.
            res = session.container.exec_run(
                cmd=["bash", "-lc", test_cmd],
                workdir="/testbed",
                demux=False,
                stdout=True,
                stderr=True,
            )
        except Exception as exc:  # noqa: BLE001
            return False, Evidence(
                summary=f"oracle docker exec error: {type(exc).__name__}: {exc}",
                detail={"test_cmd": test_cmd},
            )
        output = ""
        if res.output is not None:
            output = (
                res.output.decode("utf-8", errors="replace")
                if isinstance(res.output, (bytes, bytearray))
                else str(res.output)
            )
        state.kv["last_criterion_output"] = output
        exit_code = int(res.exit_code or 0)
        passed = (exit_code == 0)
        return passed, Evidence(
            summary=f"test exit_code={exit_code} {'PASS' if passed else 'FAIL'}",
            detail={"exit_code": exit_code, "test_cmd": test_cmd, "output": output[-2000:]},
        )
    return Criterion(
        id="test-runner",
        description=f"exit_code==0 for: {test_cmd}",
        predicate=predicate,
        is_oracle=True,
    )


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
        model_name: str = os.environ.get("CAT_MODEL", "openrouter/deepseek/deepseek-v4-flash"),
        max_iterations: int = 30,
        test_cmd: "Union[str, dict[str, str]]" = "",
        criterion_check_interval: int = 3,
        window_size: int = 4,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model_name = model_name
        self._max_iterations = max_iterations
        self._criterion_check_interval = criterion_check_interval
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

    def _use_tmux_backend(self) -> bool:
        """Subclasses opt in to the tmux execution backend if their tasks need
        an interactive shell (e.g., crack-7z-hash, configure-git-webserver).

        Default False (post-1.6.6.1): use docker exec for non-interactive
        tasks like SWE-bench / AgentBench OS. The tmux backend remains
        available but is no longer the default.
        """
        return False

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
            # 1.6.6.1: prefer the docker exec backend for non-interactive tasks
            # (SWE-bench, AgentBench OS, etc.). Falls back to tmux only when
            # the subclass explicitly requests it via `_use_tmux_backend()`.
            if self._use_tmux_backend():
                registry = build_tmux_registry(session)
            else:
                registry = build_docker_registry(session.container)
            resolved_cmd = self._resolve_test_cmd(logging_dir)
            if resolved_cmd:
                criterion = _make_test_runner_criterion(session, resolved_cmd)
            else:
                criterion = _make_terminal_criterion(llm, instruction)

            constraints = Constraints(
                max_tokens_total=500_000,
                max_tool_calls=self._max_iterations,
                max_llm_calls=3 * self._max_iterations,
                # 2700s = 45 min; harness cap is 3000s (50 min). The 5-min
                # buffer lets the contract self-close via the admission-locked
                # deadline_ts before the harness reaps the container. Result:
                # failure_mode=unset with explicit state_reason instead of
                # silent agent_timeout. See PLAN.md 1.5.15.
                max_wall_s=2700.0,
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

            # Legacy "election" advisor — CAT_CORPUS_DB enables the v1-style
            # sample-based recommender (cat/_legacy/librarian.py). Cold-start
            # safe: if path doesn't exist OR corpus is empty, recommend()
            # returns None and the activator falls through to standard behavior.
            librarian = None
            corpus_db = os.environ.get("CAT_CORPUS_DB", "").strip()
            if corpus_db:
                try:
                    from cat._legacy.librarian import Librarian
                    librarian = Librarian(corpus_db)
                    librarian.refresh()
                except Exception as exc:  # noqa: BLE001 — never fail task on librarian
                    print(f"warning: librarian init failed ({exc}); continuing without advisor")
                    librarian = None

            # Plan-tree library — persistent plan trees + edges across tasks
            # (cat/librarian.py LibrarianV2 class; env var name kept for back-compat).
            librarian_writer = None
            plan_db = os.environ.get("CAT_LIBRARIAN_V2_DB", "").strip()
            if plan_db:
                try:
                    from cat.librarian import LibrarianV2
                    librarian_writer = LibrarianV2(plan_db)
                except Exception as exc:
                    print(f"warning: plan-tree librarian init failed ({exc}); continuing without it")
                    librarian_writer = None
            activator = Activator(
                llm=llm,
                registry=registry,
                recorder=recorder,
                max_iterations=self._max_iterations,
                criterion_check_interval=self._criterion_check_interval,
                window_size=self._window_size,
                librarian=librarian,
                librarian_writer=librarian_writer,
            )
            activator.run(contract, state)

        if isinstance(recorder, Recorder):
            recorder.finalize()
            # 1.5.18: per-task auto-emit. Cheap — heuristic only (no LLM here
            # because we don't want every task to make an extra LLM call on
            # the agent's critical path). Run-level reports re-attribute with
            # LLM via `python -m cat.run_report <dir>`.
            try:
                self._emit_task_kpi(recorder, contract)
            except Exception as exc:  # noqa: BLE001 — never fail the task on KPI emit
                pass  # silently skip if anything goes wrong; the trace is still written

        tokens_in, tokens_out = _recorder_totals(recorder)
        return TbResult(
            total_input_tokens=tokens_in,
            total_output_tokens=tokens_out,
            failure_mode=FailureMode.NONE,
        )

    def _emit_task_kpi(self, recorder: "Recorder", contract: "Contract") -> None:
        """Write task_kpi.json next to the trace JSONL. Heuristic attribution only."""
        import json as _json
        from cat.metrics import RunMetrics
        from cat.attribution import attribute_task

        jsonl_path = recorder.jsonl_path
        metrics = RunMetrics.from_jsonl(jsonl_path)
        events = [ev.model_dump() for ev in recorder.traj.events]
        # Outcome from contract state (no harness verdict available at this point)
        outcome = {
            "fulfilled": "PASS",
            "failed": "FAIL",
            "suspended": "SUSPENDED",
            "expired": "FAIL",
        }.get(contract.state.value, "FAIL")
        attribution = attribute_task(
            contract.id, metrics, events, outcome=outcome, llm=None,
        )
        out_path = jsonl_path.with_name(jsonl_path.stem + ".task_kpi.json")
        out_path.write_text(_json.dumps({
            "task_id": getattr(recorder, "task_id", ""),
            "outcome": outcome,
            "metrics": {
                "iterations": metrics.iterations,
                "tool_calls": metrics.tool_calls,
                "llm_calls": metrics.llm_calls,
                "tokens_in": metrics.tokens_in,
                "tokens_out": metrics.tokens_out,
                "usd": metrics.usd,
                "wall_s": metrics.wall_s,
                "plan_steps_completed": metrics.plan_steps_completed,
                "plan_steps_total": metrics.plan_steps_total,
            },
            "attribution": {
                "primary": attribution.primary,
                "rationale": attribution.rationale,
                "layers": [
                    {"layer": l.layer, "status": l.status, "evidence": l.evidence}
                    for l in attribution.layers
                ],
                "method": attribution.method,
            },
        }, indent=2))


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
        model_name: str = os.environ.get("CAT_MODEL", "openrouter/deepseek/deepseek-v4-flash"),
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
