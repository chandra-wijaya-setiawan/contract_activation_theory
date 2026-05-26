"""Activator — the 8-step loop that drives contracts to completion.

The eight steps from DESIGN.md:
    1. INGEST    receive contract
    2. REPRESENT run bounded probes; establish verified facts before planning
    3. DECOMPOSE planner emits checklist of steps, grounded in representations
    4. EVALUATE  check budget + conditions
    5. SELECT    pick capability path (v1: always executor LLM with tools)
    6. ACTIVATE  executor emits one tool call → capability runs
    7. VERIFY    deterministic criteria + (optional) judge per step
    8. CLOSE     fulfilled / failed / suspended

This is NOT a `while True`. The loop is bounded by every Constraint on the
Contract; any violation transitions to FAILED with an explicit reason and
exits. There is no "model decides done" path — closure rests on
Criterion.predicate returning True (deterministic).
"""
from __future__ import annotations
import json
import re
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
from cat.representer import Representer
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
    criterion_fail_streak: int = 0   # F4: consecutive criterion FAILs
    replan_count: int = 0            # F2/F3: replans issued (0=initial, 1=replanned, 2=free-form)
    free_form: bool = False          # F3: True once plan is dropped to single free-form step
    consecutive_nonwrite: int = 0    # W1: consecutive non-write tool calls (write resets it)
    prior_attempts: list[str] = field(default_factory=list)   # P1: compressed log of dropped turns
    _recent_turns: list[list[dict]] = field(default_factory=list)  # P1: raw recent turn message groups
    criterion_history: list[tuple[int, str]] = field(default_factory=list)  # (iter, evidence) for each FAIL


def _extract_turn_summary(msgs: list[dict], turn_num: int) -> str:
    """Compress one turn's messages to a single line for the prior-attempts log.

    Format: 't01 bash("pip install pandas") → ERR: pip not found'

    This is deterministic — no LLM call. We extract the first tool call's name +
    key argument, and the first line of the tool result (success or error).
    Returns "" if the turn has no tool call (e.g. executor said DONE).
    """
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
    return f"t{turn_num:02d} {tool_name}({repr(arg_preview)[:45]}) → {outcome}"


