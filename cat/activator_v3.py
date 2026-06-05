"""CAT v3 — Hypothesis-based loop engine.

Replaces the plan-tree + Resolver + PlanPatcher architecture with a
clean hypothesis search loop:

  Outer: Stipulator → hypothesis → Performer → oracle → (repeat)
  Inner: Performer implements hypothesis in K attempts, self-verifies

See design.v3.md for the full architecture spec.
See plan.v3.md for the migration plan.

USE_V3=true env var routes swe_bench to this activator.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from cat.activator import Activator  # v2 activator — has librarian_writer, _represent, etc.
from cat._legacy.activator import (
    ActivatorRun,
    ContractState,
)
from cat.capabilities import PERFORMER_DONE_MARKER, PERFORMER_GIVE_UP_MARKER
from cat.llm import (
    PERFORMER_V3_SYSTEM_PROMPT,
    call_stipulator_v3,
    call_attestor_v3,
)
from cat.schemas import (
    HypothesisSchema,
    PerformerTaskSchema,
    KnowledgeSchema,
    AttestorV3Schema,
    InvocationSchema as ToolInvocation,
    OutcomeSchema as PerformerOutcome,
)
from cat.trace import TraceEvent, utcnow_iso

MAX_HYPOTHESES = 3
MAX_INNER_ATTEMPTS = 5


class ActivatorV3(Activator):
    """v3 hypothesis-based activator.

    Inherits admission, tool dispatch, criteria check, and budget
    enforcement from the base Activator. Replaces run() with the
    hypothesis loop.
    """

    # ── Admission override ──────────────────────────────────────────────

    def _admit(self, contract: Any, state: Any, run: ActivatorRun):  # type: ignore[override]
        """v3 override: skip the 'empty plan' check — v3 uses hypotheses not a plan."""
        from cat.contracts import AdmissionDecision
        import time

        if not (contract.obligation or "").strip():
            return AdmissionDecision.reject("boundary", "empty obligation")
        if not contract.criteria:
            return AdmissionDecision.reject("boundary", "no criteria declared")
        # NOTE: no run.plan check — v3 generates hypotheses at runtime, not a pre-baked plan
        cons = contract.constraints
        if cons.max_tokens_total <= 0 or cons.max_tool_calls <= 0 or cons.max_llm_calls <= 0:
            return AdmissionDecision.reject("boundary", "non-positive constraint bounds")
        now = time.time()
        if cons.deadline_ts is not None and now > cons.deadline_ts:
            return AdmissionDecision.reject("boundary", "deadline already passed")
        if not contract.conditions_met(state):
            return AdmissionDecision.reject("environment", "declared conditions not met")

        # Grant full capability set from registry
        grant = set(self.registry.names()) if self.registry else {
            "bash", "read", "write", "str_replace", "list",
            "rg", "fd", "multi_edit", "apply_patch",
            "goto_def", "find_refs", "outline", "git_diff", "git_status",
            "done", "give_up",
        }
        return AdmissionDecision.admit(grant)

    # ── Outer loop ──────────────────────────────────────────────────────

    def run(self, contract: Any, state: Any) -> ActivatorRun:
        """v3 discharge loop: stipulate hypothesis → perform → oracle → repeat."""
        run = ActivatorRun(contract=contract, state=state)

        # Represent: run environment probes (same as base)
        from cat.representer import Representer
        representer = Representer(registry=self.registry, recorder=self.recorder)
        pre_reps = list(contract.representations or [])
        contract.representations = pre_reps + representer.represent(
            contract, state, parent_event_id=run.last_state_event_id,
        )
        self._maybe_inject_oracle_criterion(contract)

        # Admit: v3 override skips the 'empty plan' check
        decision = self._admit(contract, state, run)
        if not decision.ok:
            self._apply_admission_failure(contract, run, decision)
            self._finalize(contract, run)
            return run

        run.grant = decision.grant
        contract.grant = decision.grant
        self._transition(contract, ContractState.ELIGIBLE, "admission gate passed", run)
        self._transition(contract, ContractState.ACTIVE, "discharge begins", run)
        self._init_executor_messages(contract, run)

        # Extract oracle command from representations
        oracle_cmd = self._extract_oracle(contract)

        knowledge = KnowledgeSchema()
        prior_attempts: list[dict] = []

        for hypothesis_n in range(MAX_HYPOTHESES):
            if not self._check_budget(contract, state, run):
                break

            # Stipulate: generate ONE hypothesis
            hypothesis, stip_result = call_stipulator_v3(
                self.llm,
                obligation=contract.obligation,
                representations=contract.representations,
                knowledge=knowledge,
                prior_attempts=prior_attempts,
                parent_event_id=run.last_state_event_id,
            )
            self._charge_llm(contract, stip_result)

            if hypothesis is None:
                self.recorder.append(TraceEvent(
                    event_id=f"h-fail-{uuid.uuid4().hex[:6]}",
                    parent_id=run.last_state_event_id,
                    ts=utcnow_iso(),
                    kind="hypothesis_attempt",
                    payload={"n": hypothesis_n + 1, "error": "stipulator returned no hypothesis"},
                ))
                break

            self.recorder.append(TraceEvent(
                event_id=f"h-{uuid.uuid4().hex[:6]}",
                parent_id=run.last_state_event_id,
                ts=utcnow_iso(),
                kind="hypothesis_attempt",
                payload={
                    "n": hypothesis_n + 1,
                    "location": hypothesis.location,
                    "change": hypothesis.change,
                    "predicts": hypothesis.predicts,
                    "basis": hypothesis.basis,
                },
            ))

            # Perform: implement the hypothesis (inner react loop)
            task = PerformerTaskSchema(
                obligation=contract.obligation,
                hypothesis=hypothesis,
                oracle=oracle_cmd,
                context=self._build_context(knowledge),
                max_attempts=MAX_INNER_ATTEMPTS,
            )
            result = self._perform_hypothesis(contract, state, run, task)

            # Knowledge update regardless of outcome
            self._update_knowledge(knowledge, result, oracle_cmd)
            prior_attempts.append({
                "hypothesis": hypothesis,
                "verdict": result.verdict,
                "reason": result.performer_reason,
            })

            self.recorder.append(TraceEvent(
                event_id=f"h-result-{uuid.uuid4().hex[:6]}",
                parent_id=run.last_state_event_id,
                ts=utcnow_iso(),
                kind="knowledge_update",
                payload={
                    "n": hypothesis_n + 1,
                    "verdict": result.verdict,
                    "reason": result.performer_reason[:300],
                    "findings_count": len(knowledge.findings),
                },
            ))

            # Oracle is the ONLY judge
            if self._verify_criteria(contract, state, run):
                self._transition(contract, ContractState.FULFILLED,
                                 f"hypothesis {hypothesis_n + 1} satisfied oracle", run)
                self._finalize(contract, run)
                return run

            # Record oracle failure
            oracle_failure = self._get_last_test_output(run)
            if oracle_failure:
                knowledge.oracle_failures.append(oracle_failure[:400])

            # Attestor: decide whether to try again or give up
            # Only called when oracle failed (not every iter)
            if result.verdict == "give_up" or hypothesis_n == MAX_HYPOTHESES - 1:
                attest_result, attest_llm = call_attestor_v3(
                    self.llm,
                    performer_result=result,
                    knowledge=knowledge,
                    prior_attempts=prior_attempts,
                    parent_event_id=run.last_state_event_id,
                )
                self._charge_llm(contract, attest_llm)
                if attest_result.verdict == "give_up":
                    self.recorder.append(TraceEvent(
                        event_id=f"attest-giveup-{uuid.uuid4().hex[:6]}",
                        parent_id=run.last_state_event_id,
                        ts=utcnow_iso(),
                        kind="attestor_give_up",
                        payload={"reason": attest_result.reason, "diagnosis": attest_result.diagnosis},
                    ))
                    break

        # All hypotheses exhausted
        if contract.state == ContractState.ACTIVE:
            self._transition(contract, ContractState.FAILED,
                             f"all {MAX_HYPOTHESES} hypotheses exhausted", run)
            run.failure_reason = "hypotheses_exhausted"

        self._finalize(contract, run)
        return run

    # ── Inner loop ───────────────────────────────────────────────────────

    def _perform_hypothesis(
        self,
        contract: Any,
        state: Any,
        run: ActivatorRun,
        task: PerformerTaskSchema,
    ) -> PerformerOutcome:
        """Inner react loop: Performer implements one hypothesis."""
        # Build initial prompt
        hypothesis_prompt = (
            f"OBLIGATION:\n{task.obligation}\n\n"
            f"HYPOTHESIS:\n"
            f"  Location: {task.hypothesis.location}\n"
            f"  Change:   {task.hypothesis.change}\n"
            f"  Predicts: {task.hypothesis.predicts}\n"
            f"  Basis:    {task.hypothesis.basis}\n\n"
            f"ORACLE (run after any change to verify):\n{task.oracle}\n"
        )
        if task.context:
            hypothesis_prompt += f"\nCONTEXT (already known — do not re-investigate):\n{task.context}\n"

        # V3 inner loop uses its own isolated message thread.
        # Does NOT use run.messages (which is the v2 flat-plan thread).
        # The Performer sees: system prompt + hypothesis task + accumulated tool results.
        inner_messages: list[dict] = [
            {"role": "user", "content": hypothesis_prompt}
        ]

        all_invocations: list[ToolInvocation] = []
        verdict = "exhausted"
        performer_reason = ""

        tools = self.registry.tool_schemas(only=run.grant) if self.registry else []

        for attempt in range(1, task.max_attempts + 1):
            if not self._check_budget(contract, state, run):
                verdict = "exhausted"
                performer_reason = "budget exhausted"
                break

            # Direct LLM call with isolated message thread
            messages = [
                {"role": "system", "content": PERFORMER_V3_SYSTEM_PROMPT},
            ] + inner_messages

            llm_result = self.llm.chat(
                messages=messages,
                tools=tools,
                tool_choice="required",
                role_tag="performer",
                temperature=0.2,
                parent_event_id=run.last_perform_event_id or run.last_state_event_id,
            )
            self._charge_llm(contract, llm_result)
            run.last_perform_event_id = f"perf-v3-{uuid.uuid4().hex[:6]}"

            # Build assistant message
            asst_msg: dict = {"role": "assistant", "content": llm_result.text or ""}
            tool_calls_raw = llm_result.tool_calls or []
            if tool_calls_raw:
                asst_msg["tool_calls"] = tool_calls_raw
            inner_messages.append(asst_msg)

            # Dispatch tool calls
            iter_invocations: list[ToolInvocation] = []
            keep_going = bool(tool_calls_raw)

            for tc in tool_calls_raw:
                tc_id = tc.get("id", f"tc-{uuid.uuid4().hex[:6]}")
                fn = tc.get("function", {}) or {}
                tool_name = fn.get("name", "")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except Exception:
                    args = {}

                # Dispatch to registry
                tr = self.registry.invoke(
                    tool_name, args, state,
                    recorder=self.recorder,
                    contract_id=contract.id,
                    tool_call_id=tc_id,
                    parent_event_id=run.last_perform_event_id,
                )
                self._charge_tool(contract, tr)

                content = tr.output if not tr.is_error else f"[ERROR] {tr.output}"
                inner_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": content,
                })

                # Build InvocationSchema
                is_err = tr.is_error
                durable = not is_err and tool_name in ("str_replace", "multi_edit", "apply_patch", "write")
                files: list[str] = []
                if durable:
                    path = args.get("path") or ""
                    if path:
                        files.append(path)

                reason_arg = args.pop("reason", "") if isinstance(args, dict) else ""
                inv = ToolInvocation(
                    tool_name=tool_name,
                    args=args,
                    reason=str(reason_arg)[:300],
                    attempt=attempt,
                    succeeded=not is_err,
                    execution_status="error" if is_err else "success",
                    output_text=content[:3000],
                    error=content[7:300] if is_err else "",
                    durable_change=durable,
                    files_modified=files,
                )
                iter_invocations.append(inv)
                all_invocations.append(inv)

            # Build outcome for this attempt
            inner_outcome = PerformerOutcome(
                text_response=llm_result.text or "",
                invocations=iter_invocations,
                finish_reason="tool_calls" if tool_calls_raw else "stop",
            )

            # Check for exit signals (only successful done/give_up)
            done_inv = next(
                (inv for inv in inner_outcome.invocations
                 if inv.tool_name == "done" and inv.succeeded), None)
            give_up_inv = next(
                (inv for inv in inner_outcome.invocations
                 if inv.tool_name == "give_up" and inv.succeeded), None)

            if done_inv:
                verdict = "completed"
                performer_reason = (
                    done_inv.args.get("reason", "")
                    or done_inv.output_text.replace(PERFORMER_DONE_MARKER, "").strip()
                )
                break

            if give_up_inv:
                verdict = "give_up"
                performer_reason = (
                    give_up_inv.args.get("reason", "")
                    or give_up_inv.output_text.replace(PERFORMER_GIVE_UP_MARKER, "").strip()
                )
                break

            if not keep_going:
                verdict = "completed"
                performer_reason = "Performer stopped (implicit done)"
                break

            # Oracle check after any durable change
            if inner_outcome.any_durable_change and not inner_outcome.any_tool_errored:
                if self._verify_criteria(contract, state, run):
                    verdict = "completed"
                    performer_reason = "oracle passed after durable change"
                    break

        else:
            # Exhausted — summarisation call
            try:
                last_outputs = "\n---\n".join(
                    f"attempt {inv.attempt} {inv.tool_name}: {inv.output_text[:200]}"
                    for inv in all_invocations[-3:]
                )
                summary_msgs = [
                    {"role": "system", "content": (
                        "Summarise in 2-3 sentences: what was tried, what blocked "
                        "completion, what info would help. Be specific."
                    )},
                    {"role": "user", "content": (
                        f"Hypothesis: {task.hypothesis.change} at {task.hypothesis.location}\n\n"
                        f"Last tool outputs:\n{last_outputs}"
                    )},
                ]
                summary = self.llm.chat(summary_msgs, role_tag="performer")
                self._charge_llm(contract, summary)
                performer_reason = summary.text or "Exhausted max attempts"
            except Exception:
                performer_reason = "Exhausted max attempts"

        combined = PerformerOutcome(
            text_response="",
            invocations=all_invocations,
            finish_reason="inner_loop",
        )
        combined.verdict = verdict
        combined.performer_reason = performer_reason
        return combined

    # ── Helpers ──────────────────────────────────────────────────────────

    def _extract_oracle(self, contract: Any) -> str:
        """Extract the oracle test command from contract representations."""
        for rep in (contract.representations or []):
            key = getattr(rep, "key", "")
            if key in ("eval_command", "oracle_command", "test_command"):
                val = getattr(rep, "value", "") or getattr(rep, "evidence", "")
                if val and len(val.strip()) > 5:
                    return val.strip()[:500]
        # Fallback: look for oracle in criteria
        for c in getattr(contract, "criteria", []):
            cmd = getattr(c, "command", "") or getattr(c, "cmd", "")
            if cmd:
                return cmd[:500]
        return "(oracle command not found in representations)"

    def _build_context(self, knowledge: KnowledgeSchema) -> str:
        """Compress knowledge into a short context string for the Performer."""
        if not knowledge.findings:
            return ""
        lines = []
        for loc, finding in list(knowledge.findings.items())[:8]:
            lines.append(f"- {loc}: {str(finding)[:150]}")
        return "\n".join(lines)

    def _update_knowledge(
        self,
        knowledge: KnowledgeSchema,
        result: PerformerOutcome,
        oracle_cmd: str,
    ) -> None:
        """Extract key findings from the Performer's tool results."""
        for inv in result.invocations:
            if inv.tool_name in ("read", "rg", "bash", "goto_def") and inv.succeeded:
                key = inv.args.get("path") or inv.args.get("pattern") or inv.tool_name
                if inv.output_text and len(inv.output_text) > 20:
                    knowledge.findings[str(key)[:60]] = inv.output_text[:300]
            if inv.durable_change:
                for f in inv.files_modified:
                    knowledge.findings[f"modified:{f}"] = f"changed by: {inv.tool_name}"

    def _get_last_test_output(self, run: ActivatorRun) -> str:
        """Get the most recent bash/oracle tool output from messages."""
        for msg in reversed(run.messages):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 20:
                    return content[:400]
        return ""

    def _perform_once_with_messages(
        self, contract: Any, state: Any, run: ActivatorRun, messages: list[dict]
    ) -> bool:
        """Run one LLM+tool-dispatch cycle with explicit messages.
        Returns keep_going (False when LLM stops calling tools).
        """
        # Temporarily override run.messages for this call
        orig_messages = run.messages
        run.messages = messages
        try:
            keep_going = self._perform_once(contract, state, run)
            # Merge new messages back
            new_msgs = run.messages[len(messages):]
            orig_messages.extend(new_msgs)
        finally:
            run.messages = orig_messages
        return keep_going

    def _charge_llm_from_run(
        self, contract: Any, run: ActivatorRun, iter_start: int
    ) -> None:
        """No-op — costs already charged by _perform_once via _charge_llm."""
        pass
