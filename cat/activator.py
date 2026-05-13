"""Activator — the 7-step loop that drives contracts to completion.

The seven steps from DESIGN.md:
    1. INGEST    receive contract
    2. DECOMPOSE planner emits checklist of steps
    3. EVALUATE  check budget + conditions
    4. SELECT    pick capability path (v1: always executor LLM with tools)
    5. ACTIVATE  executor emits one tool call → capability runs
    6. VERIFY    deterministic criteria + (optional) judge per step
    7. CLOSE     fulfilled / failed / suspended

This is NOT a `while True`. The loop is bounded by every Constraint on the
Contract; any violation transitions to FAILED with an explicit reason and
exits. There is no "model decides done" path — closure rests on
Criterion.predicate returning True (deterministic).
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cat.capabilities import CapabilityRegistry, ToolResult, workdir_snapshot
from cat.contracts import (
    Contract,
    ContractState,
    Cost,
    Criterion,
    State,
)
from cat.llm import LLM, call_judge, call_planner
from cat.trace import Recorder, TraceEvent, utcnow_iso


EXECUTOR_SYSTEM_PROMPT = """\
You are the EXECUTOR role in a contract-disciplined agent.

You will be given:
- the OBLIGATION you must fulfill
- an ordered PLAN of verifiable steps
- a CHECKLIST showing which plan steps are already complete
- the LAST TOOL RESULT (if any)
- a list of available TOOLS (use them by emitting tool_calls)
- the BUDGET STATUS

