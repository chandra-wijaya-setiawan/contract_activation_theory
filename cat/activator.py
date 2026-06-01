"""Contract-disciplined agent loop (docs/iteration.md).

Subclasses the legacy base Activator (cat/_legacy/activator.py) to reuse
the unchanged scaffolding: _admit, _perform_once, _verify_criteria,
_charge_llm, _transition, _finalize, _record_state, _check_budget,
_init_executor_messages, _build_user_prompt, _prune_to_window.

run() implements the v2 flow:
    pre-loop:  REPRESENT → obligation_class → STIPULATE-phased → ACTIVATE
    iter:      PLAN (state_class, attention) → ACTION (_perform_once)
               → OBSERVE (surprise) → tree-edit (in-loop PlanPatcher on
               surprise) OR COMMIT + advance attention
    post-loop: FINALIZE plan tree (write to librarian if available)
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from cat._legacy.activator import (
    Activator as _BaseActivator,
    ActivatorRun,
    _PlanStep,
    PERFORMER_SYSTEM_PROMPT,
)
from cat.contracts import Contract, ContractState, State
from cat.llm import (
    call_stipulator_phased,
    call_plan_patcher,
    call_attestor,
)
from cat.obligation_class import compute_obligation_class
from cat.plan_tree import PlanTree, TreeEditOp
from cat.representer import Representer
from cat.state_class import RunCounts, compute_state_class
from cat.surprise import compute_surprise, SurpriseVector
from cat.trace import TraceEvent, utcnow_iso


PERFORMER_ATTENTION_SUFFIX = """
ATTENTION/LOOKAHEAD: a working plan tree is being maintained for you.
Each turn you see the current ATTENTION step (what to do now) and a short
LOOKAHEAD (what's coming next). Stay focused on the ATTENTION step.
Do not skip ahead. The activator will advance attention or revise the
plan based on observation.
"""


class Activator(_BaseActivator):
    """Contract-disciplined agent loop: phased plan tree, surprise vector,
    in-loop revisor. Inherits stipulate/admit/perform/verify utilities from
    cat/_legacy/activator.py (folded in once we no longer need the legacy
    flat-plan Activator for reference)."""

    def __init__(
        self,
        *args,
        max_restip_per_task: int = 5,
        stall_threshold: int = 3,
        max_backwards_per_task: int = 2,
        resolver: "Optional[Any]" = None,
        librarian_writer=None,  # optional: object with .record_edge / .record_plan_tree
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.max_restip_per_task = max_restip_per_task
        self.stall_threshold = stall_threshold
        self.max_backwards_per_task = max_backwards_per_task
        self.librarian_writer = librarian_writer
        # Default resolver is LLMResolver if not provided
        if resolver is None:
            from cat.resolver import LLMResolver
            resolver = LLMResolver(self.llm)
        self.resolver = resolver

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
        run.patch_plan_count = 0  # type: ignore[attr-defined]

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
            self._bind_oracle_to_verify_steps(plan_tree, contract)
            # Mirror the flat list for v1 admission gate compat
            flat_steps: list[_PlanStep] = []
            for phase_id in plan_tree.phase_order:
                phase = plan_tree.nodes[phase_id]
                for step_id in phase.children:
                    n = plan_tree.nodes[step_id]
                    flat_steps.append(_PlanStep(id=step_id, description=n.description))
            run.plan = flat_steps
        else:
            # R12 follow-up: phased stipulator exhausted its retry budget.
            # Do NOT fall back to v1 flat plan — that disables every piece of
            # v2 machinery (no surprise, no plan_commit, no PlanPatcher) and burns
            # tokens running "naked". Fail the contract cleanly instead.
            run.plan = []
            run.plan_tree = None  # type: ignore[attr-defined]
            self._transition(
                contract, ContractState.FAILED,
                "stipulator failed to emit a valid PhasedPlan after retries", run,
            )
            run.failure_reason = "stipulator_schema_failure"
            self._finalize(contract, run)
            return run

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
            iter_start_len = len(run.messages)

            # ACTION (Step 5+6: SELECT + PERFORM)
            keep_going = self._perform_once(contract, state, run)
            cost_after = contract.cost.tokens_in + contract.cost.tokens_out
            actual_cost = cost_after - cost_before

            # R19b: extract the Performer's outcome from the messages this
            # iteration appended. One centralized walk replaces the scattered
            # _infer_durable_change + _infer_verifier_outcome + [ERROR] checks.
            outcome = self._extract_performer_outcome(run, iter_start_len)
            run.last_outcome = outcome  # type: ignore[attr-defined]

            # R19.7-prep: emit a trace event with the Performer's actual text
            # response + tools called. Before this, JSONL captured only token
            # counts (no content), so any "why did the LLM act this way"
            # diagnostic was inference, not observation. This event makes the
            # LLM's reasoning visible for offline analysis.
            attn_intent_for_trace = (
                getattr(attention_node, "intent", None) if attention_node else None
            )
            self.recorder.append(TraceEvent(
                event_id=f"perf-{uuid.uuid4().hex[:6]}",
                parent_id=run.last_perform_event_id or run.last_iter_anchor,
                ts=utcnow_iso(),
                kind="performer_response",
                payload={
                    "iter": run.iterations,
                    "attention_id": attention_node.id if attention_node else None,
                    "attention_intent": attn_intent_for_trace,
                    "text_response": (outcome.text_response or "")[:4000],
                    "tools_called": [
                        {"name": inv.tool_name,
                         "args": {k: (str(v)[:200] if isinstance(v, str) else v)
                                  for k, v in (inv.args or {}).items()},
                         "succeeded": inv.succeeded}
                        for inv in outcome.invocations
                    ],
                    "any_tool_called": outcome.any_tool_called,
                    "any_durable_change": outcome.any_durable_change,
                    "any_tool_errored": outcome.any_tool_errored,
                    "is_terminal_for_intent": outcome.is_terminal(attn_intent_for_trace),
                    "finish_reason": outcome.finish_reason,
                },
            ))

            if not keep_going:
                # Defensive: distinguish a REAL "DONE" signal from a degenerate
                # LLM response (zero tokens, no text, no tool calls). The former
                # is the LLM saying it's finished; the latter is a transient
                # API error (rate limit, content filter, etc.) and should NOT
                # terminate the contract.
                degenerate = (actual_cost == 0) and not (run.final_text or "").strip()
                if degenerate:
                    # Treat as transient — emit a trace event, force-advance
                    # attention (so we don't loop here forever), and continue
                    self.recorder.append(TraceEvent(
                        event_id=f"err-{uuid.uuid4().hex[:6]}",
                        parent_id=run.last_perform_event_id or run.last_iter_anchor,
                        ts=utcnow_iso(),
                        kind="surprise",
                        payload={
                            "iter": run.iterations,
                            "error": "degenerate_llm_response",
                            "note": "performer returned 0 tokens / no text / no tools; treating as transient",
                        },
                    ))
                    if plan_tree and attention_node:
                        self._default_advance(plan_tree, attention_node, run)
                    # Reset run.final_text so v1 doesn't think we exited
                    run.final_text = None
                    # Don't break — continue the loop
                    continue

                # R19c: text-only response is only a real DONE when the
                # current attention step doesn't *require* a tool call.
                # On mutate/verify intent steps, "I'll modify the file"
                # without actually calling str_replace is the Performer
                # describing the action without executing it — bump the
                # stall counter and let the loop's Resolver/PlanPatcher
                # machinery handle it instead of silently exiting.
                attn_intent = getattr(attention_node, "intent", None) if attention_node else None
                if not outcome.is_terminal(attn_intent):
                    self.recorder.append(TraceEvent(
                        event_id=f"err-{uuid.uuid4().hex[:6]}",
                        parent_id=run.last_perform_event_id or run.last_iter_anchor,
                        ts=utcnow_iso(),
                        kind="surprise",
                        payload={
                            "iter": run.iterations,
                            "attention_id": attention_node.id if attention_node else None,
                            "attention_intent": attn_intent,
                            "error": "performer_text_only_on_action_step",
                            "note": (
                                "Performer returned text without a tool call on a "
                                f"{attn_intent!r}-intent step. Treating as stall (not DONE) "
                                "so the loop's stall + Resolver + PlanPatcher pipeline can engage."
                            ),
                        },
                    ))
                    # Bump the stall counter for this attention so Resolver
                    # fires once threshold is hit. Loop continues; the next
                    # iter will re-prompt the Performer with the same plan
                    # attention hint.
                    if attention_node is not None:
                        run._stall_count = getattr(run, "_stall_count", {})  # type: ignore[attr-defined]
                        run._stall_count[attention_node.id] = run._stall_count.get(attention_node.id, 0) + 1  # type: ignore[attr-defined]
                    run.final_text = None
                    continue

                # Real DONE: check criteria + terminate
                if self._verify_criteria(contract, state, run):
                    self._transition(contract, ContractState.FULFILLED,
                                     "executor stopped and criteria satisfied", run)
                else:
                    self._transition(contract, ContractState.FAILED,
                                     "executor stopped before criteria satisfied", run)
                    run.failure_reason = "executor_quit_early"
                break

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

            # P1 sliding window — drop oldest raw turn → compressed prior_attempts.
            # Same as v1.7 to prevent message history bloat (pytest-6202 burned
            # 218K performer-input tokens because of unbounded growth).
            turn_msgs = run.messages[iter_start_len:]
            self._prune_to_window(contract, run, turn_msgs)

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
    _ATTENTION_HINT_MARKER = "PLAN ATTENTION (focus on this one step):"

    def _bind_oracle_to_verify_steps(
        self,
        plan_tree: PlanTree,
        contract: Contract,
    ) -> int:
        """R16: bind verify-intent steps to the actual oracle command.

        The Stipulator emits verify steps with descriptions like 'Run the
        oracle test command...' but typically leaves `suggested_args` empty
        or with a guessed command. The Performer then guesses a pytest path,
        runs it, gets misleading 'passed' output, attestor approves, and
        the final criterion_check (which runs the REAL oracle) discovers
        nothing was actually validated.

        Fix: post-process the plan tree. For each verify-intent step that
        has no concrete bash command in suggested_args, inject the oracle
        command read from `contract.representations` (key='judge_strategy',
        format 'oracle: <cmd>'). The Performer then runs the exact oracle
        verbatim via the existing STIPULATOR SUGGESTS hint.

        Returns the number of verify steps bound (for trace / debugging).
        """
        # Pull the oracle command from representations (same source the
        # Activator already uses to inject the oracle criterion).
        oracle_cmd = ""
        for r in (contract.representations or []):
            if r.key == "judge_strategy" and r.value.startswith("oracle:"):
                oracle_cmd = r.value[len("oracle:"):].strip()
                break
        if not oracle_cmd:
            return 0

        bound = 0
        for node in plan_tree.nodes.values():
            if getattr(node, "intent", None) != "verify":
                continue
            # Don't overwrite a Stipulator-suggested command that already
            # contains a concrete invocation — only fill the gap when
            # suggested_args lacks a 'command' key.
            existing = getattr(node, "suggested_args", None) or {}
            if isinstance(existing, dict) and existing.get("command"):
                continue
            node.suggested_tool = "bash"  # type: ignore[attr-defined]
            node.suggested_args = {"command": oracle_cmd, "timeout_s": 300}  # type: ignore[attr-defined]
            bound += 1
        return bound

    def _extract_performer_outcome(
        self, run: ActivatorRun, iter_start_len: int,
    ) -> "PerformerOutcome":
        """R19: build a typed PerformerOutcome from the messages this iter
        appended. Centralizes interpretation of "what just happened?" so
        downstream consumers (observe, attestor, surprise) read a typed
        object instead of walking run.messages with their own helpers.

        Walks only run.messages[iter_start_len:] (the slice this iteration
        added), not the whole history.
        """
        from cat.plan_schema import PerformerOutcome, ToolInvocation
        _WRITE_TOOLS = ("str_replace", "multi_edit", "apply_patch", "write")

        new_msgs = run.messages[iter_start_len:]
        if not new_msgs:
            return PerformerOutcome()

        # The Performer always appends exactly one assistant message per turn
        # (possibly followed by tool result messages).
        asst = next((m for m in new_msgs if m.get("role") == "assistant"), None)
        if asst is None:
            return PerformerOutcome()

        text = asst.get("content", "") or ""
        tool_calls_raw = asst.get("tool_calls") or []

        invocations: list[ToolInvocation] = []
        for tc in tool_calls_raw:
            tc_id = tc.get("id", "")
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except Exception:
                args = {"_raw": args_raw}

            # Find the matching tool result message
            tool_msg = next(
                (m for m in new_msgs
                 if m.get("role") == "tool" and m.get("tool_call_id") == tc_id),
                None,
            )
            content = (tool_msg.get("content") if tool_msg else "") or ""
            if not isinstance(content, str):
                content = str(content)
            is_error = content.startswith("[ERROR]") or content.startswith("[skipped")

            # Derive durable_change + files_modified
            durable = False
            files: list[str] = []
            if not is_error and name in _WRITE_TOOLS:
                durable = True
                path = args.get("path") or args.get("file") or ""
                if path:
                    files.append(path)
                if name == "apply_patch":
                    patch = args.get("patch", "") or args.get("diff", "") or ""
                    for line in str(patch).splitlines():
                        if line.startswith("+++ b/") or line.startswith("--- a/"):
                            p = line[6:].strip()
                            if p and p not in files:
                                files.append(p)

            invocations.append(ToolInvocation(
                tool_name=name,
                args=args if isinstance(args, dict) else {"_raw": str(args)[:200]},
                succeeded=not is_error,
                output_text=content[:5000],
                error=content[7:300].strip() if is_error else "",
                durable_change=durable,
                files_modified=files,
            ))

        return PerformerOutcome(
            text_response=text,
            invocations=invocations,
            finish_reason="tool_calls" if invocations else "stop",
        )

    def _inject_plan_attention(self, run: ActivatorRun, plan_tree: PlanTree) -> None:
        """Inject the attention hint as a USER message.

        R5 v2: changed role from 'system' to 'user'.

        The hint MUST be the last message in the array so the LLM
        responds to it. A trailing 'system' message at the END (after
        assistant + tool messages) was empirically rejected by the
        deepseek/openrouter endpoint with degenerate 0/0 token
        responses. Using 'user' role keeps it a valid conversation
        turn the model knows to answer.

        Strip-and-replace: remove any prior attention hint (system OR
        user role with the marker) before appending the new one.
        Exactly one attention hint in messages at any time.
        """
        attention = plan_tree.get_attention()
        if not attention:
            return

        marker = self._ATTENTION_HINT_MARKER
        run.messages[:] = [
            m for m in run.messages
            if not (
                isinstance(m.get("content", ""), str)
                and marker in m["content"]
            )
        ]

        lookahead = plan_tree.lookahead(max_steps=4)
        lookahead_str = "\n".join(
            f"  - [{n.id}] ({n.intent}) {n.description[:100]}" for n in lookahead[1:]
        ) or "  (none)"

        # R6+ next_instruction from Resolver (if any) — injected once
        resolver_directive = getattr(run, "resolver_next_instruction", "") or ""
        resolver_block = ""
        if resolver_directive:
            resolver_block = (
                f"\nRESOLVER NEXT INSTRUCTION (apply to this turn):\n"
                f"  {resolver_directive}\n"
            )
            # Clear after consuming so it only fires once
            run.resolver_next_instruction = ""  # type: ignore[attr-defined]

        # R10b: surface Stipulator's tool suggestion (when present)
        sugg_tool = getattr(attention, "suggested_tool", None) or ""
        sugg_args = getattr(attention, "suggested_args", None) or {}
        if isinstance(sugg_args, dict) and sugg_tool:
            try:
                args_json = json.dumps(sugg_args, ensure_ascii=False)
            except Exception:
                args_json = str(sugg_args)
            suggestion_block = (
                f"\nSTIPULATOR SUGGESTS for this step:\n"
                f"  tool: {sugg_tool}\n"
                f"  args: {args_json[:400]}\n"
                f"  (follow if applicable; adapt if you see something better)\n"
            )
        else:
            suggestion_block = ""

        hint = (
            f"{marker}\n"
            f"  [{attention.id}] ({attention.phase_name}/{attention.intent}) {attention.description}\n"
            f"  expects: durable_change={attention.expects.get('durable_change', False)}, "
            f"files={attention.expects.get('files_modified', [])}, "
            f"verifier={attention.expects.get('verifier_outcome', 'N/A')}\n"
            f"{suggestion_block}"
            f"{resolver_block}"
            f"\nUPCOMING (informational, not for this turn):\n{lookahead_str}\n"
            f"\nAct on the current step. Reply with one tool call."
        )
        run.messages.append({"role": "user", "content": hint})

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

    def _infer_verifier_outcome(self, run: ActivatorRun) -> Optional[str]:
        """R12 Bug B: parse last test-running tool output for PASS/FAIL.

        Without this, surprise.verifier_match defaults vacuously True and a
        failing pytest can silently advance a verify step. Returns "PASS",
        "FAIL", or None when the last tool call wasn't a recognizable test
        invocation (so we don't false-positive on unrelated bash output).
        """
        if not run.messages:
            return None
        last_tool_msg_idx = None
        for i in range(len(run.messages) - 1, -1, -1):
            if run.messages[i].get("role") == "tool":
                last_tool_msg_idx = i
                break
        if last_tool_msg_idx is None:
            return None
        # Find the assistant tool_call that produced this result
        tool_name = ""
        cmd = ""
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
                cmd = (args.get("command", "") or "").lower()
                break

        # Only infer on test-runner commands: pytest, runtests.py, tox, unittest
        # (bash invocations of test runners) — or our dedicated oracle capability.
        is_test_invocation = (
            tool_name in ("oracle", "runtests") or
            (tool_name == "bash" and any(p in cmd for p in (
                "pytest", "runtests", "tox", "unittest", "django-admin test",
            )))
        )
        if not is_test_invocation:
            return None

        content = run.messages[last_tool_msg_idx].get("content", "") or ""
        if not isinstance(content, str):
            return None
        if content.startswith("[ERROR]"):
            return "FAIL"
        lc = content.lower()
        # Clear failure signatures first
        fail_markers = (
            " failed", "failed,", "failed]", "= fail", "errored",
            "traceback (most recent call last)", "internalerror",
            "no module named",
        )
        if any(m in lc for m in fail_markers):
            return "FAIL"
        # FAILED in caps (pytest summary)
        if "FAILED" in content or "ERROR" in content[-2000:]:
            return "FAIL"
        # Pass signatures
        if "passed" in lc and "failed" not in lc:
            return "PASS"
        # "no tests ran" — vacuously OK but the verify step is meaningless;
        # treat as None so we don't false-advance.
        if "no tests ran" in lc:
            return None
        return None

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
        # R12 Bug A: when the cold-start stipulator failed to emit a phased
        # plan, we fell through to the v1 flat-plan path with plan_tree=None
        # and attention_node=None. Dereferencing attention_node.intent below
        # would crash; the try/except wrapping the caller would then spin
        # without ever advancing. Skip observation entirely in that case.
        if attention_node is None or plan_tree is None:
            return

        # R19d: read the actual signals off run.last_outcome (PerformerOutcome).
        # Previously this called _infer_durable_change + _infer_verifier_outcome,
        # each of which walked run.messages backwards re-deriving the same data.
        # PerformerOutcome centralizes that derivation in one place per iter.
        outcome = getattr(run, "last_outcome", None)
        if outcome is not None:
            actual_durable_change = outcome.any_durable_change
            actual_files = outcome.files_modified
            actual_verifier_outcome = outcome.verifier_outcome
        else:
            # Defensive fallback: shouldn't happen post-R19b but keeps the
            # contract robust if some path bypasses _perform_once.
            actual_durable_change, actual_files = self._infer_durable_change(run)
            actual_verifier_outcome = self._infer_verifier_outcome(run)

        # Update counts
        run.run_counts.bump(attention_node.intent)  # type: ignore[attr-defined]
        if actual_durable_change:
            run.run_counts.mark_durable_change()  # type: ignore[attr-defined]
        else:
            run.run_counts.tick_no_change()  # type: ignore[attr-defined]

        state_next_class = compute_state_class(
            oc, attention_node.phase_name, attention_node.intent, run.run_counts  # type: ignore[attr-defined]
        )

        # Surprise vector (mechanical signals only)
        sv = compute_surprise(
            expects=attention_node.expects,
            actual_cost=float(actual_cost),
            actual_durable_change=actual_durable_change,
            actual_files_modified=actual_files,
            actual_verifier_outcome=actual_verifier_outcome,
        )

        # R7: step_done requires CONSISTENCY across all 7 dimensions
        # (per docs/iteration.md §4b). Attestor is ONE dimension, not the
        # gatekeeper. A mutate step cannot be 'done' when durable_match or
        # files_match is False — even if the attestor said YES.
        attestor_done, attestor_reason = self._attestor_verdict(contract, run, attention_node)
        sv.details["attestor_reason"] = attestor_reason
        sv.details["attestor_done"] = "yes" if attestor_done else "no"

        # step_done = attestor_done AND no mechanical dimension diverges on
        # the dims that matter for this intent.
        # For mutate intent: durable_match AND files_match must hold.
        # For verify intent: verifier_match must hold (when expected != N/A).
        # For investigate / design / other: mechanical dims rarely diverge
        # (expects.durable_change=False, etc.) so attestor effectively rules.
        critical_dim_violated = False
        if attention_node.intent == "mutate":
            if not (sv.durable_match and sv.files_match):
                critical_dim_violated = True
                sv.details["block_step_done"] = (
                    "mutate intent requires durable_match AND files_match"
                )
        elif attention_node.intent == "verify":
            ev = attention_node.expects.get("verifier_outcome", "N/A") if isinstance(attention_node.expects, dict) else "N/A"
            if ev in ("PASS", "FAIL") and not sv.verifier_match:
                critical_dim_violated = True
                sv.details["block_step_done"] = (
                    "verify intent requires verifier_match"
                )

        if critical_dim_violated:
            step_done = False
        elif attestor_done:
            step_done = True
        elif self._mechanical_evidence_strong(attention_node, sv):
            step_done = True
            sv.details["override"] = "mechanical evidence overrode attestor=NO"
        else:
            step_done = False

        # Record surprise event (now includes attestor verdict)
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
                "step_done": step_done,
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

        # Track stall count per attention node (resets when attention moves)
        att_id = attention_node.id
        run._stall_count = getattr(run, "_stall_count", {})  # type: ignore[attr-defined]
        run.backward_count = getattr(run, "backward_count", 0)  # type: ignore[attr-defined]

        if step_done:
            # Natural progress — COMMIT + advance
            self._default_advance(plan_tree, attention_node, run)
            run._stall_count[att_id] = 0
            return

        # Step is NOT done. Increment stall counter for this node.
        run._stall_count[att_id] = run._stall_count.get(att_id, 0) + 1
        below_stall_threshold = run._stall_count[att_id] < self.stall_threshold

        if below_stall_threshold:
            # Below threshold → let the LLM (Performer) take another shot
            # at the same attention next iter. No Resolver call yet.
            return

        # Stall threshold hit — ESCALATE to Resolver
        output = self._invoke_resolver(
            contract, run, plan_tree, attention_node, sv, state_now_class,
        )
        self._apply_resolver_decision(
            contract, run, plan_tree, attention_node, output.decision, sv, state_now_class,
        )

    def _invoke_resolver(
        self,
        contract: Contract,
        run: ActivatorRun,
        plan_tree: PlanTree,
        attention_node,
        sv: SurpriseVector,
        state_now_class: str,
    ):
        """Build a StallContext, invoke the Resolver, apply safety guards."""
        from cat.resolver import StallContext, ResolverOutput, enforce_guards

        # Build recent tool calls list from message history (pair assistant tool_calls with tool result)
        recent: list[dict[str, Any]] = []
        intent_counts: dict[str, int] = {"investigate": 0, "mutate": 0, "verify": 0, "other": 0}
        files_actually_modified: list[str] = []
        write_tools = {"str_replace", "multi_edit", "apply_patch", "write"}
        # Walk forward to count tools by intent over this whole run
        for i in range(len(run.messages)):
            m = run.messages[i]
            if m.get("role") != "assistant":
                continue
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) or {}
                tool_name = fn.get("name", "?")
                # Classify intent of tool
                if tool_name in write_tools:
                    intent_counts["mutate"] += 1
                    try:
                        args = json.loads(fn.get("arguments", "{}") or "{}")
                        path = args.get("path") or args.get("file") or ""
                        if path:
                            files_actually_modified.append(path)
                    except Exception:
                        pass
                elif tool_name in ("read", "outline", "rg", "fd", "find_refs", "goto_def", "list"):
                    intent_counts["investigate"] += 1
                elif tool_name == "bash":
                    # bash is ambiguous; classify by command verbs
                    try:
                        args = json.loads(fn.get("arguments", "{}") or "{}")
                        cmd = (args.get("command") or "").lower()
                        if "pytest" in cmd or "django test" in cmd or "python -m test" in cmd:
                            intent_counts["verify"] += 1
                        elif "grep " in cmd or "find " in cmd or "ls " in cmd:
                            intent_counts["investigate"] += 1
                        else:
                            intent_counts["other"] += 1
                    except Exception:
                        intent_counts["other"] += 1
                else:
                    intent_counts["other"] += 1

                # Pair with the matching tool result
                output_summary = ""
                for j in range(i + 1, min(i + 5, len(run.messages))):
                    if run.messages[j].get("tool_call_id") == tc.get("id"):
                        c = run.messages[j].get("content", "")
                        output_summary = c[:120] if isinstance(c, str) else ""
                        break
                recent.append({
                    "tool": tool_name,
                    "args": fn.get("arguments", "")[:120],
                    "output_summary": output_summary,
                })

        # Collect recent attestor reasons from trace events (last few)
        attestor_reasons: list[str] = []
        try:
            for e in reversed(self.recorder.traj.events[-30:]):
                if e.kind == "surprise":
                    p = e.payload or {}
                    details = p.get("details", {}) or {}
                    ar = details.get("attestor_reason")
                    if ar:
                        attestor_reasons.append(str(ar)[:200])
                if len(attestor_reasons) >= 3:
                    break
            attestor_reasons.reverse()
        except Exception:
            pass

        expects = attention_node.expects if isinstance(attention_node.expects, dict) else {}
        files_expected = list(expects.get("files_modified") or [])

        ctx = StallContext(
            attention_node_id=attention_node.id,
            attention_description=attention_node.description,
            attention_intent=attention_node.intent,
            attention_phase=attention_node.phase_name,
            expects=expects,
            surprise_dims=sv.to_dict(),
            stall_count=run._stall_count.get(attention_node.id, 0),  # type: ignore[attr-defined]
            patch_plan_count=getattr(run, "patch_plan_count", 0),
            plan_tree_outline=plan_tree.render_outline(),
            recent_tool_calls=recent[-6:],
            obligation_brief=(contract.obligation or "")[:500],
            state_now_class=state_now_class,
            attestor_reasons=attestor_reasons,
            files_expected=files_expected,
            files_actually_modified=files_actually_modified[-5:],
            intent_tool_counts=intent_counts,
        )

        try:
            output = self.resolver.resolve(ctx)
        except Exception:
            output = ResolverOutput(decision="forward", reason="resolver crashed; safest")

        # Apply deterministic guards (the floor)
        output = enforce_guards(output, ctx)

        # Stash next_instruction so the next iter's attention hint can inject it
        run.resolver_next_instruction = output.next_instruction  # type: ignore[attr-defined]

        # Trace event for the escalation — now includes reflection fields
        self.recorder.append(TraceEvent(
            event_id=f"res-{uuid.uuid4().hex[:6]}",
            parent_id=run.last_perform_event_id or run.last_iter_anchor,
            ts=utcnow_iso(),
            kind="surprise",
            payload={
                "iter": run.iterations,
                "resolver_escalation": True,
                "attention_id": attention_node.id,
                "stall_count": run._stall_count.get(attention_node.id, 0),  # type: ignore[attr-defined]
                **output.to_dict(),
            },
        ))
        return output

    def _apply_resolver_decision(
        self,
        contract: Contract,
        run: ActivatorRun,
        plan_tree: PlanTree,
        attention_node,
        decision: str,
        sv: SurpriseVector,
        state_now_class: str,
    ) -> None:
        """Translate the Resolver's decision into concrete actions."""
        att_id = attention_node.id

        if decision == "forward":
            # Force COMMIT + advance, same as if step was done
            self._default_advance(plan_tree, attention_node, run)
            run._stall_count[att_id] = 0  # type: ignore[attr-defined]

        elif decision == "patch_plan":
            if getattr(run, "patch_plan_count", 0) >= self.max_restip_per_task:
                # Cap hit — fall back to forward
                self._default_advance(plan_tree, attention_node, run)
                run._stall_count[att_id] = 0  # type: ignore[attr-defined]
                return
            ops_applied = self._run_plan_patcher(contract, run, plan_tree, sv, state_now_class)
            run.patch_plan_count = getattr(run, "patch_plan_count", 0) + 1  # type: ignore[attr-defined]
            if ops_applied == 0:
                # Re-stipulate returned nothing — force forward
                self._default_advance(plan_tree, attention_node, run)
                run._stall_count[att_id] = 0  # type: ignore[attr-defined]
            else:
                # Successfully applied ops — reset stall counter for current attention
                if plan_tree.attention_id and plan_tree.attention_id in plan_tree.nodes:
                    run._stall_count[plan_tree.attention_id] = 0  # type: ignore[attr-defined]

        elif decision == "backward":
            if getattr(run, "backward_count", 0) >= self.max_backwards_per_task:
                # Backward cap hit — fall back to forward
                self._default_advance(plan_tree, attention_node, run)
                run._stall_count[att_id] = 0  # type: ignore[attr-defined]
                return
            self._apply_backward(plan_tree, attention_node, run)
            run.backward_count = getattr(run, "backward_count", 0) + 1  # type: ignore[attr-defined]
            run._stall_count[att_id] = 0  # type: ignore[attr-defined]

        elif decision == "give_up":
            # Mark contract FAILED with explicit reason
            self._transition(
                contract, ContractState.FAILED,
                f"resolver gave up at {att_id} after {run._stall_count[att_id]} iters",  # type: ignore[attr-defined]
                run,
            )
            run.failure_reason = f"resolver_gave_up_at_{att_id}"

        else:
            # Unknown decision — treat as forward to keep loop progressing
            self._default_advance(plan_tree, attention_node, run)
            run._stall_count[att_id] = 0  # type: ignore[attr-defined]

    def _apply_backward(self, plan_tree: PlanTree, attention_node, run: ActivatorRun) -> None:
        """Backward op: DROP the current subtree, restore attention to the
        last completed mutate ancestor (or root if none)."""
        # Find last completed mutate node before current attention
        target_id: Optional[str] = None
        order: list[str] = []
        for phase_id in plan_tree.phase_order:
            for child_id in plan_tree.nodes[phase_id].children:
                order.append(child_id)
        try:
            cur_idx = order.index(attention_node.id) if attention_node.id in order else len(order)
        except ValueError:
            cur_idx = len(order)
        for nid in order[:cur_idx][::-1]:
            n = plan_tree.nodes[nid]
            if n.status == "complete" and n.intent == "mutate":
                target_id = nid
                break
        # DROP current
        plan_tree.apply_op(
            TreeEditOp(
                op="DROP",
                payload={"node_id": attention_node.id, "reason": "resolver: backward"},
                reflection="resolver: backward — current subtree abandoned",
            ),
            iter_n=run.iterations,
        )
        self.recorder.append(TraceEvent(
            event_id=f"pl-{uuid.uuid4().hex[:6]}",
            parent_id=run.last_perform_event_id or run.last_iter_anchor,
            ts=utcnow_iso(),
            kind="plan_drop",
            payload={"node_id": attention_node.id, "reason": "resolver: backward",
                     "reflection": "resolver: backward — current subtree abandoned"},
        ))
        # If we found a mutate ancestor, advance to next pending leaf after it
        if target_id:
            # Reset attention by advancing from target_id's position
            nxt = plan_tree._next_pending_leaf_after(target_id)
            if nxt and nxt in plan_tree.nodes:
                plan_tree.nodes[nxt].status = "active"
                plan_tree.attention_id = nxt
                self.recorder.append(TraceEvent(
                    event_id=f"pl-{uuid.uuid4().hex[:6]}",
                    parent_id=run.last_perform_event_id or run.last_iter_anchor,
                    ts=utcnow_iso(),
                    kind="plan_checkout",
                    payload={"node_id": nxt, "reflection": "resolver: backward — restored attention"},
                ))
                return
        # No mutate ancestor — advance to next pending leaf overall
        plan_tree.advance_attention()

    def _mechanical_evidence_strong(self, attention_node, sv: SurpriseVector) -> bool:
        """Override-attestor-NO when mechanical signals are unambiguously positive.

        - mutate intent: durable_match AND files_match AND expected_durable=True
        - verify intent: verifier_match AND expects.verifier_outcome in {"PASS","FAIL"}
        For investigate / design / other intents, attestor remains authoritative.
        """
        intent = getattr(attention_node, "intent", "other")
        expects = attention_node.expects or {}
        if intent == "mutate":
            return (
                bool(expects.get("durable_change", False))
                and sv.durable_match
                and sv.files_match
            )
        if intent == "verify":
            expected_v = expects.get("verifier_outcome", "N/A")
            return expected_v in ("PASS", "FAIL") and sv.verifier_match
        return False

    def _attestor_verdict(
        self, contract: Contract, run: ActivatorRun, attention_node,
    ) -> tuple[bool, str]:
        """Call the attestor on the current attention node + recent evidence.

        Authority for step-done. Returns (complete: bool, reason: str).
        Errors / degenerate responses fall back to False (step not done)
        so the loop continues safely rather than crashing or false-committing.
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
            # R9: build attestor input with the expects.result done-criterion as
            # the explicit contract the attestor judges against.
            expects = attention_node.expects if isinstance(attention_node.expects, dict) else {}
            # Backward-compat: read expects.result first; fall back to old name
            # 'knowledge_gained' so legacy plans still attest sensibly.
            expected_result = (
                str(expects.get("result", "")
                    or expects.get("knowledge_gained", "")).strip()
            )
            extended_desc = (
                f"description: {attention_node.description}\n"
                f"intent: {attention_node.intent}\n"
                f"expected_result: {expected_result or '(none specified)'}\n"
                f"mechanical_predictions: durable_change={expects.get('durable_change', False)}, "
                f"files_modified={expects.get('files_modified', [])}, "
                f"verifier_outcome={expects.get('verifier_outcome', 'N/A')}"
            )
            complete, reason, llm_result = call_attestor(
                self.llm,
                step_description=extended_desc,
                state_summary=state_summary,
                parent_event_id=run.last_perform_event_id or run.last_iter_anchor,
            )
            self._charge_llm(contract, llm_result)
            # Degenerate attestor response (no tokens) → conservative False
            if llm_result is None or (
                getattr(llm_result, "tokens_in", 0) == 0
                and getattr(llm_result, "tokens_out", 0) == 0
            ):
                return False, "attestor returned empty response (fallback)"
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

    def _run_plan_patcher(
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

        try:
            ops, llm_result = call_plan_patcher(
                self.llm,
                contract.obligation,
                plan_tree_outline=outline,
                attention_id=attention_id,
                state_class_summary=state_summary,
                recent_trace=recent_trace,
                surprise_dims=surprise.to_dict(),
                parent_event_id=run.last_perform_event_id or run.last_iter_anchor,
            )
        except Exception as e:
            # Fallback: don't crash on LLM failure
            return 0
        if llm_result is not None:
            self._charge_llm(contract, llm_result)
        ops = ops or []  # ensure never None downstream

        # Emit a trace event for the in-loop call
        self.recorder.append(TraceEvent(
            event_id=f"sip-{uuid.uuid4().hex[:6]}",
            parent_id=self.recorder.traj.events[-1].event_id if self.recorder.traj.events else None,
            ts=utcnow_iso(),
            kind="plan_patch",
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
