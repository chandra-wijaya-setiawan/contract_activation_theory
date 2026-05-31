"""Agent entry points: CatAgent (contract-disciplined) and ReactAgent (control).

Both implement the same `Agent` protocol, share the same LLM + Capability stack,
and emit into the same TraceEvent schema. The only difference is the loop:

    CatAgent    — Activator runs the 7-step contract loop with planner + judge +
                  deterministic criterion checks + mid-run budget enforcement.

    ReactAgent  — Pure ReAct: one LLM in a while-loop with tool-calling, no plan,
                  no judge, no criterion gate. Loop terminates on the model
                  returning text without tool calls OR on hitting max_iterations.
                  This is the apples-to-apples control we compare CAT against.
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from cat._legacy.activator import Activator
from cat.capabilities import CapabilityRegistry, build_default_registry
from cat.contracts import (
    Constraints,
    Contract,
    ContractState,
    Criterion,
    Evidence,
    State,
)
from cat.llm import LLM
from cat.tasks import TaskSpec, grade as synthetic_grade
from cat.trace import Recorder, TraceEvent, utcnow_iso


@dataclass
class AgentResult:
    finished_cleanly: bool
    verdict: str           # "solved" | "failed" | "error" | "timeout" | "over_budget"
    final_text: Optional[str] = None
    failure_reason: Optional[str] = None
    iterations: int = 0


class Agent(Protocol):
    name: str
    def run(self, task: TaskSpec, recorder: Recorder, workdir: Path) -> AgentResult: ...


# ── Criterion builder from a synthetic oracle ───────────────────────────────


def _criterion_from_oracle(task: TaskSpec) -> Criterion:
    """Wrap a registered synthetic oracle as a deterministic CAT Criterion."""
    def predicate(state: State) -> tuple[bool, Evidence]:
        ok, score = synthetic_grade(task.task_id, state.workdir)
        return ok, Evidence(
            summary=("PASS" if ok else "FAIL") + f" oracle={task.task_id} score={score}",
            detail={"oracle": task.task_id, "score": score},
        )
    return Criterion(id=f"oracle-{task.task_id}", description=f"oracle({task.task_id}) returns PASS", predicate=predicate)


# ── CatAgent ─────────────────────────────────────────────────────────────────


class CatAgent:
    name = "cat-0.0.1"

    def __init__(
        self,
        model: str = "openrouter/deepseek/deepseek-v4-flash",
        registry: Optional[CapabilityRegistry] = None,
        max_iterations: int = 40,
    ):
        self.model = model
        self.registry = registry or build_default_registry()
        self.max_iterations = max_iterations

    def run(self, task: TaskSpec, recorder: Recorder, workdir: Path) -> AgentResult:
        workdir.mkdir(parents=True, exist_ok=True)
        recorder.append(TraceEvent(
            event_id=f"sess-{uuid.uuid4().hex[:6]}",
            ts=utcnow_iso(),
            kind="session_start",
            payload={
                "agent": self.name,
                "model": self.model,
                "task_id": task.task_id,
                "cwd": str(workdir),
                "budget": task.budget.model_dump(),
            },
        ))
        recorder.append(TraceEvent(
            event_id=f"user-{uuid.uuid4().hex[:6]}",
            ts=utcnow_iso(),
            kind="user_message",
            payload={"text": task.instruction},
        ))

        llm = LLM(model=self.model, recorder=recorder)
        constraints = Constraints(
            max_tokens_total=task.budget.tokens_total,
            max_tool_calls=task.budget.tool_calls,
            max_llm_calls=max(3 * task.budget.tool_calls, 30),  # planner+judge per loop
            max_wall_s=float(task.budget.wall_s),
            max_usd=task.budget.usd,
            deadline_ts=time.time() + task.budget.wall_s,
        )
        criterion = _criterion_from_oracle(task)
        contract = Contract.new(
            obligation=task.instruction,
            criteria=[criterion],
            constraints=constraints,
        )
        state = State(workdir=workdir)

        activator = Activator(llm=llm, registry=self.registry, recorder=recorder,
                              max_iterations=self.max_iterations)
        t0 = time.time()
        run = activator.run(contract, state)
        wall = time.time() - t0

        verdict = _verdict_from_contract_state(contract.state)
        if wall > task.budget.wall_s:
            verdict = "timeout"
        if contract.cost.usd > task.budget.usd and verdict != "solved":
            verdict = "over_budget"

        recorder.append(TraceEvent(
            event_id=f"sessend-{uuid.uuid4().hex[:6]}",
            ts=utcnow_iso(),
            kind="session_end",
            payload={
                "verdict": verdict,
                "contract_state": contract.state.value,
                "iterations": run.iterations,
                "failure_reason": run.failure_reason,
                "wall_s": round(wall, 2),
            },
        ))
        return AgentResult(
            finished_cleanly=contract.state == ContractState.FULFILLED,
            verdict=verdict,
            final_text=run.final_text,
            failure_reason=run.failure_reason,
            iterations=run.iterations,
        )


def _verdict_from_contract_state(s: ContractState) -> str:
    if s == ContractState.FULFILLED:
        return "solved"
    if s == ContractState.EXPIRED:
        return "timeout"
    return "failed"


# ── ReactAgent (control) ─────────────────────────────────────────────────────


REACT_SYSTEM_PROMPT = """\
You are a coding agent with bash, write, read, and list tools. Complete the user's \
task using the tools, then reply with a short confirmation when done. Be terse.
"""


class ReactAgent:
    name = "react-0.0.1"

    def __init__(
        self,
        model: str = "openrouter/deepseek/deepseek-v4-flash",
        registry: Optional[CapabilityRegistry] = None,
        max_iterations: int = 40,
    ):
        self.model = model
        self.registry = registry or build_default_registry()
        self.max_iterations = max_iterations

    def run(self, task: TaskSpec, recorder: Recorder, workdir: Path) -> AgentResult:
        workdir.mkdir(parents=True, exist_ok=True)
        recorder.append(TraceEvent(
            event_id=f"sess-{uuid.uuid4().hex[:6]}",
            ts=utcnow_iso(),
            kind="session_start",
            payload={
                "agent": self.name,
                "model": self.model,
                "task_id": task.task_id,
                "cwd": str(workdir),
                "budget": task.budget.model_dump(),
            },
        ))
        recorder.append(TraceEvent(
            event_id=f"user-{uuid.uuid4().hex[:6]}",
            ts=utcnow_iso(),
            kind="user_message",
            payload={"text": task.instruction},
        ))

        llm = LLM(model=self.model, recorder=recorder)
        state = State(workdir=workdir)
        messages: list[dict] = [
            {"role": "system", "content": REACT_SYSTEM_PROMPT},
            {"role": "user", "content": task.instruction},
        ]
        tools = self.registry.tool_schemas()

        t0 = time.time()
        iterations = 0
        verdict = "failed"
        final_text: Optional[str] = None
        failure_reason: Optional[str] = None
        tokens_in = tokens_out = 0
        tool_calls_count = 0
        usd = 0.0

        while True:
            iterations += 1
            if iterations > self.max_iterations:
                failure_reason = f"max_iterations {self.max_iterations} reached"
                break
            if (time.time() - t0) > task.budget.wall_s:
                verdict = "timeout"
                failure_reason = "wall_s budget exceeded"
                break
            if usd > task.budget.usd:
                verdict = "over_budget"
                failure_reason = "usd budget exceeded"
                break
            if tool_calls_count > task.budget.tool_calls:
                verdict = "over_budget"
                failure_reason = "tool_calls budget exceeded"
                break
            if (tokens_in + tokens_out) > task.budget.tokens_total:
                verdict = "over_budget"
                failure_reason = "tokens_total budget exceeded"
                break

            result = llm.chat(
                messages=messages,
                tools=tools,
                tool_choice="auto",
                role_tag="react",
                temperature=0.2,
            )
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            usd += result.usd

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
                    tr = self.registry.invoke(
                        name=tc["name"],
                        args=tc["arguments"],
                        state=state,
                        recorder=recorder,
                        tool_call_id=tc["id"],
                    )
                    tool_calls_count += 1
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tr.output if not tr.is_error else f"[ERROR] {tr.output}",
                    })
                continue
            else:
                messages.append({"role": "assistant", "content": result.text or ""})
                final_text = result.text
                # ReAct closure: model stopped emitting tool calls. Grade with oracle.
                ok, _score = synthetic_grade(task.task_id, workdir)
                verdict = "solved" if ok else "failed"
                if not ok:
                    failure_reason = "model stopped but oracle failed"
                break

        wall = time.time() - t0
        recorder.append(TraceEvent(
            event_id=f"sessend-{uuid.uuid4().hex[:6]}",
            ts=utcnow_iso(),
            kind="session_end",
            payload={
                "verdict": verdict,
                "iterations": iterations,
                "failure_reason": failure_reason,
                "wall_s": round(wall, 2),
            },
        ))
        return AgentResult(
            finished_cleanly=(verdict == "solved"),
            verdict=verdict,
            final_text=final_text,
            failure_reason=failure_reason,
            iterations=iterations,
        )
