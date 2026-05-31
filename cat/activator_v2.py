"""ActivatorV2 — v2 loop redesign (docs/iteration.md).

Subclasses Activator; reuses _admit, _perform_once, _verify_criteria,
_charge_llm, _transition, _finalize, _record_state, _check_budget,
_init_executor_messages, _build_user_prompt, _prune_to_window.

Replaces run() with the new flow:
    pre-loop:  REPRESENT → obligation_class → STIPULATE-phased → ACTIVATE
    iter:      PLAN (state_class, attention) → ACTION (_perform_once)
               → OBSERVE (surprise) → tree-edit (in-loop Stipulate on
               surprise) OR COMMIT + advance attention
    post-loop: FINALIZE plan tree (write to librarian if available)
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from cat.activator import Activator, ActivatorRun, _PlanStep, PERFORMER_SYSTEM_PROMPT
from cat.contracts import Contract, ContractState, State
from cat.llm import (
    call_stipulator_phased,
    call_stipulator_inloop,
    call_stipulator,  # fallback
    call_attestor,
)
from cat.obligation_class import compute_obligation_class
from cat.plan_tree import PlanTree, TreeEditOp
from cat.representer import Representer
from cat.state_class_v2 import RunCounts, compute_state_class
from cat.surprise import compute_surprise, SurpriseVector
from cat.trace import TraceEvent, utcnow_iso


PERFORMER_V2_PROMPT_SUFFIX = """
v2 ATTENTION/LOOKAHEAD: a working plan tree is being maintained for you.
Each turn you see the current ATTENTION step (what to do now) and a short
LOOKAHEAD (what's coming next). Stay focused on the ATTENTION step.
Do not skip ahead. The activator will advance attention or revise the
plan based on observation.
"""


class ActivatorV2(Activator):
    """v2 loop: phased plan tree, surprise vector, in-loop re-stipulate."""

    def __init__(
        self,
        *args,
        max_restip_per_task: int = 5,
        librarian_writer=None,  # optional: object with .record_edge / .record_plan_tree
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.max_restip_per_task = max_restip_per_task
        self.librarian_writer = librarian_writer

    def run(self, contract: Contract, state: State) -> ActivatorRun:
        """v2 discharge loop."""
        run = ActivatorRun(contract=contract, state=state)
        self._record_state(contract, ContractState.LATENT, run)

        # Step 2: REPRESENT
        representer = Representer(registry=self.registry, recorder=self.recorder)
        pre_reps = list(contract.representations or [])
        contract.representations = pre_reps + representer.represent(
            contract, state, parent_event_id=run.last_state_event_id,
        )
        represent_event_id = representer.represent_event_id(contract)
        self._maybe_inject_oracle_criterion(contract)

        # obligation_class
        oc = compute_obligation_class(
            contract.obligation,
            representations=contract.representations,
            task_id=contract.id,
        )
        run.obligation_class = oc  # type: ignore[attr-defined]
        run.run_counts = RunCounts()  # type: ignore[attr-defined]
        run.restip_count = 0  # type: ignore[attr-defined]

        # Retrieve PASS/FAIL templates from the librarian (if any)
        plan_templates = []
        anti_templates = []
        if self.librarian_writer is not None:
            try:
                plan_templates = self.librarian_writer.get_plan_templates(oc, k=3)
                anti_templates = self.librarian_writer.get_anti_templates(oc, k=3)
            except Exception:
                plan_templates = []
                anti_templates = []

        # Step 3: STIPULATE — phased; fall back to flat on failure
        phased_plan, planner_result = call_stipulator_phased(
            self.llm,
            contract.obligation,
            representations=contract.representations or None,
            plan_templates=plan_templates or None,
            anti_templates=anti_templates or None,
            parent_event_id=represent_event_id,
        )
        self._charge_llm(contract, planner_result)

        plan_tree: Optional[PlanTree] = None
        if phased_plan and phased_plan.get("phases"):
            plan_tree = PlanTree.from_phased_plan(phased_plan, obligation_class=oc)
            run.plan_tree = plan_tree  # type: ignore[attr-defined]
            # Mirror the flat list for v1 admission gate compat
            flat_steps: list[_PlanStep] = []
            for phase_id in plan_tree.phase_order:
                phase = plan_tree.nodes[phase_id]
                for step_id in phase.children:
                    n = plan_tree.nodes[step_id]
                    flat_steps.append(_PlanStep(id=step_id, description=n.description))
            run.plan = flat_steps
        else:
            # Fallback: use v1 call_stipulator
            steps, planner_result2 = call_stipulator(
                self.llm,
                contract.obligation,
                representations=contract.representations or None,
                parent_event_id=represent_event_id,
            )
            self._charge_llm(contract, planner_result2)
            if not steps:
                steps = [{"id": "s1", "description": contract.obligation.strip().splitlines()[0][:200]}]
            run.plan = [_PlanStep(id=s["id"], description=s["description"]) for s in steps]
            plan_tree = None
            run.plan_tree = None  # type: ignore[attr-defined]

        # Emit contract_stipulate (chains from planner llm_usage)
        event_id = f"dec-{uuid.uuid4().hex[:6]}"
        dec_parent = self.recorder.traj.events[-1].event_id if self.recorder.traj.events else represent_event_id
        self.recorder.append(TraceEvent(
            event_id=event_id,
            parent_id=dec_parent,
            ts=utcnow_iso(),
            kind="contract_stipulate",
            payload={
                "contract_id": contract.id,
                "steps": [{"id": s.id, "description": s.description} for s in run.plan],
                "v2_phased": plan_tree is not None,
                "obligation_class": oc,
            },
        ))
        run.last_stipulate_event_id = event_id

        # Step 4: ACTIVATE
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
        run.last_iter_anchor = run.last_state_event_id

        self._init_executor_messages(contract, run)

        # Discharge loop — v2 shape
        while contract.state == ContractState.ACTIVE:
            if not self._check_budget(contract, state, run):
                break
            if run.iterations >= self.max_iterations:
                self._transition(
                    contract, ContractState.SUSPENDED,
                    f"max_iterations {self.max_iterations} reached without criterion satisfaction",
                    run,
                )
                run.suspend_reason = "criterion_unreachable: iteration envelope exhausted"
                contract.suspend_reason = run.suspend_reason
                contract.re_entry_condition = "fresh representations or extended envelope"
                break

            # PLAN phase
            attention_node = plan_tree.get_attention() if plan_tree else None
            if plan_tree and attention_node is None:
                # All plan steps done — final verify
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED,
                                     "plan tree exhausted, criteria satisfied", run)
                else:
                    self._transition(contract, ContractState.FAILED,
                                     "plan tree exhausted before criteria satisfied", run)
                    run.failure_reason = "plan_tree_exhausted"
                break

            state_now_class = ""
            if plan_tree and attention_node:
                state_now_class = compute_state_class(
                    oc, attention_node.phase_name, attention_node.intent, run.run_counts  # type: ignore[attr-defined]
                )
                self._inject_plan_attention(run, plan_tree)

            cost_before = contract.cost.tokens_in + contract.cost.tokens_out

            # ACTION (Step 5+6: SELECT + PERFORM)
            keep_going = self._perform_once(contract, state, run)
            if not keep_going:
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED,
                                     "executor stopped and criteria satisfied", run)
                else:
                    self._transition(contract, ContractState.FAILED,
                                     "executor stopped before criteria satisfied", run)
                    run.failure_reason = "executor_quit_early"
                break

            cost_after = contract.cost.tokens_in + contract.cost.tokens_out
            actual_cost = cost_after - cost_before

            # OBSERVE phase — wrapped to guarantee loop continues even if helpers fail
            try:
                self._observe_phase(
                    contract, run, plan_tree, attention_node, actual_cost,
                    state_now_class, oc,
                )
            except Exception as e:
                # Log + force-advance to prevent infinite loop on crash
                import traceback
                self.recorder.append(TraceEvent(
                    event_id=f"err-{uuid.uuid4().hex[:6]}",
                    parent_id=run.last_perform_event_id or run.last_iter_anchor,
                    ts=utcnow_iso(),
                    kind="surprise",
                    payload={"error": str(e), "trace": traceback.format_exc()[:500]},
                ))
                if attention_node:
                    self._default_advance(plan_tree, attention_node, run)


            # Step 7: VERIFY criterion on interval
            all_steps_done = bool(run.plan) and all(s.complete for s in run.plan)
            should_check = (
                run.iterations % self.criterion_check_interval == 0
                or all_steps_done
                or (plan_tree is not None and plan_tree.all_complete())
            )
            if should_check:
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED,
                                     "all criteria satisfied", run)
                    break

            # NOTE: We deliberately do NOT call _update_plan_checklist here.
            # The attestor already ran once in _observe_phase on the current
            # attention node — that's the authoritative signal. Running it
            # again over the full flat plan would cost +N attestor calls per
            # iter for no extra signal.
            self._update_plan_checklist(contract, run)

            # P1 sliding window prune (same as v1)
            iter_start_len = len(run.messages)
            # Note: messages already extended by _perform_once and may include the SYSTEM hint;
            # prune still works.

        # Post-loop persistence
        if plan_tree and self.librarian_writer is not None:
            try:
                outcome = contract.state.value if hasattr(contract.state, "value") else str(contract.state)
                self.librarian_writer.record_plan_tree(plan_tree, outcome)
            except Exception:
                pass

        self._finalize(contract, run)
        return run

    # ── helpers ──────────────────────────────────────────────────────────
    def _inject_plan_attention(self, run: ActivatorRun, plan_tree: PlanTree) -> None:
        """Append a transient SYSTEM message showing attention + lookahead.

        Like the librarian_hint mechanism in v1.7 A.1: ephemeral hint for
        this turn only; we don't keep it across turns (the next iter will
        re-inject from updated tree state).
        """
        attention = plan_tree.get_attention()
        if not attention:
            return
        lookahead = plan_tree.lookahead(max_steps=4)
        lookahead_str = "\n".join(
            f"  - [{n.id}] ({n.intent}) {n.description[:100]}" for n in lookahead[1:]
        ) or "  (none)"
        hint = (
            f"PLAN ATTENTION (focus on this one step):\n"
            f"  [{attention.id}] ({attention.phase_name}/{attention.intent}) {attention.description}\n"
            f"  expects: durable_change={attention.expects.get('durable_change', False)}, "
            f"files={attention.expects.get('files_modified', [])}, "
            f"verifier={attention.expects.get('verifier_outcome', 'N/A')}\n"
            f"\nLOOKAHEAD (do NOT do these yet):\n{lookahead_str}\n"
        )
        # Append transient SYSTEM (will be present for THIS turn only; pruned next iter)
        run.messages.append({"role": "system", "content": hint})

    def _infer_durable_change(self, run: ActivatorRun) -> tuple[bool, list[str]]:
        """Approximate: was the last tool call a write-tool that succeeded?

        Returns (durable_change_observed, list_of_files_modified).
        """
        if not run.messages:
            return False, []
        # Walk back from the end looking for the last tool message + the
        # assistant tool_call that preceded it
        last_tool_msg_idx = None
        for i in range(len(run.messages) - 1, -1, -1):
            if run.messages[i].get("role") == "tool":
                last_tool_msg_idx = i
                break
        if last_tool_msg_idx is None:
            return False, []
        # Look for the assistant that emitted this tool call
        for j in range(last_tool_msg_idx - 1, -1, -1):
            m = run.messages[j]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                tc = m["tool_calls"][0]
                tool_name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                # Check if tool result was an error
                tool_content = run.messages[last_tool_msg_idx].get("content", "") or ""
                if isinstance(tool_content, str) and tool_content.startswith("[ERROR]"):
                    return False, []
                # Write-tools
                if tool_name in ("str_replace", "multi_edit", "apply_patch", "write"):
                    path = args.get("path", "") or args.get("file", "") or ""
                    files = [path] if path else []
                    if tool_name == "apply_patch":
                        # Try to extract file paths from the patch
                        patch = args.get("patch", "") or args.get("diff", "") or ""
                        for line in patch.splitlines():
                            if line.startswith("+++ b/") or line.startswith("--- a/"):
                                p = line[6:].strip()
                                if p and p not in files:
                                    files.append(p)
                    return True, files
                return False, []
        return False, []

    def _observe_phase(
        self,
        contract: Contract,
        run: ActivatorRun,
        plan_tree: PlanTree,
        attention_node,
        actual_cost: float,
        state_now_class: str,
        oc: str,
    ) -> None:
        """End-of-iter OBSERVE phase. Computes surprise, applies tree edits.

        Authority for "is this step done?" is the attestor (LLM judge), NOT
        the heuristic. This replaces the v1.7 _update_plan_checklist's
        attestor-over-every-step with one focused attestor call per iter.
        """
        # Approximate: did the last tool call modify the filesystem?
        actual_durable_change, actual_files = self._infer_durable_change(run)

        # Update counts
        run.run_counts.bump(attention_node.intent)  # type: ignore[attr-defined]
        if actual_durable_change:
            run.run_counts.mark_durable_change()  # type: ignore[attr-defined]
        else:
            run.run_counts.tick_no_change()  # type: ignore[attr-defined]

        state_next_class = compute_state_class(
            oc, attention_node.phase_name, attention_node.intent, run.run_counts  # type: ignore[attr-defined]
        )

        # Surprise vector
        sv = compute_surprise(
            expects=attention_node.expects,
            actual_cost=float(actual_cost),
            actual_durable_change=actual_durable_change,
            actual_files_modified=actual_files,
        )

        # Record surprise event
        self.recorder.append(TraceEvent(
            event_id=f"sup-{uuid.uuid4().hex[:6]}",
            parent_id=run.last_perform_event_id or run.last_iter_anchor,
            ts=utcnow_iso(),
            kind="surprise",
            payload={
                "iter": run.iterations,
                "attention_id": attention_node.id,
                "state_now": state_now_class,
                "state_next": state_next_class,
                **sv.to_dict(),
            },
        ))

        # Record edge in librarian (if configured) for cross-task transfer
        if self.librarian_writer is not None:
            try:
                action_class = self._compute_action_class(run, attention_node)
                outcome_label = "PASS" if not sv.any_surprised else "SURPRISE"
                self.librarian_writer.record_edge(
                    state_now_class,
                    action_class,
                    state_next_class,
                    int(actual_cost),
                    outcome_label,
                    task_id=contract.id,
                )
            except Exception:
                pass

        # AUTHORITY: call attestor on the current attention node to decide
        # whether the step is complete. This replaces the per-intent heuristic
        # as primary; heuristic still serves as the empty-tool-result short-circuit.
        attestor_done, attestor_reason = self._attestor_verdict(contract, run, attention_node)
        sv.details["attestor_reason"] = attestor_reason

        # Combine: attestor=YES is sufficient; otherwise fall back to per-intent heuristic
        if attestor_done:
            step_done = True
        else:
            # Attestor said NO. Still allow step_appears_done to override
            # ONLY if it's a strong positive (verifier/durable match for mutate/verify).
            step_done = False

        if step_done:
            self._default_advance(plan_tree, attention_node, run)
        elif sv.any_surprised and getattr(run, "restip_count", 0) < self.max_restip_per_task:
            ops_applied = self._run_inloop_stipulate(contract, run, plan_tree, sv, state_now_class)
            run.restip_count = getattr(run, "restip_count", 0) + 1  # type: ignore[attr-defined]
            if ops_applied == 0:
                att_id = attention_node.id
                run._stall_count = getattr(run, "_stall_count", {})  # type: ignore[attr-defined]
                run._stall_count[att_id] = run._stall_count.get(att_id, 0) + 1
                if run._stall_count[att_id] >= 3:
                    self._default_advance(plan_tree, attention_node, run)
        else:
            att_id = attention_node.id
            run._stall_count = getattr(run, "_stall_count", {})  # type: ignore[attr-defined]
            run._stall_count[att_id] = run._stall_count.get(att_id, 0) + 1
            if run._stall_count[att_id] >= 3:
                self._default_advance(plan_tree, attention_node, run)

    def _attestor_verdict(
        self, contract: Contract, run: ActivatorRun, attention_node,
    ) -> tuple[bool, str]:
        """Call the attestor on the current attention node + recent evidence.

        Authority for step-done. Returns (complete: bool, reason: str).
        Errors are swallowed (returns False, reason explaining the error).
        """
        try:
            # State summary: last few tool outputs + assistant text
            recent_tool_msgs: list[str] = []
            for m in reversed(run.messages):
                role = m.get("role", "")
                if role == "tool":
                    content = m.get("content", "")
                    if isinstance(content, str):
                        recent_tool_msgs.append(content[:800])
                if len(recent_tool_msgs) >= 2:
                    break
            state_summary = "\n---\n".join(reversed(recent_tool_msgs)) or "(no tool output yet)"
            # Add expects hint to the description for attestor's reference
            expects = attention_node.expects or {}
            extended_desc = (
                f"{attention_node.description}\n"
                f"  expected durable_change: {expects.get('durable_change', False)}; "
                f"files_modified: {expects.get('files_modified', [])}; "
                f"verifier_outcome: {expects.get('verifier_outcome', 'N/A')}"
            )
            complete, reason, llm_result = call_attestor(
                self.llm,
                step_description=extended_desc,
                state_summary=state_summary,
                parent_event_id=run.last_perform_event_id or run.last_iter_anchor,
            )
            self._charge_llm(contract, llm_result)
            return bool(complete), (reason or "")[:300]
        except Exception as e:
            return False, f"attestor error: {e}"[:300]

    def _compute_action_class(self, run: ActivatorRun, attention_node) -> str:
        """Cheap action class hash: tool_name + intent + phase_name."""
        import hashlib
        last_tool = "?"
        for m in reversed(run.messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_tool = m["tool_calls"][0].get("function", {}).get("name", "?")
                break
        key = f"{last_tool}|{attention_node.intent}|{attention_node.phase_name}"
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def _step_appears_done(self, run: ActivatorRun, attention_node, sv: SurpriseVector) -> bool:
        """Per-intent heuristic for whether the current attention node looks complete.

        investigate: needs a non-empty, non-error tool result
        mutate:      needs sv.durable_match AND sv.files_match (no surprise on those)
        verify:      needs sv.verifier_match (when expects.verifier_outcome != "N/A")
        design/other: needs a non-empty assistant response (we accept by default)
        """
        intent = attention_node.intent
        # Any mechanical surprise → not done
        if sv.cost_surprised:
            return False
        last_result = self._last_tool_result_text(run)
        if intent == "investigate":
            if not last_result:
                return False
            if last_result.startswith("[ERROR]"):
                return False
            # Crude "did this produce useful output?" heuristic
            stripped = last_result.strip()
            if len(stripped) < 30:
                return False
            return True
        if intent == "mutate":
            # mutate requires the durable + files dimensions to match
            return sv.durable_match and sv.files_match and not last_result.startswith("[ERROR]")
        if intent == "verify":
            # verify requires verifier_match (only relevant when expects.verifier_outcome != "N/A")
            if attention_node.expects.get("verifier_outcome", "N/A") == "N/A":
                # ran a verify-intent step without a specific predicted outcome — accept if no error
                return not last_result.startswith("[ERROR]")
            return sv.verifier_match
        # other / design
        return not last_result.startswith("[ERROR]")

    def _last_tool_result_text(self, run: ActivatorRun) -> str:
        for m in reversed(run.messages):
            if m.get("role") == "tool":
                content = m.get("content", "")
                return content if isinstance(content, str) else str(content)
        return ""

    def _default_advance(
        self, plan_tree: PlanTree, attention_node, run: ActivatorRun,
    ) -> None:
        """COMMIT the attention node and advance to the next pending leaf."""
        op = TreeEditOp(
            op="COMMIT",
            payload={"node_id": attention_node.id, "reason": "no surprise"},
            reflection="step appeared to advance without surprise",
        )
        ok, _ = plan_tree.apply_op(op, iter_n=run.iterations)
        if ok:
            self.recorder.append(TraceEvent(
                event_id=f"pl-{uuid.uuid4().hex[:6]}",
                parent_id=run.last_perform_event_id or run.last_iter_anchor,
                ts=utcnow_iso(),
                kind="plan_commit",
                payload={
                    "node_id": attention_node.id,
                    "reason": "no surprise",
                    "reflection": "step appeared to advance without surprise",
                },
            ))
        plan_tree.advance_attention()

    def _run_inloop_stipulate(
        self,
        contract: Contract,
        run: ActivatorRun,
        plan_tree: PlanTree,
        surprise: SurpriseVector,
        state_now_class: str,
    ) -> int:
        """Call in-loop Stipulator; apply returned ops; return # applied."""
        outline = plan_tree.render_outline()
        attention_id = plan_tree.attention_id
        state_summary = f"state_class={state_now_class[:8]}"
        recent_trace = self._recent_trace_text(run, n_iters=3)

        ops, llm_result = call_stipulator_inloop(
            self.llm,
            contract.obligation,
            plan_tree_outline=outline,
            attention_id=attention_id,
            state_class_summary=state_summary,
            recent_trace=recent_trace,
            surprise_dims=surprise.to_dict(),
            parent_event_id=run.last_perform_event_id or run.last_iter_anchor,
        )
        self._charge_llm(contract, llm_result)

        # Emit a trace event for the in-loop call
        self.recorder.append(TraceEvent(
            event_id=f"sip-{uuid.uuid4().hex[:6]}",
            parent_id=self.recorder.traj.events[-1].event_id if self.recorder.traj.events else None,
            ts=utcnow_iso(),
            kind="stipulate_inloop",
            payload={
                "iter": run.iterations,
                "surprise": surprise.to_dict(),
                "n_ops": len(ops or []),
            },
        ))

        if not ops:
            return 0

        applied = 0
        for op_dict in ops:
            kind = op_dict.get("op", "")
            tree_op = TreeEditOp(
                op=kind,
                payload=op_dict,
                reflection=op_dict.get("reflection", ""),
            )
            ok, err = plan_tree.apply_op(tree_op, iter_n=run.iterations)
            if ok:
                applied += 1
                self.recorder.append(TraceEvent(
                    event_id=f"pl-{uuid.uuid4().hex[:6]}",
                    parent_id=self.recorder.traj.events[-1].event_id if self.recorder.traj.events else None,
                    ts=utcnow_iso(),
                    kind=f"plan_{kind.lower()}",
                    payload={
                        "iter": run.iterations,
                        **{k: v for k, v in op_dict.items() if k in
                           ("node_id", "parent_id", "branch_id", "target_id",
                            "reason", "reflection")},
                    },
                ))
        return applied

    def _recent_trace_text(self, run: ActivatorRun, n_iters: int = 3) -> str:
        """Short text summary of the last N iters for the in-loop prompt."""
        # Take the last ~6 messages (3 turns worth)
        msgs = run.messages[-(n_iters * 2):]
        lines: list[str] = []
        for m in msgs:
            role = m.get("role", "")
            if role == "assistant" and m.get("tool_calls"):
                tc = m["tool_calls"][0]
                fn = tc.get("function", {})
                lines.append(f"  assistant→tool {fn.get('name', '?')}({fn.get('arguments', '{}')[:80]})")
            elif role == "tool":
                content = m.get("content", "")
                if isinstance(content, str):
                    lines.append(f"  tool_result: {content[:200]}")
            elif role == "system":
                continue  # skip system noise
        return "\n".join(lines)