Rules:
1. Take exactly ONE concrete action per turn (one tool call) OR emit a brief text \
"DONE" only when every plan step is checked complete.
2. Address the FIRST un-checked step. Do not skip ahead.
3. Be terse. Do not narrate. Do not re-state the plan. Just act.
4. If a tool returns an error, diagnose and try a corrected action; do NOT repeat \
the same failing call.
5. NEVER claim a step is complete on your own — only the deterministic criteria \
and the judge close steps.
"""


@dataclass
class _PlanStep:
    id: str
    description: str
    complete: bool = False
    last_reason: str = ""


@dataclass
class ActivatorRun:
    contract: Contract
    state: State
    plan: list[_PlanStep] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    iterations: int = 0
    final_text: Optional[str] = None
    failure_reason: Optional[str] = None


class Activator:
    """Drives a Contract through its lifecycle using one LLM and one CapabilityRegistry."""

    def __init__(
        self,
        llm: LLM,
        registry: CapabilityRegistry,
        recorder: Recorder,
        max_iterations: int = 40,
    ):
        self.llm = llm
        self.registry = registry
        self.recorder = recorder
        self.max_iterations = max_iterations

    # ── Public entry ────────────────────────────────────────────────────────

    def run(self, contract: Contract, state: State) -> ActivatorRun:
        """Run the 7-step loop on `contract` until terminal state. Returns the run record."""
        run = ActivatorRun(contract=contract, state=state)
        self._record_state(contract, ContractState.LATENT)

        # Step 1: INGEST — already done by caller passing in `contract`.

        # Step 2: DECOMPOSE
        self._decompose(contract, run)

        # Step 3: EVALUATE (initial)
        if not self._evaluate(contract, state, run):
            return run

        # Move LATENT → ELIGIBLE → ACTIVE
        self._transition(contract, ContractState.ELIGIBLE, "conditions met")
        self._transition(contract, ContractState.ACTIVE, "selected for execution")

        self._init_executor_messages(contract, run)

        # Main loop — bounded by every constraint
        while contract.state == ContractState.ACTIVE:
            # Step 6 (early): VERIFY — if criteria already met, we're done.
            if self._verify_criteria(contract, state, run):
                self._transition(contract, ContractState.FULFILLED, "all criteria satisfied")
                break

            # Step 3 (per-iter): EVALUATE budget
            if not self._evaluate(contract, state, run):
                break

            if run.iterations >= self.max_iterations:
                self._transition(contract, ContractState.FAILED,
                                 f"max_iterations {self.max_iterations} reached")
                run.failure_reason = "max_iterations"
                break

            # Step 4: SELECT — v1 always uses the executor LLM with tools
            # Step 5: ACTIVATE (increments run.iterations)
            keep_going = self._activate_once(contract, state, run)
            if not keep_going:
                # Executor declared DONE or emitted no tool call.
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED,
                                     "executor stopped and criteria satisfied")
                else:
                    self._transition(contract, ContractState.FAILED,
                                     "executor stopped before criteria satisfied")
                    run.failure_reason = "executor_quit_early"
                break

            # Step 6 (post-activate): re-verify deterministic criteria immediately.
            if self._verify_criteria(contract, state, run):
                self._transition(contract, ContractState.FULFILLED, "all criteria satisfied")
                break

            # Step 6 (judge): advisory — update checklist using judge per un-checked step
            self._update_plan_checklist(contract, run)

        self.recorder.append(TraceEvent(
            event_id=f"step-{uuid.uuid4().hex[:6]}",
            parent_id=contract.id,
            ts=utcnow_iso(),
            kind="step_end",
            payload={
                "iterations": run.iterations,
                "final_state": contract.state.value,
                "cost": _cost_dict(contract.cost),
            },
        ))
        return run

    # ── Step 2: DECOMPOSE ───────────────────────────────────────────────────

    def _decompose(self, contract: Contract, run: ActivatorRun) -> None:
        steps = call_planner(self.llm, contract.obligation)
        if not steps:
            # Fall back to a single step that mirrors the obligation.
            steps = [{"id": "s1", "description": contract.obligation.strip().splitlines()[0][:200]}]
        run.plan = [_PlanStep(id=s["id"], description=s["description"]) for s in steps]
        contract.cost.llm_calls += 1
        self.recorder.append(TraceEvent(
            event_id=f"dec-{uuid.uuid4().hex[:6]}",
            parent_id=contract.id,
            ts=utcnow_iso(),
            kind="contract_decompose",
            payload={
                "contract_id": contract.id,
                "steps": [{"id": s.id, "description": s.description} for s in run.plan],
            },
        ))

    # ── Step 3: EVALUATE budget ─────────────────────────────────────────────

    def _evaluate(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """Return True if still eligible to continue; False otherwise (transitions to FAILED/EXPIRED)."""
        now = time.time()
        violation = contract.constraints.check(contract.cost, contract.depth, now)
        if violation:
            self._transition(contract, ContractState.FAILED, f"budget violation: {violation}")
            run.failure_reason = violation
            return False
        if not contract.conditions_met(state):
            # Should not happen for the root contract (no conditions), but safe to check
            self._transition(contract, ContractState.SUSPENDED, "conditions not met")
            return False
        return True

    # ── Step 5: ACTIVATE one capability ─────────────────────────────────────

    def _init_executor_messages(self, contract: Contract, run: ActivatorRun) -> None:
        run.messages = [
            {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(contract, run)},
        ]

    def _build_user_prompt(self, contract: Contract, run: ActivatorRun) -> str:
        checklist = self._format_checklist(run)
        budget = self._format_budget(contract)
        return (
            f"OBLIGATION:\n{contract.obligation}\n\n"
            f"PLAN (checklist — mark via judge, not yourself):\n{checklist}\n\n"
            f"BUDGET:\n{budget}\n\n"
            f"Begin. Take ONE action toward the first un-checked step."
        )

    def _format_checklist(self, run: ActivatorRun) -> str:
        if not run.plan:
            return "(empty)"
        lines = []
        for s in run.plan:
            mark = "[x]" if s.complete else "[ ]"
            reason = f"   ({s.last_reason})" if s.last_reason else ""
            lines.append(f"  {mark} {s.id}: {s.description}{reason}")
        return "\n".join(lines)

    def _format_budget(self, contract: Contract) -> str:
        c = contract.cost
        b = contract.constraints
        return (
            f"  tokens: {c.tokens_total}/{b.max_tokens_total}  "
            f"tool_calls: {c.tool_calls}/{b.max_tool_calls}  "
            f"llm_calls: {c.llm_calls}/{b.max_llm_calls}  "
            f"usd: ${c.usd:.4f}/${b.max_usd:.2f}"
        )

    def _activate_once(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """One executor turn: ask LLM for next action, run the tool call (if any).

        Increments run.iterations. Returns True if the loop should continue
        (a tool was called); False if the executor signaled DONE or emitted no
        tool call.
        """
        run.iterations += 1
        tools = self.registry.tool_schemas()
        result = self.llm.chat(
            messages=run.messages,
            tools=tools,
            tool_choice="auto",
            role_tag="executor",
            temperature=0.2,
        )
        contract.cost.tokens_in += result.tokens_in
        contract.cost.tokens_out += result.tokens_out
        contract.cost.usd += result.usd
        contract.cost.llm_calls += 1

        # Append assistant turn (tool_calls or text)
        if result.tool_calls:
            run.messages.append({
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
        else:
            run.messages.append({"role": "assistant", "content": result.text or ""})

        if not result.tool_calls:
            # No tool call. Treat any "DONE" text as a stop signal; criteria verify next.
            run.final_text = result.text
            return False

        # Discipline: only execute the FIRST tool call this turn.
        first = result.tool_calls[0]
        self.recorder.append(TraceEvent(
            event_id=f"act-{uuid.uuid4().hex[:6]}",
            parent_id=contract.id,
            ts=utcnow_iso(),
            kind="capability_activate",
            payload={
                "contract_id": contract.id,
                "tool": first["name"],
                "arguments": first["arguments"],
                "tool_call_id": first["id"],
                "iteration": run.iterations,
            },
        ))
        tool_result: ToolResult = self.registry.invoke(
            name=first["name"],
            args=first["arguments"],
            state=state,
            recorder=self.recorder,
            contract_id=contract.id,
            tool_call_id=first["id"],
        )
        contract.cost.tool_calls += 1

        # Feed the tool result back to the LLM thread.
        run.messages.append({
            "role": "tool",
            "tool_call_id": first["id"],
            "content": tool_result.output if not tool_result.is_error
            else f"[ERROR] {tool_result.output}",
        })

        # If there were extra parallel tool_calls in the assistant message, OpenAI
        # requires a matching tool message for each. Reply to ignored ones with
        # a stub so the thread stays valid for the next call.
        for skipped in result.tool_calls[1:]:
            run.messages.append({
                "role": "tool",
                "tool_call_id": skipped["id"],
                "content": "[skipped — discipline: one action per turn]",
            })

        return True

    # ── Step 6: VERIFY ──────────────────────────────────────────────────────

    def _verify_criteria(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """Run every Criterion predicate. Record each check. Return True iff all satisfied."""
        if not contract.criteria:
            return False
        all_ok = True
        for crit in contract.criteria:
            ok = crit.check(state)
            self.recorder.append(TraceEvent(
                event_id=f"crit-{crit.id}-{uuid.uuid4().hex[:4]}",
                parent_id=contract.id,
                ts=utcnow_iso(),
                kind="criterion_check",
                payload={
                    "contract_id": contract.id,
                    "criterion_id": crit.id,
                    "description": crit.description,
                    "satisfied": ok,
                    "evidence": crit.last_evidence.summary if crit.last_evidence else None,
                    "iteration": run.iterations,
                },
            ))
            if not ok:
                all_ok = False
        return all_ok

    def _update_plan_checklist(self, contract: Contract, run: ActivatorRun) -> None:
        """Ask the judge per un-checked step; persist verdicts on the plan."""
        if not run.plan:
            return
        summary = self._state_summary_for_judge(run)
        for step in run.plan:
            if step.complete:
                continue
            complete, reason = call_judge(self.llm, step.description, summary)
            contract.cost.llm_calls += 1
            step.complete = complete
            step.last_reason = reason[:140]
            # We don't have token harvest on the judge call separately at this
            # layer (LLM class already recorded llm_usage). Step status remains
            # advisory; the final close still rests on Criterion.predicate.
            self.recorder.append(TraceEvent(
                event_id=f"judge-{step.id}-{uuid.uuid4().hex[:4]}",
                parent_id=contract.id,
                ts=utcnow_iso(),
                kind="criterion_check",
                payload={
                    "contract_id": contract.id,
                    "step_id": step.id,
                    "description": step.description,
                    "complete": complete,
                    "reason": reason,
                    "judge": True,
                    "iteration": run.iterations,
                },
            ))
            # Only ask judge for the first un-checked step per turn — keeps token
            # cost bounded and matches the "address the first un-checked step"
            # discipline rule.
            break

        # Re-issue the user prompt so the executor sees the updated checklist
        # on the next turn. (We append a user message; the executor will
        # produce the next tool_call in response.)
        run.messages.append({
            "role": "user",
            "content": (
                "CHECKLIST UPDATE:\n"
                f"{self._format_checklist(run)}\n\n"
                f"BUDGET:\n{self._format_budget(contract)}\n\n"
                "Take ONE action toward the first un-checked step, or reply DONE only if all are checked."
            ),
        })

    def _state_summary_for_judge(self, run: ActivatorRun) -> str:
        last = run.state.last_tool_result or {}
        last_str = ""
        if last:
            last_str = (
                f"LAST_TOOL: {last.get('tool')}\n"
                f"LAST_ARGS: {json.dumps(last.get('args', {}))[:400]}\n"
                f"LAST_IS_ERROR: {last.get('is_error')}\n"
                f"LAST_OUTPUT (truncated):\n{(last.get('output') or '')[:1500]}\n"
            )
        snapshot = workdir_snapshot(run.state.workdir)
        return (
            f"{last_str}\n"
            f"WORKDIR SNAPSHOT:\n{snapshot}\n"
        )

    # ── State + cost machinery ──────────────────────────────────────────────

    def _transition(self, contract: Contract, to: ContractState, reason: str) -> None:
        if not contract.can_transition(to):
            # Forbidden transition — record as terminal failure rather than silent skip.
            self.recorder.append(TraceEvent(
                event_id=f"state-{uuid.uuid4().hex[:6]}",
                parent_id=contract.id,
                ts=utcnow_iso(),
                kind="contract_state",
                payload={
                    "contract_id": contract.id,
                    "from": contract.state.value,
                    "to": to.value,
                    "rejected": True,
                    "reason": f"transition forbidden: {reason}",
                },
            ))
            return
        prev = contract.state
        contract.state = to
        contract.state_reason = reason
        self.recorder.append(TraceEvent(
            event_id=f"state-{uuid.uuid4().hex[:6]}",
            parent_id=contract.id,
            ts=utcnow_iso(),
            kind="contract_state",
            payload={
                "contract_id": contract.id,
                "from": prev.value,
                "to": to.value,
                "reason": reason,
            },
        ))

    def _record_state(self, contract: Contract, state: ContractState) -> None:
        # Used once at run start to log the initial state.
        self.recorder.append(TraceEvent(
            event_id=f"state-init-{contract.id}",
            parent_id=None,
            ts=utcnow_iso(),
            kind="contract_state",
            payload={
                "contract_id": contract.id,
                "from": None,
                "to": state.value,
                "reason": "ingested",
                "obligation": contract.obligation,
            },
        ))


def _cost_dict(cost: Cost) -> dict:
    return {
        "tokens_in": cost.tokens_in,
        "tokens_out": cost.tokens_out,
        "tokens_total": cost.tokens_total,
        "tool_calls": cost.tool_calls,
        "llm_calls": cost.llm_calls,
        "usd": round(cost.usd, 5),
        "wall_s": round(cost.wall_s, 2),
    }
