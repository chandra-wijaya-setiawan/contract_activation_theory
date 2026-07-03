"""TbCatAgent — terminal-bench base class for CAT agents."""
from __future__ import annotations
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from terminal_bench.agents.base_agent import AgentResult as TbResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

from cat.agent.hypothesis import Activator
from cat.capability.full_registry import build_docker_registry, build_tmux_registry
from cat.contract.model import Constraints, Contract, Criterion, Evidence, State
from cat.llm import LLM, call_judge
from cat.trace import Recorder, TraceEvent


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


def _make_terminal_criterion(llm: LLM, instruction: str) -> Criterion:
    def predicate(state: State) -> tuple[bool, Evidence]:
        terminal_output = state.kv.get("terminal_output", "(no terminal output yet)")
        summary = f"TERMINAL PANE (last capture):\n{terminal_output[:3000]}"
        complete, reason = call_judge(llm, instruction, summary)
        return complete, Evidence(summary=reason, detail={"judge": "terminal"})
    return Criterion(id="terminal-judge", description=instruction[:120], predicate=predicate)


def _make_test_runner_criterion(session: TmuxSession, test_cmd: str) -> Criterion:
    def predicate(state: State) -> tuple[bool, Evidence]:
        try:
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


class TbCatAgent(BaseAgent):
    """CAT agent base for terminal-bench. Subclasses override _resolve_test_cmd
    and _initial_representations to provide task-specific oracle setup."""

    @staticmethod
    def name() -> str:
        return "cat-tb"

    def __init__(
        self,
        model_name: str = os.environ.get("CAT_MODEL", "openrouter/deepseek/deepseek-v4-flash"),
        max_iterations: int = 30,
        criterion_check_interval: int = 1,
        window_size: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model_name = model_name
        self._max_iterations = max_iterations
        self._criterion_check_interval = criterion_check_interval
        self._window_size = window_size

    def _use_tmux_backend(self) -> bool:
        return False

    def _resolve_test_cmd(self, logging_dir: Optional[Path]) -> str:
        return ""

    def _initial_representations(self, session: TmuxSession, criterion: Criterion,
                                 state: State) -> list:
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
                max_wall_s=2700.0,
                max_usd=5.0,
            )
            contract = Contract.new(
                obligation=instruction,
                criteria=[criterion],
                constraints=constraints,
            )
            state = State(workdir=workdir)

            pre_reps = self._initial_representations(session, criterion, state)
            if pre_reps:
                contract.representations = pre_reps

            librarian = None
            corpus_db = os.environ.get("CAT_CORPUS_DB", "").strip()
            if corpus_db:
                try:
                    from cat._legacy.librarian import Librarian
                    librarian = Librarian(corpus_db)
                    librarian.refresh()
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: librarian init failed ({exc}); continuing without advisor")
                    librarian = None

            librarian_writer = None
            plan_db = os.environ.get("CAT_LIBRARIAN_V2_DB", "").strip()
            if plan_db:
                try:
                    from cat.librarian import LibrarianV2
                    librarian_writer = LibrarianV2(plan_db)
                except Exception as exc:
                    print(f"warning: plan-tree librarian init failed ({exc}); continuing")
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
            try:
                self._emit_task_kpi(recorder, contract)
            except Exception:  # noqa: BLE001
                pass

        tokens_in, tokens_out = _recorder_totals(recorder)
        return TbResult(
            total_input_tokens=tokens_in,
            total_output_tokens=tokens_out,
            failure_mode=FailureMode.NONE,
        )

    def _emit_task_kpi(self, recorder: "Recorder", contract: "Contract") -> None:
        import json as _json
        from cat.metrics import RunMetrics
        from cat.attribution import attribute_task

        jsonl_path = recorder.jsonl_path
        metrics = RunMetrics.from_jsonl(jsonl_path)
        events = [ev.model_dump() for ev in recorder.traj.events]
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


# TbReactAgent kept for backward-compat imports
TbReactAgent = TbCatAgent

__all__ = ["TbCatAgent", "TbReactAgent"]
