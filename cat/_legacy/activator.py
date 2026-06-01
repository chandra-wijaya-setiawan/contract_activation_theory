"""Activator — the 8-step CAT loop (contract-vocabulary names per PRD 1.6.2.9).

The eight steps:

    1. INGEST      receive contract (LATENT)
    2. REPRESENT   bounded probes establish verified facts (no LLM)
    3. STIPULATE   stipulator emits ordered performance terms (1 LLM call)
    4. ACTIVATE    admission gate (one-shot):
                     boundary check
                     environment fit
                     capacity check
                     capability grant
                   → LATENT → ACTIVE on pass
                   → consult contract.policy on fail (FAIL / SUSPEND / ESCALATE / ASK_USER)
    5. SELECT      pick capability from grant (v1: fixed performer LLM)
    6. PERFORM     one LLM call → one tool call → one tool result
                   (performer LLM deliberates + carries out the performance act)
    7. VERIFY      run criterion; per-step attestor advises
    8. DISCHARGE   FULFILLED / SUSPENDED / FAILED / re-enter at 5

Steps 1-4 are activation. Steps 5-8 are discharge, looped under the grant
from Step 4 until terminal.

The loop is not a `while True`. It is bounded by every Constraint and by
the admission gate; criterion-unreachable transitions to SUSPENDED with a
reason, not to a streak-counted escape ladder.
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
    AdmissionDecision,
    Contract,
    ContractState,
    Cost,
    Criterion,
    State,
)
from cat.llm import LLM, LLMResult, call_attestor, call_stipulator
from cat.representer import Representer
from cat.trace import Recorder, TraceEvent, utcnow_iso


PERFORMER_SYSTEM_PROMPT = """\
You are the PERFORMER role in a contract-disciplined agent.