class Activator:
    """Drives a Contract through its lifecycle using one LLM and one CapabilityRegistry."""

    def __init__(
        self,
        llm: LLM,
        registry: CapabilityRegistry,
        recorder: Recorder,
        max_iterations: int = 40,
        max_criterion_fails: int = 12,    # F4: exit if criterion fails this many times in a row
        criterion_check_interval: int = 1, # P2: only probe criterion every N iterations
        window_size: int = 4,              # P1: raw turns to keep; older turns → prior_attempts log
    ):
        self.llm = llm
        self.registry = registry
        self.recorder = recorder
        self.max_iterations = max_iterations
        self.max_criterion_fails = max_criterion_fails
        self.criterion_check_interval = criterion_check_interval
        self.window_size = window_size

    # ── Public entry ────────────────────────────────────────────────────────

    def run(self, contract: Contract, state: State) -> ActivatorRun:
        """Run the 8-step loop on `contract` until terminal state. Returns the run record."""
        run = ActivatorRun(contract=contract, state=state)
        self._record_state(contract, ContractState.LATENT)

        # Step 1: INGEST — already done by caller passing in `contract`.

        # Step 2: REPRESENT — establish verified facts before any planning.
        representer = Representer(registry=self.registry, recorder=self.recorder)
        contract.representations = representer.represent(contract, state)

        # Post-REPRESENT: if a test oracle was discovered, inject it as the primary criterion.
        self._maybe_inject_oracle_criterion(contract)

        # Step 3: DECOMPOSE — planner receives obligation + representations.
        self._decompose(contract, run)

        # Step 4: EVALUATE (initial)
        if not self._evaluate(contract, state, run):
            return run

        # Move LATENT → ELIGIBLE → ACTIVE
        self._transition(contract, ContractState.ELIGIBLE, "conditions met")
        self._transition(contract, ContractState.ACTIVE, "selected for execution")

        self._init_executor_messages(contract, run)

        # Main loop — bounded by every constraint
        while contract.state == ContractState.ACTIVE:
            # Step 4 (per-iter): EVALUATE budget
            if not self._evaluate(contract, state, run):
                break

            if run.iterations >= self.max_iterations:
                self._transition(contract, ContractState.FAILED,
                                 f"max_iterations {self.max_iterations} reached")
                run.failure_reason = "max_iterations"
                break

            # Step 5: SELECT — v1 always uses the executor LLM with tools
            # Step 6: ACTIVATE (increments run.iterations)
            iter_start_len = len(run.messages)  # P1: mark start of this turn
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

            # Step 7 (post-activate): verify criteria on interval OR when all steps done.
            # Avoids running an expensive test-runner probe on every single iteration.
            all_steps_done = bool(run.plan) and all(s.complete for s in run.plan)
            should_check = (run.iterations % self.criterion_check_interval == 0) or all_steps_done
            criterion_checked = False
            if should_check:
                criterion_checked = True
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED, "all criteria satisfied")
                    break

                replan_threshold = max(1, self.max_criterion_fails // 2)
                if run.criterion_fail_streak >= replan_threshold:
                    if run.replan_count == 0:
                        # F2: first escape — call planner again with context of what was tried
                        self._do_replan(contract, run)
                    elif not run.free_form:
                        # F3: second escape — drop plan, run free like ReAct
                        self._enter_free_form(contract, run)
                    else:
                        # All escapes exhausted — give up
                        self._transition(
                            contract, ContractState.FAILED,
                            f"criterion_unreachable after replan+free-form ({run.criterion_fail_streak} fails)",
                        )
                        run.failure_reason = "criterion_unreachable"
                        break

            # Step 7 (judge): advisory — skip in free-form mode (no plan steps to evaluate)
            if not run.free_form:
                self._update_plan_checklist(contract, run)

            # P1: sliding window — compress the oldest turn into prior_attempts log
            turn_msgs = run.messages[iter_start_len:]
            self._prune_to_window(contract, run, turn_msgs)

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

    # ── Step 3: DECOMPOSE ───────────────────────────────────────────────────

    def _decompose(self, contract: Contract, run: ActivatorRun) -> None:
        steps = call_planner(
            self.llm,
            contract.obligation,
            representations=contract.representations or None,
        )
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

    # ── Step 4: EVALUATE budget ─────────────────────────────────────────────

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

    # ── Step 6: ACTIVATE one capability ─────────────────────────────────────

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

    # ── Action Realization (LIFE-Harness Layer 3) ──────────────────────────
    _MARKDOWN_FENCE_RE = re.compile(r"^```[\w]*\n?|```$", re.MULTILINE)

    def _realize_action(self, tool_name: str, args: dict, state: State) -> dict:
        args = dict(args)
        if tool_name in ("write", "read", "list", "str_replace"):
            path = args.get("path")
            if isinstance(path, str):
                args["path"] = path.replace("\\", "/").lstrip("./")
        if tool_name == "write":
            content = args.get("content")
            if isinstance(content, str) and content.startswith("```"):
                args["content"] = self._MARKDOWN_FENCE_RE.sub("", content).strip() + "\n"
        if tool_name == "bash":
            timeout = args.get("timeout_s")
            if isinstance(timeout, str):
                try:
                    args["timeout_s"] = float(timeout)
                except ValueError:
                    del args["timeout_s"]
        return args

    # ── Failure Attribution (LIFE-Harness taxonomy, priority-ordered) ─────
    @staticmethod
    def _attribute_tool_failure(result: ToolResult) -> str:
        out = result.output.lower()
        detail = result.detail or {}
        if detail.get("precondition_failed"):
            return "environment_contract"
        if any(s in out for s in ("missing", "unknown tool", "invalid", "json", "type")):
            return "action_realization"
        if any(s in out for s in ("not found", "permission", "escapes workdir", "timeout")):
            return "environment_contract"
        return "residual_reasoning"

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
        first["arguments"] = self._realize_action(first["name"], first["arguments"], state)
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

        if tool_result.is_error:
            layer = self._attribute_tool_failure(tool_result)
            self.recorder.append(TraceEvent(
                event_id=f"fail-{uuid.uuid4().hex[:6]}",
                parent_id=contract.id,
                ts=utcnow_iso(),
                kind="failure_attribution",
                payload={
                    "layer": layer,
                    "description": tool_result.output[:300],
                    "tool": first["name"],
                    "args": first["arguments"],
                    "iteration": run.iterations,
                },
            ))

        # W1: track write pressure — reset on any "write" tool call
        if first["name"] == "write":
            run.consecutive_nonwrite = 0
        else:
            run.consecutive_nonwrite += 1

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

    # ── Criterion history summary ───────────────────────────────────────────

    def _build_criterion_context(self, run: ActivatorRun) -> str:
        """Compact summary of every criterion FAIL so far.

        Passed into replan and free-form prompts so the planner/executor
        knows *why* the criterion kept failing, not just what tools were called.
        Format: one line per check — 'iter NN: <evidence>'
        """
        if not run.criterion_history:
            return ""
        lines = ["CRITERION FEEDBACK (why the goal was not yet satisfied):"]
        # Deduplicate consecutive identical evidence — long runs repeat the same message
        last_ev = ""
        repeat = 0
        for iter_num, ev in run.criterion_history:
            if ev == last_ev:
                repeat += 1
            else:
                if repeat > 0:
                    lines.append(f"  ... (same verdict repeated {repeat}x)")
                    repeat = 0
                lines.append(f"  iter {iter_num:02d}: {ev}")
                last_ev = ev
        if repeat > 0:
            lines.append(f"  ... (same verdict repeated {repeat}x)")
        lines.append(
            "Your NEXT plan MUST directly address these failure reasons. "
            "Do not repeat approaches that already failed."
        )
        return "\n".join(lines)

    # ── F2: Dynamic replan ──────────────────────────────────────────────────

    def _do_replan(self, contract: Contract, run: ActivatorRun) -> None:
        """Call the planner again with a summary of what was tried.

        Replaces run.plan with a fresh plan grounded in prior-attempts context.
        Resets the criterion_fail_streak so the executor gets a full budget.
        """
        from cat.contracts import Representation
        reps = list(contract.representations or [])
        crit_ctx = self._build_criterion_context(run)
        if run.prior_attempts or crit_ctx:
            tried = "\n".join(f"  {l}" for l in run.prior_attempts[-15:])
            evidence = ""
            if crit_ctx:
                evidence += crit_ctx + "\n\n"
            if tried:
                evidence += f"WHAT WAS TRIED (tool call log):\n{tried}"
            reps = [r for r in reps if r.key != "prior_attempts"]
            reps.append(Representation(
                key="prior_attempts",
                value="previous approach failed — generate a different plan",
                evidence=evidence.strip(),
            ))

        new_steps = call_planner(self.llm, contract.obligation, representations=reps)
        contract.cost.llm_calls += 1
        if not new_steps:
            return  # planner failed — keep current plan
        run.plan = [_PlanStep(id=s["id"], description=s["description"]) for s in new_steps]
        run.replan_count += 1
        run.criterion_fail_streak = 0

        self.recorder.append(TraceEvent(
            event_id=f"replan-{uuid.uuid4().hex[:6]}",
            parent_id=contract.id,
            ts=utcnow_iso(),
            kind="contract_decompose",
            payload={
                "contract_id": contract.id,
                "replan": run.replan_count,
                "steps": [{"id": s.id, "description": s.description} for s in run.plan],
            },
        ))

        # Rebuild executor messages with fresh plan + criterion feedback
        run.messages = [
            {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(contract, run)},
        ]
        context_parts = []
        if run.prior_attempts:
            log = "\n".join(f"  {l}" for l in run.prior_attempts[-20:])
            context_parts.append("PRIOR ATTEMPTS (do not repeat these approaches):\n" + log)
        if crit_ctx:
            context_parts.append(crit_ctx)
        if context_parts:
            run.messages.append({
                "role": "user",
                "content": "\n\n".join(context_parts),
            })

    def _enter_free_form(self, contract: Contract, run: ActivatorRun) -> None:
        """Drop the plan entirely — executor runs free like ReAct.

        Replaces the plan with a single open-ended step. No per-step judge.
        The criterion gate still applies; we just remove the plan structure
        so the executor is not constrained to a multi-step checklist.
        """
        run.free_form = True
        run.replan_count += 1
        run.criterion_fail_streak = 0
        run.plan = [_PlanStep(
            id="free",
            description="Satisfy the criterion by any means. No plan constraints.",
        )]

        self.recorder.append(TraceEvent(
            event_id=f"freeform-{uuid.uuid4().hex[:6]}",
            parent_id=contract.id,
            ts=utcnow_iso(),
            kind="contract_decompose",
            payload={
                "contract_id": contract.id,
                "replan": run.replan_count,
                "free_form": True,
                "steps": [{"id": "free", "description": "free-form: satisfy criterion"}],
            },
        ))

        prior_log = "\n".join(f"  {l}" for l in run.prior_attempts[-20:]) if run.prior_attempts else "(none)"
        crit_ctx = self._build_criterion_context(run)
        run.messages = [
            {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"OBLIGATION:\n{contract.obligation}\n\n"
                "MODE: FREE-FORM — structured plans have not satisfied the criterion. "
                "Act freely: read, write, run whatever is needed. "
                "The criterion will tell you when you succeed. No checklist constraints.\n\n"
                f"PRIOR ATTEMPTS (do not repeat):\n{prior_log}\n\n"
                + (f"{crit_ctx}\n\n" if crit_ctx else "")
                + f"BUDGET:\n{self._format_budget(contract)}\n\n"
                "Take ONE action now."
            )},
        ]

    # ── Oracle criterion injection ───────────────────────────────────────────

    def _maybe_inject_oracle_criterion(self, contract: Contract) -> None:
        """If REPRESENT found a test oracle and no oracle criterion exists, inject one.

        Oracle criteria are authoritative: _verify_criteria runs only oracle criteria
        when any are present, skipping LLM-judge criteria entirely. This ensures the
        contract closes on real test results, not on model opinion.
        """
        if any(c.is_oracle for c in contract.criteria):
            return  # oracle already set (e.g. by benchmarks.swe_bench.TbSweAgent)

        strategy_rep = next(
            (r for r in (contract.representations or []) if r.key == "judge_strategy"),
            None,
        )
        if strategy_rep is None or not strategy_rep.value.startswith("oracle:"):
            return

        oracle_cmd = strategy_rep.value[len("oracle:"):].strip()
        if not oracle_cmd:
            return

        contract.criteria.insert(0, self._build_oracle_criterion(oracle_cmd))

    def _build_oracle_criterion(self, command: str) -> "Criterion":
        """Wrap a shell command as an authoritative oracle Criterion.

        Passes when the command exits 0 (is_error=False from the bash handler).
        Works in both local (subprocess) and tmux (terminal-bench) modes because
        both bash handlers set is_error based on exit code.
        """
        from cat.contracts import Criterion, Evidence
        registry = self.registry
        recorder = self.recorder

        def _predicate(state: State) -> tuple[bool, Evidence]:
            result = registry.invoke(
                name="bash",
                args={"command": command, "timeout_s": 120},
                state=state,
                recorder=recorder,
                contract_id="oracle-check",
                tool_call_id=f"oracle-{uuid.uuid4().hex[:6]}",
            )
            passed = not result.is_error
            summary = ("PASS" if passed else "FAIL") + f": oracle ({command[:60]})"
            return passed, Evidence(
                summary=summary,
                detail={"command": command, "output": result.output[:800]},
            )

        return Criterion(
            id=f"oracle_{uuid.uuid4().hex[:8]}",
            description=f"oracle test passes: {command[:100]}",
            predicate=_predicate,
            is_oracle=True,
        )

    # ── Step 7: VERIFY ──────────────────────────────────────────────────────

    def _verify_criteria(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """Run criteria. Oracle criteria are authoritative; LLM criteria are fallback.

        If any criterion has is_oracle=True, ONLY oracle criteria are evaluated —
        LLM-judge criteria are skipped entirely. This prevents a lenient LLM from
        closing a contract when the real tests still fail.
        """
        if not contract.criteria:
            return False

        oracle_crits = [c for c in contract.criteria if c.is_oracle]
        crits_to_run = oracle_crits if oracle_crits else contract.criteria

        all_ok = True
        for crit in crits_to_run:
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
                    "is_oracle": crit.is_oracle,
                },
            ))
            if not ok:
                all_ok = False

        # F4: maintain consecutive-failure streak for criterion_unreachable detection
        if all_ok:
            run.criterion_fail_streak = 0
        else:
            run.criterion_fail_streak += 1
            # Record the evidence from each failure so replans can learn from it.
            for crit in crits_to_run:
                if not crit.satisfied and crit.last_evidence:
                    evidence = crit.last_evidence.summary[:200]
                    run.criterion_history.append((run.iterations, evidence))
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
        # W1: inject write-pressure warning if executor has been reading without writing
        write_warning = ""
        if run.consecutive_nonwrite >= 5:
            write_warning = (
                f"\n\n⚠️  WRITE PRESSURE ({run.consecutive_nonwrite} consecutive non-write actions): "
                "You have spent your last several turns reading code without writing any changes. "
                "Your VERY NEXT action MUST be a write tool call. "
                "Do NOT read any more code. Write your best fix now — the criterion will verify correctness."
            )
        run.messages.append({
            "role": "user",
            "content": (
                "CHECKLIST UPDATE:\n"
                f"{self._format_checklist(run)}\n\n"
                f"BUDGET:\n{self._format_budget(contract)}\n\n"
                "Take ONE action toward the first un-checked step, or reply DONE only if all are checked."
                f"{write_warning}"
            ),
        })

    def _prune_to_window(
        self, contract: Contract, run: ActivatorRun, turn_msgs: list[dict]
    ) -> None:
        """P1: Sliding window context management.

        After each complete iteration, record the turn's messages. When the
        window overflows, drop the oldest raw turn and compress it to a one-line
        entry in run.prior_attempts. Rebuild run.messages from:

            [system]
            [user: fresh obligation + current checklist + budget]
            [user: PRIOR ATTEMPTS log]   ← only if non-empty
            [last window_size raw turns]

        The fresh user prompt replaces the stale initial prompt (which still shows
        the original checklist). The prior_attempts log gives the executor negative
        knowledge ("don't try pip install — it failed") without the token cost of
        the full tool output.

        Executor token cost: O(window * avg_turn_tokens + prior_log) instead of O(N).
        """
        if self.window_size <= 0 or not turn_msgs:
            return

        run._recent_turns.append(list(turn_msgs))  # snapshot

        if len(run._recent_turns) <= self.window_size:
            return  # still within window, nothing to prune

        # Drop oldest turn → compress to prior_attempts
        dropped = run._recent_turns.pop(0)
        summary = _extract_turn_summary(dropped, len(run.prior_attempts) + 1)
        if summary:
            run.prior_attempts.append(summary)

        # Rebuild message thread from structured state
        new_msgs: list[dict] = [
            {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
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
        # Terminal-bench mode: use live pane capture instead of local workdir snapshot.
        terminal_output = run.state.kv.get("terminal_output")
        if terminal_output:
            snapshot = f"TERMINAL PANE (last capture):\n{terminal_output[:3000]}"
        else:
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
