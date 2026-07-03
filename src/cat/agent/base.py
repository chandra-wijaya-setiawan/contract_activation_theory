"""BaseActivator — scaffolding for all activator implementations.

Provides the shared machinery that every loop variant needs:
  - Cost accounting (_charge_llm, _charge_tool)
  - State machine transitions (_transition, _record_state, _finalize)
  - Budget enforcement (_check_budget)
  - Criteria verification (_verify_criteria)
  - Admission gate (_admit, _apply_admission_failure)
  - Message window management (_prune_to_window)
  - Oracle criterion injection (_maybe_inject_oracle_criterion)

Subclasses override run() to implement their specific loop strategy.

See design.v3.md §"Roles" and plan.v3.md Phase 1.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from cat.capability.registry import CapabilityRegistry, ToolResult
from cat.contract.model import (
    AdmissionDecision,
    Contract,
    ContractState,
    Cost,
    Criterion,
    Evidence,
    State,
)
from cat.llm.client import LLM, LLMResult
from cat.trace import Recorder, TraceEvent
from cat.utils.time import utcnow_iso


PERFORMER_SYSTEM_PROMPT = """\
You are the PERFORMER role in a contract-disciplined agent.
...
"""


@dataclass
class _PlanStep:
    id: str
    description: str
    complete: bool = False
    last_reason: str = ""


@dataclass
class ActivatorRun:
    """Mutable state for one contract discharge attempt."""
    contract: Contract
    state: State
    plan: list[_PlanStep] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    iterations: int = 0
    final_text: Optional[str] = None
    failure_reason: Optional[str] = None
    suspend_reason: Optional[str] = None
    prior_attempts: list[str] = field(default_factory=list)
    grant: set[str] = field(default_factory=set)
    last_state_event_id: str = ""
    last_perform_event_id: str = ""
    last_stipulate_event_id: str = ""
    last_admit_event_id: str = ""
    last_iter_anchor: str = ""
    _recent_turns: list = field(default_factory=list)


class Agent:
    """Scaffolding for contract discharge loops. Subclass and override run()."""

    def __init__(
        self,
        llm: LLM,
        registry: CapabilityRegistry,
        recorder: Recorder,
        max_iterations: int = 40,
        criterion_check_interval: int = 1,
        window_size: int = 4,
        **kwargs,  # absorb legacy kwargs (librarian, etc.) without breaking
    ):
        self.llm = llm
        self.registry = registry
        self.recorder = recorder
        self.max_iterations = max_iterations
        self.criterion_check_interval = criterion_check_interval
        self.window_size = window_size

    # ── Abstract ──────────────────────────────────────────────────────────

    def run(self, contract: Contract, state: State) -> ActivatorRun:
        """Run the discharge loop. Override in subclasses."""
        raise NotImplementedError

    # ── Cost accounting ──────────────────────────────────────────────────

    def _charge_llm(self, contract: Contract, result: LLMResult) -> None:
        contract.cost.tokens_in += result.tokens_in
        contract.cost.tokens_out += result.tokens_out
        contract.cost.usd += result.usd
        contract.cost.llm_calls += 1

    def _charge_tool(self, contract: Contract, result: ToolResult) -> None:
        contract.cost.tool_calls += 1

    # ── State machine ────────────────────────────────────────────────────

    def _transition(
        self, contract: Contract, to: ContractState, reason: str,
        run: Optional[ActivatorRun] = None,
    ) -> None:
        last_ev = self.recorder.traj.events[-1].event_id if self.recorder.traj.events else None
        if not contract.can_transition(to):
            self.recorder.append(TraceEvent(
                event_id=f"state-{uuid.uuid4().hex[:6]}",
                parent_id=last_ev, ts=utcnow_iso(), kind="contract_state",
                payload={"contract_id": contract.id, "from": contract.state.value,
                         "to": to.value, "rejected": True, "reason": reason},
            ))
            return
        prev = contract.state
        contract.state = to
        contract.state_reason = reason
        event_id = f"state-{uuid.uuid4().hex[:6]}"
        self.recorder.append(TraceEvent(
            event_id=event_id, parent_id=last_ev, ts=utcnow_iso(),
            kind="contract_state",
            payload={"contract_id": contract.id, "from": prev.value,
                     "to": to.value, "reason": reason},
        ))
        if run is not None:
            run.last_state_event_id = event_id

    def _record_state(self, contract: Contract, state: ContractState, run: ActivatorRun) -> None:
        event_id = f"state-init-{contract.id}"
        self.recorder.append(TraceEvent(
            event_id=event_id, parent_id=None, ts=utcnow_iso(),
            kind="contract_state",
            payload={"contract_id": contract.id, "from": None,
                     "to": state.value, "reason": "ingested",
                     "obligation": contract.obligation},
        ))
        run.last_state_event_id = event_id

    def _finalize(self, contract: Contract, run: ActivatorRun) -> None:
        self.recorder.append(TraceEvent(
            event_id=f"step-{uuid.uuid4().hex[:6]}",
            parent_id=run.last_state_event_id or None,
            ts=utcnow_iso(), kind="step_end",
            payload={"iterations": run.iterations,
                     "final_state": contract.state.value,
                     "cost": self._cost_dict(contract.cost)},
        ))

    @staticmethod
    def _cost_dict(cost: Cost) -> dict:
        return {
            "tokens_in": cost.tokens_in, "tokens_out": cost.tokens_out,
            "tokens_total": cost.tokens_total, "tool_calls": cost.tool_calls,
            "llm_calls": cost.llm_calls, "usd": round(cost.usd, 5),
        }

    # ── Budget ───────────────────────────────────────────────────────────

    def _check_budget(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        now = time.time()
        violation = contract.constraints.check(contract.cost, contract.depth, now)
        if violation:
            self._transition(contract, ContractState.FAILED, f"budget violation: {violation}")
            run.failure_reason = violation
            return False
        return True

    # ── Criteria ─────────────────────────────────────────────────────────

    def _verify_criteria(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        if not contract.criteria:
            return False
        oracle_crits = [c for c in contract.criteria if c.is_oracle]
        crits_to_run = oracle_crits if oracle_crits else contract.criteria
        all_ok = True
        for crit in crits_to_run:
            ok = crit.check(state)
            crit_parent = (
                self.recorder.traj.events[-1].event_id
                if self.recorder.traj.events else run.last_perform_event_id
            )
            event_id = f"crit-{crit.id}-{uuid.uuid4().hex[:4]}"
            self.recorder.append(TraceEvent(
                event_id=event_id, parent_id=crit_parent, ts=utcnow_iso(),
                kind="criterion_check",
                payload={"contract_id": contract.id, "criterion_id": crit.id,
                         "description": crit.description, "satisfied": ok,
                         "evidence": crit.last_evidence.summary if crit.last_evidence else None,
                         "iteration": run.iterations, "is_oracle": crit.is_oracle},
            ))
            run.last_iter_anchor = event_id
            if not ok:
                all_ok = False
        return all_ok

    def _maybe_inject_oracle_criterion(self, contract: Contract) -> None:
        if any(c.is_oracle for c in contract.criteria):
            return
        strategy_rep = next(
            (r for r in (contract.representations or []) if r.key == "judge_strategy"), None,
        )
        if strategy_rep is None or not strategy_rep.value.startswith("oracle:"):
            return
        oracle_cmd = strategy_rep.value[len("oracle:"):].strip()
        if not oracle_cmd:
            return
        contract.criteria.insert(0, self._build_oracle_criterion(contract, oracle_cmd))

    def _build_oracle_criterion(self, contract: Contract, command: str) -> Criterion:
        from cat.contract.model import Criterion, Evidence
        registry = self.registry
        recorder = self.recorder
        charge_tool = self._charge_tool

        def _predicate(state: State) -> tuple[bool, Evidence]:
            parent = recorder.traj.events[-1].event_id if recorder.traj.events else None
            result = registry.invoke(
                name="bash", args={"command": command, "timeout_s": 120},
                state=state, recorder=recorder, contract_id="oracle-check",
                tool_call_id=f"oracle-{uuid.uuid4().hex[:6]}", parent_event_id=parent,
            )
            charge_tool(contract, result)
            passed = not result.is_error
            summary = ("PASS" if passed else "FAIL") + f": oracle ({command[:60]})"
            return passed, Evidence(
                summary=summary,
                detail={"command": command, "output": result.output[:800]},
            )

        return Criterion(
            id=f"oracle_{uuid.uuid4().hex[:8]}",
            description=f"oracle test passes: {command[:100]}",
            predicate=_predicate, is_oracle=True,
        )

    # ── Admission ─────────────────────────────────────────────────────────

    def _admit(self, contract: Contract, state: State, run: ActivatorRun) -> AdmissionDecision:

        if not (contract.obligation or "").strip():
            return AdmissionDecision.reject("boundary", "empty obligation")
        if not contract.criteria:
            return AdmissionDecision.reject("boundary", "no criteria declared")
        if not run.plan:
            return AdmissionDecision.reject("boundary", "empty plan from DECOMPOSE")
        cons = contract.constraints
        if cons.max_tokens_total <= 0 or cons.max_tool_calls <= 0 or cons.max_llm_calls <= 0:
            return AdmissionDecision.reject("boundary", "non-positive constraint bounds")
        now = time.time()
        if cons.deadline_ts is not None and now > cons.deadline_ts:
            return AdmissionDecision.reject("boundary", "deadline already passed")
        if not contract.conditions_met(state):
            return AdmissionDecision.reject("environment", "declared conditions not met")
        for cap_name in contract.capabilities or []:
            if self.registry.get(cap_name) is None:
                return AdmissionDecision.reject("environment", f"capability '{cap_name}' not in registry")
        violation = cons.check(contract.cost, contract.depth, now)
        if violation:
            return AdmissionDecision.reject("capacity", violation)
        grant = set(self.registry.names())
        if contract.constraints.deadline_ts is None and contract.constraints.max_wall_s > 0:
            contract.constraints.deadline_ts = time.time() + contract.constraints.max_wall_s
        return AdmissionDecision.admit(grant)

    def _apply_admission_failure(
        self, contract: Contract, run: ActivatorRun, decision: AdmissionDecision,
    ) -> None:
        response = contract.policy.response_for(decision.failure_type)
        reason = f"admission_{decision.failure_type}: {decision.reason}"
        if response == "FAIL":
            self._transition(contract, ContractState.FAILED, reason)
            run.failure_reason = reason
        elif response == "SUSPEND":
            self._transition(contract, ContractState.SUSPENDED, reason)
            run.suspend_reason = reason
            contract.suspend_reason = reason
            contract.re_entry_condition = decision.reason
        elif response == "ESCALATE":
            self._transition(contract, ContractState.FAILED, f"escalate: {reason}")
            run.failure_reason = f"escalate: {reason}"
        elif response == "ASK_USER":
            self._transition(contract, ContractState.SUSPENDED, f"ask_user: {reason}")
            run.suspend_reason = f"ask_user: {reason}"
            contract.suspend_reason = run.suspend_reason
            contract.re_entry_condition = "user input"

    def _record_admission(
        self, contract: Contract, decision: AdmissionDecision, run: ActivatorRun,
    ) -> None:
        event_id = f"admit-{uuid.uuid4().hex[:6]}"
        self.recorder.append(TraceEvent(
            event_id=event_id,
            parent_id=run.last_stipulate_event_id or None,
            ts=utcnow_iso(), kind="contract_admit",
            payload={"contract_id": contract.id, "ok": decision.ok,
                     "failure_type": decision.failure_type,
                     "reason": decision.reason,
                     "grant": sorted(decision.grant) if decision.ok else []},
        ))
        run.last_admit_event_id = event_id

    # ── Messages ──────────────────────────────────────────────────────────

    def _init_executor_messages(self, contract: Contract, run: ActivatorRun) -> None:
        run.messages = [
            {"role": "system", "content": PERFORMER_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(contract, run)},
        ]

    def _build_user_prompt(self, contract: Contract, run: ActivatorRun) -> str:
        checklist = self._format_checklist(run)
        budget = self._format_budget(contract)
        return (
            f"OBLIGATION:\n{contract.obligation}\n\n"
            f"PLAN (checklist):\n{checklist}\n\n"
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
            f"usd: ${c.usd:.4f}/{b.max_usd:.2f}"
        )

    def _prune_to_window(
        self, contract: Contract, run: ActivatorRun, turn_msgs: list[dict],
    ) -> None:
        """P1: sliding window. Drop oldest raw turn -> compressed prior_attempts."""
        if self.window_size <= 0 or not turn_msgs:
            return
        run._recent_turns.append(list(turn_msgs))
        if len(run._recent_turns) <= self.window_size:
            return
        dropped = run._recent_turns.pop(0)
        summary = self._extract_turn_summary(dropped, len(run.prior_attempts) + 1)
        if summary:
            run.prior_attempts.append(summary)
        new_msgs: list[dict] = [
            {"role": "system", "content": PERFORMER_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(contract, run)},
        ]
        if run.prior_attempts:
            log = "\n".join(f"  {line}" for line in run.prior_attempts[-20:])
            new_msgs.append({
                "role": "user",
                "content": "PRIOR ATTEMPTS (do not repeat these approaches):\n" + log,
            })
        for turn in run._recent_turns:
            new_msgs.extend(turn)
        run.messages = new_msgs

    @staticmethod
    def _extract_turn_summary(msgs: list[dict], turn_num: int) -> str:
        """Compress one turn's messages to a single line."""
        tool_name = arg_preview = outcome = ""
        for msg in msgs:
            role = msg.get("role", "")
            if role == "assistant" and not tool_name:
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "?")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if "command" in args:
                        arg_preview = str(args["command"])[:55]
                    elif "path" in args:
                        arg_preview = str(args["path"])[:40]
                    else:
                        first_val = next(iter(args.values()), "")
                        arg_preview = str(first_val)[:40]
                    break
            elif role == "tool" and not outcome:
                content = str(msg.get("content", ""))
                if content.startswith("[ERROR]"):
                    outcome = "ERR: " + content[7:].strip().split("\n")[0][:75]
                else:
                    first_line = content.strip().split("\n")[0][:75]
                    outcome = "OK: " + first_line
        if not tool_name:
            return ""
        return f"t{turn_num:02d} {tool_name}({repr(arg_preview)[:45]}) -> {outcome}"
BaseActivator = Agent