You will be given:
- the OBLIGATION you must fulfill
- an ordered PLAN of verifiable steps (the contract's stipulations)
- a CHECKLIST showing which plan steps are already attested complete
- the LAST TOOL RESULT (if any)
- a list of available TOOLS (use them by emitting tool_calls)
- the BUDGET STATUS

Rules:
1. EVERY TURN MUST CALL A TOOL. Your only role is to ACT — never reply with \
text alone, never describe what you "would do", never summarize. Acting is \
your contract. If you need information, call a search/read tool. If the step \
is to mutate, call a write tool. If the step is to verify, call bash with \
the test command. There is no legitimate text-only response.
2. Address the FIRST un-checked step. Do not skip ahead.
3. Be terse. Do not narrate. Do not re-state the plan. Just act.
4. If a tool returns an error, diagnose and try a corrected action; do NOT repeat \
the same failing call.
5. NEVER claim a step is complete on your own — only the deterministic criterion \
and the attestor close steps.
6. DO NOT edit files under tests/ directories or matching test_*.py / *_test.py \
patterns. Those files are the verification harness — your job is to change the \
SOURCE CODE so tests pass. Modifying test files breaks the oracle's view of \
your fix and is almost never the correct intervention.
7. If the planner's suggested tool or args look wrong/missing, FIX them and \
still call a tool. Reading the file first (to learn exact strings) and then \
mutating next turn IS the right pattern. Doing nothing is never the right \
pattern.
8. The planner's `suggested_tool` is your strong default — call it with the \
planner's args (or your refined version) when you can. When you deviate \
(call a different tool, or refine args), you MUST pass a `reason` field in \
your tool call args, e.g. `read(path="...", reason="suggested str_replace \
old_str didn't match prior read; reading actual file to get correct \
content")`. The `reason` is one sentence of context for the planner/\
attestor — what info you're missing or why this tool serves the step \
better. Silent tool-swapping without a reason is wrong. When you ARE \
following the planner's suggestion, `reason` is optional.

Tool-selection guidance (prefer the SPECIFIC tool over `bash`; one call is \
cheaper than ten):
- `goto_def(name)` to locate a Python class/function/variable definition — \
  follows imports, respects scoping. NOT `rg("def foo|class foo")`.
- `find_refs(name)` to find all references to a symbol — filters strings/\
  comments and respects scoping. NOT `rg(name)`.
- `outline(path)` to see a file's top-level classes/functions WITHOUT reading \
  the whole file. NOT `read(path)` when you only want structure.
- `rg(pattern, context=N)` for textual code search with N lines of context — \
  NOT `bash("grep ...")`.
- `fd(pattern)` to locate files by name — NOT `bash("find ...")`.
- `multi_edit(path, edits=[...])` when fixing several hunks in one file in a \
  single call (atomic against the original; order-independent). For a single \
  hunk, `str_replace` is fine.
- `apply_patch(patch=<unified diff>)` to apply a multi-file diff in one call. \
  Hunks that don't match end up in .rej files (partial apply); the tool \
  result lists them.
- `git_status` / `git_diff` to inspect your accumulated changes between \
  iterations — first-class tools, not `bash("git ...")`.
- `bash` remains available for everything else (pytest, package install, \
  one-off shell ops).
"""

# Backward-compat alias (do not remove until v1.7+)
EXECUTOR_SYSTEM_PROMPT = PERFORMER_SYSTEM_PROMPT


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
    suspend_reason: Optional[str] = None
    prior_attempts: list[str] = field(default_factory=list)            # P1: compressed log of dropped turns
    _recent_turns: list[list[dict]] = field(default_factory=list)       # P1: raw recent turn snapshots
    grant: set[str] = field(default_factory=set)                        # capabilities granted at admission
    # 1.6.1: event-id landmarks — track causal parents for DAG discipline
    last_state_event_id: str = ""                                       # most recent contract_state event
    last_perform_event_id: str = ""                                      # most recent capability_perform event
    last_stipulate_event_id: str = ""                                   # contract_stipulate event (admission's parent)
    last_admit_event_id: str = ""                                       # contract_admit event
    last_iter_anchor: str = ""                                          # last event in the current iteration chain


def _extract_turn_summary(msgs: list[dict], turn_num: int) -> str:
    """Compress one turn's messages to a single line for the prior-attempts log.

    Format: 't01 bash("pip install pandas") → ERR: pip not found'
    Returns "" if the turn has no tool call.
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
        criterion_check_interval: int = 1,
        window_size: int = 4,
        librarian: Optional["Librarian"] = None,
    ):
        self.llm = llm
        self.registry = registry
        self.recorder = recorder
        self.max_iterations = max_iterations
        self.criterion_check_interval = criterion_check_interval
        self.window_size = window_size
        # 1.7 Phase A.1 — optional advisory librarian. If set, the activator
        # injects a "[CORPUS HINT]" system message into the performer context
        # before each Step-5 SELECT LLM call. The LLM still picks; the hint
        # carries prior runs' shortest-path-to-GOAL recommendation.
        self.librarian = librarian

    # ── Public entry ────────────────────────────────────────────────────────

    def run(self, contract: Contract, state: State) -> ActivatorRun:
        """Run the 8-step loop on `contract` until terminal state."""
        run = ActivatorRun(contract=contract, state=state)
        self._record_state(contract, ContractState.LATENT, run)

        # Step 1: INGEST — done by caller passing in `contract`.

        # Step 2: REPRESENT — verified facts before any planning.
        # Probes chain from the LATENT state event for proper DAG causality.
        representer = Representer(registry=self.registry, recorder=self.recorder)
        pre_reps = list(contract.representations or [])
        contract.representations = pre_reps + representer.represent(
            contract, state, parent_event_id=run.last_state_event_id,
        )
        represent_event_id = representer.represent_event_id(contract)

        # Post-REPRESENT: if a test oracle was discovered, inject it as the primary criterion.
        self._maybe_inject_oracle_criterion(contract)

        # Step 3: STIPULATE — planner sees obligation + representations.
        # Chains from contract_represent.
        self._stipulate(contract, run, parent_event_id=represent_event_id)

        # Step 4: ACTIVATE — one-shot admission gate.
        # Chains from contract_stipulate.
        decision = self._admit(contract, state, run)
        self._record_admission(contract, decision, run)
        if not decision.ok:
            self._apply_admission_failure(contract, run, decision)
            self._finalize(contract, run)
            return run

        run.grant = decision.grant
        contract.grant = decision.grant
        self._transition(contract, ContractState.ELIGIBLE, "admission gate passed", run)
        self._transition(contract, ContractState.ACTIVE, "discharge begins", run)
        run.last_iter_anchor = run.last_state_event_id   # iter 1 chains from ACTIVE state

        self._init_executor_messages(contract, run)

        # Discharge loop — Steps 5-8 repeat until terminal.
        while contract.state == ContractState.ACTIVE:
            # Per-iter budget enforcement (separate from admission's capacity check)
            if not self._check_budget(contract, state, run):
                break

            if run.iterations >= self.max_iterations:
                # Step 8 CLOSE — criterion unreachable within iteration envelope.
                # Transition to SUSPENDED (not FAILED): the world could change
                # and a re-admission attempt could yet succeed.
                self._transition(
                    contract, ContractState.SUSPENDED,
                    f"max_iterations {self.max_iterations} reached without criterion satisfaction",
                    run,
                )
                run.suspend_reason = "criterion_unreachable: iteration envelope exhausted"
                contract.suspend_reason = run.suspend_reason
                contract.re_entry_condition = "fresh representations or extended envelope"
                break

            # Step 5 SELECT (v1: fixed executor) + Step 6 INVOKE
            iter_start_len = len(run.messages)
            keep_going = self._perform_once(contract, state, run)
            if not keep_going:
                # Executor signaled DONE or emitted no tool call.
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED,
                                     "executor stopped and criteria satisfied", run)
                else:
                    self._transition(contract, ContractState.FAILED,
                                     "executor stopped before criteria satisfied", run)
                    run.failure_reason = "executor_quit_early"
                break

            # Step 7 VERIFY — criterion on interval OR when all plan steps done.
            all_steps_done = bool(run.plan) and all(s.complete for s in run.plan)
            should_check = (run.iterations % self.criterion_check_interval == 0) or all_steps_done
            if should_check:
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED, "all criteria satisfied", run)
                    break

            # Step 7 (judge) — advisory per-step completion
            self._update_plan_checklist(contract, run)

            # P1 sliding window: drop the oldest raw turn → compressed prior_attempts entry
            turn_msgs = run.messages[iter_start_len:]
            self._prune_to_window(contract, run, turn_msgs)

        self._finalize(contract, run)
        return run

    # ── Cost charging ────────────────────────────────────────────────────────
    # Every LLM call goes through _charge_llm; every tool invocation goes through
    # _charge_tool. These are the SINGLE SOURCE OF TRUTH for cost accumulation.
    # Direct increments to contract.cost.* outside these helpers create budget
    # escapes (see PLAN.md 1.5.15 for the audit that led to this).

    def _charge_llm(self, contract: Contract, result: LLMResult) -> None:
        contract.cost.tokens_in  += result.tokens_in
        contract.cost.tokens_out += result.tokens_out
        contract.cost.usd        += result.usd
        contract.cost.llm_calls  += 1
        contract.cost.wall_s     += float(result.raw.get("wall_s", 0.0))

    def _charge_tool(self, contract: Contract, result: ToolResult) -> None:
        contract.cost.tool_calls += 1
        contract.cost.wall_s     += float(result.wall_s)

    # ── Step 3: DECOMPOSE ───────────────────────────────────────────────────

    def _stipulate(
        self, contract: Contract, run: ActivatorRun, parent_event_id: Optional[str] = None,
    ) -> None:
        # Planner LLM call chains from contract_represent (parent_event_id).
        # contract_stipulate then chains from the planner llm_usage event.
        # We don't know the llm_usage event_id pre-call, so use the most-recent-
        # event approximation: contract_stipulate chains from parent_event_id
        # (contract_represent) since the planner llm_usage is itself a sibling.
        steps, planner_result = call_stipulator(
            self.llm,
            contract.obligation,
            representations=contract.representations or None,
            parent_event_id=parent_event_id,
        )
        self._charge_llm(contract, planner_result)
        if not steps:
            steps = [{"id": "s1", "description": contract.obligation.strip().splitlines()[0][:200]}]
        run.plan = [_PlanStep(id=s["id"], description=s["description"]) for s in steps]
        event_id = f"dec-{uuid.uuid4().hex[:6]}"
        # Parent = the most-recent llm_usage (planner) which produced this plan.
        # The recorder's last event was the planner llm_usage event.
        dec_parent = self.recorder.traj.events[-1].event_id if self.recorder.traj.events else parent_event_id
        self.recorder.append(TraceEvent(
            event_id=event_id,
            parent_id=dec_parent,
            ts=utcnow_iso(),
            kind="contract_stipulate",
            payload={
                "contract_id": contract.id,
                "steps": [{"id": s.id, "description": s.description} for s in run.plan],
            },
        ))
        run.last_stipulate_event_id = event_id

    # ── Step 4: ACTIVATE — admission gate ───────────────────────────────────

    def _admit(self, contract: Contract, state: State, run: ActivatorRun) -> AdmissionDecision:
        """One-shot admission validation. Three checks → grant.

        (a) Boundary — is the contract well-formed?
        (b) Environment — does the world match the contract's assumptions?
        (c) Capacity — is there room to run?
        (d) Grant — derive granted capability set (v1: full registry).
        """
        # (a) Boundary
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

        # (b) Environment
        if not contract.conditions_met(state):
            return AdmissionDecision.reject("environment", "declared conditions not met")
        # Capability availability: every capability mentioned in `contract.capabilities`
        # must exist in the registry. (Plan-step-derived capability requirements are
        # v2 work; v1 only validates the declared set.)
        for cap_name in contract.capabilities or []:
            if self.registry.get(cap_name) is None:
                return AdmissionDecision.reject(
                    "environment", f"capability '{cap_name}' not in registry",
                )

        # (c) Capacity
        violation = cons.check(contract.cost, contract.depth, now)
        if violation:
            return AdmissionDecision.reject("capacity", violation)

        # (d) Grant — v1: full registry. v2 will derive a subset from plan steps.
        grant = set(self.registry.names())

        # (e) Lock the deadline: absolute wall-clock time by which the contract
        # must close. Per-iteration _check_budget enforces it via
        # Constraints.check() — the contract owns its envelope, not the harness.
        if contract.constraints.deadline_ts is None and contract.constraints.max_wall_s > 0:
            contract.constraints.deadline_ts = time.time() + contract.constraints.max_wall_s

        return AdmissionDecision.admit(grant)

    def _apply_admission_failure(
        self, contract: Contract, run: ActivatorRun, decision: AdmissionDecision,
    ) -> None:
        """Consult contract.policy for the response to an admission failure."""
        response = contract.policy.response_for(decision.failure_type)  # type: ignore[arg-type]
        reason = f"admission_{decision.failure_type}: {decision.reason}"

        if response == "FAIL":
            self._transition(contract, ContractState.FAILED, reason, run)
            run.failure_reason = reason
        elif response == "SUSPEND":
            self._transition(contract, ContractState.SUSPENDED, reason, run)
            run.suspend_reason = reason
            contract.suspend_reason = reason
            contract.re_entry_condition = decision.reason
        elif response == "ESCALATE":
            # v1: ESCALATE has no parent contract to receive it; treat as FAIL with tag.
            self._transition(contract, ContractState.FAILED, f"escalate: {reason}", run)
            run.failure_reason = f"escalate: {reason}"
        elif response == "ASK_USER":
            # v1: no user-input capability yet; treat as SUSPEND tagged as awaiting user.
            self._transition(contract, ContractState.SUSPENDED, f"ask_user: {reason}", run)
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
            ts=utcnow_iso(),
            kind="contract_admit",
            payload={
                "contract_id": contract.id,
                "ok": decision.ok,
                "failure_type": decision.failure_type,
                "reason": decision.reason,
                "grant": sorted(decision.grant) if decision.ok else [],
            },
        ))
        run.last_admit_event_id = event_id

    # ── Per-iteration budget enforcement ────────────────────────────────────

    def _check_budget(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """Return True if still within envelope; transition + return False otherwise."""
        now = time.time()
        violation = contract.constraints.check(contract.cost, contract.depth, now)
        if violation:
            self._transition(contract, ContractState.FAILED, f"budget violation: {violation}")
            run.failure_reason = violation
            return False
        return True

    # ── Step 6: INVOKE one capability ───────────────────────────────────────

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

    def _run_election(
        self,
        contract: Contract,
        run: ActivatorRun,
        candidates: list[dict],
        parent_event_id: str,
    ) -> int:
        """Score `candidates` via the librarian; emit an `election` trace
        event; return the winning index.

        Winner = lowest total_score among non-defer candidates; if all
        defer, returns 0 (LLM's preferred). Never raises — election is
        best-effort optimization, not a correctness path.
        """
        try:
            from cat._legacy.state_class import extract_raw_features_from_events
            events_dicts = [
                {"kind": e.kind, "payload": e.payload or {}}
                for e in self.recorder.traj.events
            ]
            # CRITICAL: do NOT strip — the corpus stores obligation text as-is
            # from the contract_state event payload, and stripping here would
            # produce a different hash than what's in the corpus, breaking
            # every state_class lookup. (Bug found 2026-05-30.)
            obligation_text = contract.obligation or ""
            raw = extract_raw_features_from_events(events_dicts, obligation_text)
            # Normalize candidates to {tool, args} dicts for librarian
            cand_inputs = [
                {"tool": c.get("name", ""), "args": c.get("arguments", {}) or {}}
                for c in candidates
            ]
            scored = self.librarian.score_candidates(cand_inputs, raw)
        except Exception as exc:  # noqa: BLE001 — never crash for an optimization
            print(f"warning: election scoring failed: {exc}")
            return 0

        # Pick winner: lowest total_score among non-defer; else 0
        eligible = [s for s in scored if not s.defer and s.total_score is not None]
        if eligible:
            winner = min(eligible, key=lambda s: s.total_score)
            elected_index = winner.index
            decision_reason = "math"
        else:
            elected_index = 0
            decision_reason = "defer"

        # Record election event
        election_id = f"elect-{uuid.uuid4().hex[:6]}"
        self.recorder.append(TraceEvent(
            event_id=election_id,
            parent_id=parent_event_id,
            ts=utcnow_iso(),
            kind="election",
            payload={
                "contract_id": contract.id,
                "iteration": run.iterations,
                "winner_index": elected_index,
                "winner_tool": candidates[elected_index].get("name", ""),
                "decision": decision_reason,
                "candidates": [
                    {
                        "index": s.index,
                        "tool": s.tool,
                        "action_klass": s.action_klass,
                        "state_class": s.state_class,
                        "expected_cost": s.expected_cost,
                        "next_state_d_star": s.next_state_d_star,
                        "total_score": s.total_score,
                        "confidence_n": s.confidence_n,
                        "defer": s.defer,
                    }
                    for s in scored
                ],
            },
        ))
        return elected_index

    def _librarian_hint(self, contract: Contract, run: ActivatorRun) -> Optional[str]:
        """Build a CORPUS HINT message from the LIBRARIAN's recommendation,
        if any. Returns None when the librarian is unavailable, the state is
        cold, or no path-to-goal candidate exists.

        When a hint IS produced, also emits a `librarian_hint` trace event
        so we can count emissions and attribute behavior changes (post-A.2
        refinement (1) — telemetry).

        Lightweight: just walks the current trace events to compute the
        runtime raw_features snapshot.
        """
        if self.librarian is None or not self.librarian.is_ready():
            return None
        try:
            from cat._legacy.state_class import extract_raw_features_from_events
            events_dicts = [
                {"kind": e.kind, "payload": e.payload or {}}
                for e in self.recorder.traj.events
            ]
            # CRITICAL: do NOT strip — the corpus stores obligation text as-is
            # from the contract_state event payload, and stripping here would
            # produce a different hash than what's in the corpus, breaking
            # every state_class lookup. (Bug found 2026-05-30.)
            obligation_text = contract.obligation or ""
            raw = extract_raw_features_from_events(events_dicts, obligation_text)
            rec = self.librarian.recommend(raw)
        except Exception as exc:  # noqa: BLE001 — never crash the run for a hint
            print(f"warning: librarian hint failed: {exc}")
            return None
        if rec is None:
            return None

        # Telemetry: record that a hint was emitted on this turn.
        # Chains from the iteration anchor — sibling to the upcoming
        # performer llm_usage event the hint will influence.
        try:
            parent = (
                run.last_iter_anchor
                or run.last_perform_event_id
                or run.last_admit_event_id
                or run.last_state_event_id
                or None
            )
            self.recorder.append(TraceEvent(
                event_id=f"hint-{uuid.uuid4().hex[:6]}",
                parent_id=parent,
                ts=utcnow_iso(),
                kind="librarian_hint",
                payload={
                    "contract_id": contract.id,
                    "iteration": run.iterations + 1,  # this hint is for the NEXT iter
                    "state_class": rec.state_class,
                    "action_klass": rec.action_klass,
                    "tool": rec.tool,
                    "canonical_args": rec.canonical_args,
                    "expected_cost": rec.expected_cost,
                    "next_state_d_star": rec.next_state_d_star,
                    "total_score": rec.total_score,
                    "confidence_n": rec.confidence_n,
                    "n_alternatives": rec.n_alternatives,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"warning: librarian_hint trace failed: {exc}")

        return (
            "[CORPUS HINT] "
            + rec.format_hint()
            + " — this is advisory only; use your judgment if the situation differs."
        )

    def _perform_once(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """One executor turn: ask LLM for next action, run the tool call (if any).

        Increments run.iterations. Returns True if the loop should continue
        (a tool was called); False if the executor signaled DONE or emitted no
        tool call.

        v1: the tool schema exposed to the LLM is the full registry. v2 will
        restrict the schema to the grant computed at admission time.
        """
        run.iterations += 1
        tools = self.registry.tool_schemas(only=run.grant or None)
        # Iteration N chains from iteration N-1 (or ACTIVE state for iter 1).
        # last_iter_anchor: the event that this iteration's executor LLM call
        # logically follows. Updated after VERIFY so iter N+1 chains from the
        # last verify event if one ran.
        iter_parent = (
            run.last_iter_anchor
            or run.last_perform_event_id
            or run.last_admit_event_id
            or run.last_state_event_id
            or None
        )

        # 1.7 Phase A.1 — inject LIBRARIAN advisory hint if available.
        # The hint goes in as a transient SYSTEM message that the LLM sees on
        # THIS turn only; it doesn't get permanently added to run.messages so
        # later turns won't carry stale advice. The hint is purely advisory;
        # the LLM still picks the tool call.
        messages_with_hint = run.messages
        hint_text = self._librarian_hint(contract, run) if self.librarian is not None else None
        if hint_text:
            messages_with_hint = run.messages + [
                {"role": "system", "content": hint_text}
            ]

        # R19.5: tool_choice="required" — Performer's contract is to ACT, not
        # to describe. Forcing a tool call at the API level makes text-only
        # responses architecturally impossible. When the planner's suggestion
        # is wrong, the Performer fixes the args inside a tool call (e.g.
        # reads the file first, then mutates next turn) — still always a tool.
        result = self.llm.chat(
            messages=messages_with_hint,
            tools=tools,
            tool_choice="required",
            role_tag="performer",
            temperature=0.2,
            parent_event_id=iter_parent,
        )
        # The executor llm_usage event was just appended (last event in trace).
        executor_llm_event_id = self.recorder.traj.events[-1].event_id
        self._charge_llm(contract, result)

        # 1.7 Phase A.2 — ELECTION: when the LLM emits ≥2 candidate tool
        # calls in one turn and we have a librarian, the math arbitrates.
        # Scoring is `expected_cost + d*(next_state)`. Lowest wins. If all
        # candidates DEFER (no corpus signal), fall through to the first
        # candidate (LLM's preferred — preserves cold-state behavior).
        # The losing candidate is recorded in an `election` trace event for
        # retrospective analysis and trimmed from the message history so
        # the OpenAI tool_call ↔ tool_result correspondence stays valid.
        elected_index = 0
        if (
            len(result.tool_calls) >= 2
            and self.librarian is not None
            and self.librarian.is_ready()
        ):
            elected_index = self._run_election(
                contract, run, result.tool_calls, executor_llm_event_id,
            )
        # Keep only the winning tool_call in result.tool_calls for downstream
        # processing. Other candidates are recorded in the election event.
        if len(result.tool_calls) >= 2 and elected_index != 0:
            winner = result.tool_calls[elected_index]
            result.tool_calls = [winner]
        elif len(result.tool_calls) >= 2:
            result.tool_calls = [result.tool_calls[0]]

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
            run.final_text = result.text
            return False

        # Discipline: only execute the FIRST tool call (post-ELECTION, this is
        # the winning candidate; pre-ELECTION it was the LLM's first emission).
        first = result.tool_calls[0]
        invoke_event_id = f"inv-{uuid.uuid4().hex[:6]}"
        # capability_perform chains from the executor llm_usage that produced this tool call
        self.recorder.append(TraceEvent(
            event_id=invoke_event_id,
            parent_id=executor_llm_event_id,
            ts=utcnow_iso(),
            kind="capability_perform",
            payload={
                "contract_id": contract.id,
                "tool": first["name"],
                "arguments": first["arguments"],
                "tool_call_id": first["id"],
                "iteration": run.iterations,
            },
        ))
        run.last_perform_event_id = invoke_event_id
        run.last_iter_anchor = invoke_event_id   # next iter chains from here (may be overridden by VERIFY)

        tool_result: ToolResult = self.registry.invoke(
            name=first["name"],
            args=first["arguments"],
            state=state,
            recorder=self.recorder,
            contract_id=contract.id,
            tool_call_id=first["id"],
            parent_event_id=invoke_event_id,
        )
        self._charge_tool(contract, tool_result)

        run.messages.append({
            "role": "tool",
            "tool_call_id": first["id"],
            "content": tool_result.output if not tool_result.is_error
            else f"[ERROR] {tool_result.output}",
        })

        # OpenAI requires a matching tool message for any extra parallel tool_calls
        # emitted by the assistant; reply with a stub for ignored ones.
        for skipped in result.tool_calls[1:]:
            run.messages.append({
                "role": "tool",
                "tool_call_id": skipped["id"],
                "content": "[skipped — discipline: one action per turn]",
            })

        return True

    # ── Oracle criterion injection ───────────────────────────────────────────

    def _maybe_inject_oracle_criterion(self, contract: Contract) -> None:
        """If REPRESENT found a test oracle and no oracle criterion exists, inject one."""
        if any(c.is_oracle for c in contract.criteria):
            return

        strategy_rep = next(
            (r for r in (contract.representations or []) if r.key == "judge_strategy"),
            None,
        )
        if strategy_rep is None or not strategy_rep.value.startswith("oracle:"):
            return

        oracle_cmd = strategy_rep.value[len("oracle:"):].strip()
        if not oracle_cmd:
            return

        contract.criteria.insert(0, self._build_oracle_criterion(contract, oracle_cmd))

    def _build_oracle_criterion(self, contract: Contract, command: str) -> "Criterion":
        from cat.contracts import Criterion, Evidence
        registry = self.registry
        recorder = self.recorder
        charge_tool = self._charge_tool   # capture; contract is closed-over

        def _predicate(state: State) -> tuple[bool, Evidence]:
            # Parent of oracle's bash tool_call is the last event in the trace.
            # When called from _verify_criteria mid-loop, that's the last invoke;
            # when called from TbSweAgent._initial_representations, that's the
            # latent state event. Either is the correct causal parent.
            parent = recorder.traj.events[-1].event_id if recorder.traj.events else None
            result = registry.invoke(
                name="bash",
                args={"command": command, "timeout_s": 120},
                state=state,
                recorder=recorder,
                contract_id="oracle-check",
                tool_call_id=f"oracle-{uuid.uuid4().hex[:6]}",
                parent_event_id=parent,
            )
            # Oracle bash runs every criterion check — charge it to the contract
            # so its tool_calls + wall_s do not silently escape the envelope.
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
            predicate=_predicate,
            is_oracle=True,
        )

    # ── Step 7: VERIFY ──────────────────────────────────────────────────────

    def _verify_criteria(self, contract: Contract, state: State, run: ActivatorRun) -> bool:
        """Run criteria. Oracle criteria are authoritative; LLM criteria are fallback.

        When any criterion has is_oracle=True, ONLY oracle criteria run; LLM-judge
        criteria are skipped — the contract closes on real test results, not on
        model opinion.
        """
        if not contract.criteria:
            return False

        oracle_crits = [c for c in contract.criteria if c.is_oracle]
        crits_to_run = oracle_crits if oracle_crits else contract.criteria

        all_ok = True
        for crit in crits_to_run:
            ok = crit.check(state)
            # criterion_check chains from the oracle bash tool_result (last appended
            # event during crit.check), which itself chains from the invoke.
            crit_parent = (
                self.recorder.traj.events[-1].event_id
                if self.recorder.traj.events
                else run.last_perform_event_id
            )
            event_id = f"crit-{crit.id}-{uuid.uuid4().hex[:4]}"
            self.recorder.append(TraceEvent(
                event_id=event_id,
                parent_id=crit_parent,
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
            run.last_iter_anchor = event_id   # next iter chains from this verify event
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
            # Judge LLM call chains from the current invoke (it's evaluating
            # that invoke's effect on the plan step).
            complete, reason, judge_result = call_attestor(
                self.llm, step.description, summary,
                parent_event_id=run.last_perform_event_id or None,
            )
            judge_llm_event_id = self.recorder.traj.events[-1].event_id
            self._charge_llm(contract, judge_result)
            step.complete = complete
            step.last_reason = reason[:140]
            judge_event_id = f"judge-{step.id}-{uuid.uuid4().hex[:4]}"
            self.recorder.append(TraceEvent(
                event_id=judge_event_id,
                parent_id=judge_llm_event_id,
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
            run.last_iter_anchor = judge_event_id
            # Only judge first un-checked step per turn (token budget discipline).
            break

        # Refresh executor's user prompt with the updated checklist for the next turn.
        run.messages.append({
            "role": "user",
            "content": (
                "CHECKLIST UPDATE:\n"
                f"{self._format_checklist(run)}\n\n"
                f"BUDGET:\n{self._format_budget(contract)}\n\n"
                "Take ONE action toward the first un-checked step, "
                "or reply DONE only if all are checked."
            ),
        })

    def _prune_to_window(
        self, contract: Contract, run: ActivatorRun, turn_msgs: list[dict]
    ) -> None:
        """P1: sliding window. Drop oldest raw turn → compressed prior_attempts entry.

        After each iteration, snapshot the turn's messages. When window overflows,
        drop the oldest snapshot and append a one-liner to prior_attempts. Rebuild
        run.messages from [system, fresh user prompt, prior_attempts log, last N turns].
        """
        if self.window_size <= 0 or not turn_msgs:
            return

        run._recent_turns.append(list(turn_msgs))

        if len(run._recent_turns) <= self.window_size:
            return

        dropped = run._recent_turns.pop(0)
        summary = _extract_turn_summary(dropped, len(run.prior_attempts) + 1)
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
        terminal_output = run.state.kv.get("terminal_output")
        if terminal_output:
            snapshot = f"TERMINAL PANE (last capture):\n{terminal_output[:3000]}"
        else:
            snapshot = workdir_snapshot(run.state.workdir)
        return (
            f"{last_str}\n"
            f"WORKDIR SNAPSHOT:\n{snapshot}\n"
        )

    # ── State machinery ─────────────────────────────────────────────────────

    def _transition(
        self, contract: Contract, to: ContractState, reason: str,
        run: Optional[ActivatorRun] = None,
    ) -> None:
        # Causal-parent semantics: a state transition is caused by the event
        # that just preceded it (admission decision, criterion check, budget
        # violation, etc.). Default to "last appended event" which is naturally
        # the cause by virtue of sequential execution order.
        last_ev = self.recorder.traj.events[-1].event_id if self.recorder.traj.events else None
        if not contract.can_transition(to):
            # Rejected transition; chain from the cause but don't update cursor
            self.recorder.append(TraceEvent(
                event_id=f"state-{uuid.uuid4().hex[:6]}",
                parent_id=last_ev,
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
        event_id = f"state-{uuid.uuid4().hex[:6]}"
        self.recorder.append(TraceEvent(
            event_id=event_id,
            parent_id=last_ev,
            ts=utcnow_iso(),
            kind="contract_state",
            payload={
                "contract_id": contract.id,
                "from": prev.value,
                "to": to.value,
                "reason": reason,
            },
        ))
        if run is not None:
            run.last_state_event_id = event_id

    def _record_state(self, contract: Contract, state: ContractState, run: ActivatorRun) -> None:
        event_id = f"state-init-{contract.id}"
        self.recorder.append(TraceEvent(
            event_id=event_id,
            parent_id=None,    # ROOT of the DAG
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
        run.last_state_event_id = event_id

    def _finalize(self, contract: Contract, run: ActivatorRun) -> None:
        # step_end chains from the terminal state transition (last state event)
        self.recorder.append(TraceEvent(
            event_id=f"step-{uuid.uuid4().hex[:6]}",
            parent_id=run.last_state_event_id or None,
            ts=utcnow_iso(),
            kind="step_end",
            payload={
                "iterations": run.iterations,
                "final_state": contract.state.value,
                "cost": _cost_dict(contract.cost),
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
