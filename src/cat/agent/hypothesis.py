"""HypothesisLoopActivator — the v3 hypothesis search loop.

Standalone activator that inherits ONLY from BaseActivator (not from v2 Activator).
Implements the hypothesis-based discharge loop:

  Outer: Stipulator -> hypothesis -> Performer -> oracle -> (repeat)
  Inner: Performer implements hypothesis in K attempts, self-verifies

See design.v3.md §"Outer Loop" and §"Inner Loop".
See plan.v3.md Phase 3-4.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from cat.agent.base import Agent as BaseActivator, ActivatorRun
from cat.agent.checkpoint import NullCheckpointer
from cat.llm import (
    LLMKernel,
    KernelResult,
    build_stipulator_messages,
    build_performer_plan_messages,
)
from cat.llm.performer_plan import PERFORMER_V3_SYSTEM_PROMPT
from cat.contract.model import AdmissionDecision, ContractState
from cat.contract.schemas import (
    HypothesisSchema,
    PerformerTaskSchema,
    PerformerPlanSchema,
    KnowledgeSchema,
    InvocationSchema as ToolInvocation,
    OutcomeSchema as PerformerOutcome,
    PerformerExecutionStep,
)
from cat.contract.representer import Representer
from cat.trace import TraceEvent
from cat.utils.time import utcnow_iso

MAX_HYPOTHESES = 6
MAX_INNER_ATTEMPTS = 5
MAX_TOOLS_PER_HYPOTHESIS = 40

_DEFAULT_MODEL_ARRAY = [
    "openrouter/deepseek/deepseek-v4-flash",
    "openrouter/deepseek/deepseek-v4-pro",
    "openrouter/owl-alpha",
]

# Stipulator/planner runs at top intelligence (clamped to 1.0 = strongest model).
PLANNER_INTELLIGENCE = float(os.environ.get("CAT_PLANNER_INTELLIGENCE", "10"))
# Performer starts at 0 and climbs by this much per hypothesis (H1=0, H2=0.2, ...).
PERFORMER_INTELLIGENCE_STEP = float(os.environ.get("CAT_PERFORMER_STEP", "0.2"))


def _model_array() -> list[str]:
    env = os.environ.get("CAT_MODEL_ARRAY", "").strip()
    return [m.strip() for m in env.split(",") if m.strip()] if env else list(_DEFAULT_MODEL_ARRAY)


class HypothesisAgent(BaseActivator):
    """v3 hypothesis-based activator. Inherits BaseActivator only.

    Replaces the plan-tree + Resolver + PlanPatcher architecture with a
    clean hypothesis search loop:

      Outer: Stipulator -> hypothesis -> Performer -> oracle -> (repeat)
      Inner: Performer implements hypothesis in K attempts, self-verifies
    """

    def __init__(self, *args, checkpointer=None, skill=None, performer_kernel=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.checkpointer = checkpointer if checkpointer is not None else NullCheckpointer()
        self._skill = skill
        self._llm_cache: dict = {}
        self._performer_kernel_override = performer_kernel  # injected in tests

    def _llm_for_model(self, model: str) -> Any:
        """Return an LLM client for a resolved model string (cached).
        Returns self.llm when the model matches the base model."""
        if model == self.llm.model:
            return self.llm
        if model not in self._llm_cache:
            try:
                from cat.llm.client import LLM as _LLM
                self._llm_cache[model] = _LLM(model=model, recorder=self.recorder)
            except Exception:
                self._llm_cache[model] = self.llm
        return self._llm_cache[model]

    def _charge_kernel(self, contract: Any, kernel_result: KernelResult) -> None:
        """Charge a KernelResult's aggregate usage to the contract budget."""
        try:
            from cat.llm.client import LLMResult as _LLMResult
            synthetic = _LLMResult(
                text="", tokens_in=getattr(kernel_result, "tokens_in", 0),
                tokens_out=getattr(kernel_result, "tokens_out", 0),
                usd=getattr(kernel_result, "usd", 0.0),
                model=getattr(kernel_result, "model", ""),
            )
            self._charge_llm(contract, synthetic)
        except Exception:  # noqa: BLE001 — never fail the task on accounting
            pass

    def _cp_snapshot(self, label: str) -> bool:
        """Defensive snapshot — a checkpoint failure must never crash the task."""
        try:
            return bool(self.checkpointer.snapshot(label))
        except Exception as exc:  # noqa: BLE001
            self._cp_log_error("snapshot", label, exc)
            return False

    def _cp_restore(self, label: str) -> bool:
        try:
            return bool(self.checkpointer.restore(label))
        except Exception as exc:  # noqa: BLE001
            self._cp_log_error("restore", label, exc)
            return False

    def _cp_labels(self) -> list:
        try:
            return list(self.checkpointer.labels())
        except Exception:  # noqa: BLE001
            return []

    def _cp_log_error(self, op: str, label: str, exc: Exception) -> None:
        try:
            import traceback
            self.recorder.append(TraceEvent(
                event_id=f"cp-err-{uuid.uuid4().hex[:6]}",
                parent_id=None,
                ts=utcnow_iso(),
                kind="checkpoint_error",
                payload={"op": op, "label": label,
                         "error": f"{type(exc).__name__}: {exc}",
                         "trace": traceback.format_exc()[-800:]},
            ))
        except Exception:  # noqa: BLE001
            pass

    # ── Admission override ──────────────────────────────────────────────

    def _admit(self, contract: Any, state: Any, run: ActivatorRun) -> AdmissionDecision:
        """v3 override: skip the 'empty plan' check — v3 uses hypotheses not a plan."""
        import time

        if not (contract.obligation or "").strip():
            return AdmissionDecision.reject("boundary", "empty obligation")
        cfg = self._loop_config()
        if not cfg.skip_oracle and not contract.criteria:
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

    def _loop_config(self):
        """Return the LoopConfig from the injected skill, or defaults."""
        from cat.agent.definition import LoopConfig
        if self._skill is not None:
            return self._skill.loop_config
        return LoopConfig()  # all defaults: full loop

    def _skill_representations(self):
        """Return Representation objects from the skill's YAML represent list."""
        from cat.contract.model import Representation
        if self._skill is None:
            return []
        return [
            Representation(key=r.get("key", ""), value=r.get("value", ""))
            for r in (self._skill.represent or [])
        ]

    def run(self, contract: Any, state: Any) -> ActivatorRun:
        """v3 discharge loop: stipulate hypothesis -> perform -> oracle -> repeat.

        Steps are gated by LoopConfig from the injected AgentDef (profile):
          skip_representer → use skill's represent list, no bash probes
          skip_stipulator  → pass obligation straight to Performer (chat mode)
          skip_oracle      → always break after 1 hypothesis (user is the oracle)
        """
        run = ActivatorRun(contract=contract, state=state)
        cfg = self._loop_config()

        # ── Step 1: Represent ────────────────────────────────────────────────
        pre_reps = list(contract.representations or [])
        representer = Representer(
            registry=self.registry,
            recorder=self.recorder,
            llm=self.llm,
            skill=self._skill,
        )
        static_reps = self._skill_representations()
        contract.representations = pre_reps + representer.represent(
            contract, state,
            static_reps=static_reps,
            parent_event_id=run.last_state_event_id,
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

        # Extract oracle command from representations (empty in skip_oracle mode)
        oracle_cmd = "" if cfg.skip_oracle else self._extract_oracle(contract)

        knowledge = KnowledgeSchema()
        prior_attempts: list[dict] = []

        # Snapshot the original clean state as 'base'. Each hypothesis can later
        # branch from 'base' or from any prior hypothesis's checkpoint.
        cp_base_ok = self._cp_snapshot("base")
        last_checkpoint = "base"  # most recent successful snapshot label

        # ── Kernels — intelligence-addressed brains ──────────────────────────
        # Skill owns the model array; env var CAT_MODEL_ARRAY is the fallback.
        models = cfg.model_array if cfg.model_array else _model_array()
        # skill.intelligence scales the planner; 0.0=fastest, 1.0=strongest.
        planner_intelligence = PLANNER_INTELLIGENCE * cfg.intelligence if cfg.intelligence < 1.0 else PLANNER_INTELLIGENCE
        planner_kernel = LLMKernel(
            models=models, mode="schema", schema=HypothesisSchema,
            temperature=0.1, intelligence=planner_intelligence,
            role_tag="stipulator", recorder=self.recorder,
        )
        performer_kernel = self._performer_kernel_override or LLMKernel(
            models=models, mode="toolcall", schema=PerformerExecutionStep,
            temperature=0.2, intelligence=0.0,
            role_tag="performer", recorder=self.recorder,
        )

        max_hypotheses = cfg.max_hypotheses
        for hypothesis_n in range(max_hypotheses):
            if not self._check_budget(contract, state, run):
                break

            # Performer band: H1=0.0, H2=0.2, ... (+step per hypothesis), clamped 1.0.
            performer_kernel.set_intelligence(hypothesis_n * PERFORMER_INTELLIGENCE_STEP)
            exec_model = performer_kernel.current_model()

            # ── Step 2: Stipulate ────────────────────────────────────────────
            if cfg.skip_stipulator:
                # Chat / direct mode: no planning LLM call.
                # Pass the obligation straight to the Performer as a synthetic hypothesis.
                hypothesis = HypothesisSchema(
                    location="*",
                    change=contract.obligation,
                    predicts="task completed or question answered",
                    basis="direct user request — no planning step",
                    start_from="base",
                )
            else:
                stip_messages = build_stipulator_messages(
                    obligation=contract.obligation,
                    representations=contract.representations,
                    knowledge=knowledge,
                    prior_attempts=prior_attempts,
                )
                stip_res = planner_kernel.generate_schema(
                    stip_messages, parent_event_id=run.last_state_event_id)
                hypothesis = stip_res.value if stip_res.ok else None
                self._charge_kernel(contract, stip_res)

                if hypothesis is None:
                    self.recorder.append(TraceEvent(
                        event_id=f"h-fail-{uuid.uuid4().hex[:6]}",
                        parent_id=run.last_state_event_id,
                        ts=utcnow_iso(),
                        kind="hypothesis_attempt",
                        payload={"n": hypothesis_n + 1, "error": "stipulator returned no hypothesis"},
                    ))
                    break

            # Branch selection: restore the working state to the checkpoint the
            # Stipulator chose. Validate the requested label exists; fall back to
            # the most recent checkpoint if the model named an unknown one.
            requested_cp = getattr(hypothesis, "start_from", "base") or "base"
            available = set(self._cp_labels())
            if cp_base_ok and available:
                target_cp = requested_cp if requested_cp in available else last_checkpoint
                restored = self._cp_restore(target_cp)
            else:
                target_cp, restored = "base", False

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
                    "planning_model": planner_kernel.current_model().split("/")[-1],
                    "execution_model": exec_model.split("/")[-1],
                    "performer_intelligence": round(performer_kernel.intelligence, 3),
                    "start_from_requested": requested_cp,
                    "start_from_used": target_cp if restored else "(none)",
                },
            ))

            # Performer executes on the band-resolved model (exec_model). The
            # continuation plan (H2+) uses the planner kernel.
            task = PerformerTaskSchema(
                obligation=contract.obligation,
                hypothesis=hypothesis,
                oracle=oracle_cmd,
                context=self._build_context(knowledge),
                max_attempts=MAX_INNER_ATTEMPTS,
                chat_history=list(contract.chat_history),
            )
            exec_llm = self._llm_for_model(exec_model)
            result = self._perform_hypothesis(
                contract, state, run, task,
                llm_override=exec_llm if exec_llm is not self.llm else None,
                performer_kernel=performer_kernel,
                prior_results=self._build_prior_results(knowledge, prior_attempts),
                planning_llm=planner_kernel,
                require_explicit_done=cfg.require_explicit_done,
                extra_context=self._reps_to_context(contract),
            )

            # Snapshot the Performer's edits BEFORE the oracle runs — the oracle
            # mutates the working tree (applies test_patch), so capturing now
            # keeps this checkpoint as clean source-only edits the next
            # hypothesis can branch from.
            cp_label = f"h{hypothesis_n + 1}"
            if cp_base_ok and result.any_durable_change:
                if self._cp_snapshot(cp_label):
                    last_checkpoint = cp_label

            # Knowledge update regardless of outcome
            self._update_knowledge(knowledge, result, oracle_cmd)
            prior_attempts.append({
                "hypothesis": hypothesis,
                "verdict": result.verdict,
                "reason": result.performer_reason,
                "intelligence": round(performer_kernel.intelligence, 3),
                "checkpoint": cp_label if (cp_base_ok and result.any_durable_change) else last_checkpoint,
            })

            self.recorder.append(TraceEvent(
                event_id=f"h-result-{uuid.uuid4().hex[:6]}",
                parent_id=run.last_state_event_id,
                ts=utcnow_iso(),
                kind="knowledge_update",
                payload={
                    "n": hypothesis_n + 1,
                    "verdict": result.verdict,
                    "reason": result.performer_reason[:2000],
                    "findings_count": len(knowledge.findings),
                },
            ))

            # ── Step 4: Verify ───────────────────────────────────────────────
            # Expose performer response so LLM-judge criteria can read it.
            state.last_response = result.performer_reason or ""

            if cfg.skip_oracle:
                # Chat / no-oracle mode: Performer's done() is the verdict.
                # Only fulfil on completed; give_up continues to next hypothesis
                # so the outer loop can escalate to a higher model tier.
                if result.verdict == "completed":
                    self._transition(contract, ContractState.FULFILLED,
                                     "skill skip_oracle: performer completed", run)
                    self._finalize(contract, run)
                    return run
                # give_up / exhausted → fall through to next hypothesis

            # Skip oracle on give_up — the Performer signalled a bad approach,
            # not a response to evaluate. Escalate directly.
            if result.verdict == "give_up":
                knowledge.oracle_failures.append(
                    f"give_up: {(result.performer_reason or '')[:500]}"
                )
                continue

            if self._verify_criteria(contract, state, run):
                if cp_base_ok and result.any_durable_change:
                    self._cp_restore(cp_label)
                self._transition(contract, ContractState.FULFILLED,
                                 f"hypothesis {hypothesis_n + 1} satisfied oracle", run)
                self._finalize(contract, run)
                return run

            # Record oracle failure for next Stipulator call
            oracle_failure = ""
            for crit in contract.criteria:
                if crit.satisfied:
                    continue
                ev = getattr(crit, "last_evidence", None)
                if ev is not None:
                    oracle_failure = (ev.detail or {}).get("output", "") or ev.summary
                    if oracle_failure:
                        break
            if oracle_failure:
                knowledge.oracle_failures.append(oracle_failure[:2000])

        # All hypotheses exhausted
        if contract.state == ContractState.ACTIVE:
            self._transition(contract, ContractState.FAILED,
                             f"all {max_hypotheses} hypotheses exhausted", run)
            run.failure_reason = "hypotheses_exhausted"

        self._finalize(contract, run)
        return run

    # ── Inner loop ───────────────────────────────────────────────────────

    def _format_plan(self, plan: Optional[PerformerPlanSchema]) -> str:
        """Format plan for injection into the Performer's user message."""
        if plan is None:
            return ""
        lines = [f"PLAN (current_state: {plan.current_state}, confidence: {plan.confidence}):"]
        for i, s in enumerate(plan.steps, 1):
            marker = "→" if s.status == "pending" else ("✓" if s.status == "done" else "✗")
            lines.append(f"  {marker} Step {i}: {s.action}  [{s.rationale[:80]}]")
        next_s = plan.next_step
        if next_s:
            lines.append(f"\nEXECUTE NOW: {next_s.action}")
        return "\n".join(lines)

    def _reps_to_context(self, contract: Any) -> str:
        """Format contract representations as context for the Performer."""
        lines = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for r in (contract.representations or []):
            key = getattr(r, "key", "") or ""
            val = getattr(r, "value", "") or getattr(r, "evidence", "") or ""
            if not isinstance(val, str):
                val = str(val)
            val = val.strip()
            val = val.replace("{{CURRENT_DATETIME}}", now)
            if key and val:
                lines.append(f"{key}:\n{val}")
        return "\n\n".join(lines)

    def _perform_hypothesis(
        self,
        contract: Any,
        state: Any,
        run: ActivatorRun,
        task: PerformerTaskSchema,
        llm_override: Optional[Any] = None,
        performer_kernel: Optional[object] = None,
        prior_results: str = "",
        planning_llm: Optional[Any] = None,
        require_explicit_done: bool = False,
        extra_context: str = "",
    ) -> PerformerOutcome:
        """Inner react loop: Performer implements one hypothesis.

        llm_override: when set, uses this LLM for Performer calls instead of
        self.llm. Allows full engine escalation (same model for Stipulator +
        Performer + Attestor within one hypothesis cycle).
        """
        # Use escalated LLM if provided, fall back to self.llm
        active_llm = llm_override if llm_override is not None else self.llm

        # Only generate a continuation plan when there are prior attempts.
        # H1 (no prior_results) works best without a plan — the performer reasons
        # freely. H2+ benefits from a plan that anchors current_state to what was
        # already tried so the model continues rather than restarts.
        plan: Optional[PerformerPlanSchema] = None
        if prior_results:
            # planning_llm is an LLMKernel (preferred) or a plain LLM (legacy).
            plan_messages = build_performer_plan_messages(task, prior_results=prior_results)
            parent = run.last_perform_event_id or run.last_state_event_id
            if planning_llm is not None:
                pres = planning_llm.generate_schema(
                    plan_messages, schema=PerformerPlanSchema,
                    parent_event_id=parent)
                plan = pres.value if pres.ok else None
                self._charge_kernel(contract, pres)
            else:
                plan = None
            if plan and self.recorder:
                self.recorder.append(TraceEvent(
                    event_id=f"pp-{uuid.uuid4().hex[:6]}",
                    parent_id=run.last_state_event_id,
                    ts=utcnow_iso(),
                    kind="performer_plan",
                    payload={
                        "current_state": plan.current_state,
                        "confidence": plan.confidence,
                        "steps": [s.model_dump() for s in plan.steps],
                    },
                ))

        # ── Build initial user message with hypothesis + plan ─────────────────
        if require_explicit_done:
            # Chat / no-oracle mode: strip the hypothesis scaffolding — just deliver
            # the task directly. Inject skill representations (assistant_mode, workdir)
            # since the Stipulator was skipped and reps never reached the model otherwise.
            hypothesis_prompt = f"TASK:\n{task.obligation}\n"
            if extra_context:
                hypothesis_prompt += f"\n{extra_context}\n"
        else:
            hypothesis_prompt = (
                f"OBLIGATION:\n{task.obligation}\n\n"
                f"HYPOTHESIS:\n"
                f"  Location: {task.hypothesis.location}\n"
                f"  Change:   {task.hypothesis.change}\n"
                f"  Predicts: {task.hypothesis.predicts}\n"
                f"  Basis:    {task.hypothesis.basis}\n"
            )
            if task.oracle:
                hypothesis_prompt += f"\nORACLE (run when ready to verify):\n{task.oracle}\n"
        if task.context:
            hypothesis_prompt += f"\nDOMAIN CONTEXT:\n{task.context}\n"
        if plan:
            hypothesis_prompt += f"\n{self._format_plan(plan)}\n"
        elif not prior_results and not require_explicit_done:
            # H1: no plan, but enforce read-before-write with a single reminder.
            hypothesis_prompt += "\nNOTE: Inspect the target location before applying any change.\n"

        # V3 inner loop: prepend chat history (if any) so prior turns are visible
        inner_messages: list[dict] = list(task.chat_history or []) + [
            {"role": "user", "content": hypothesis_prompt}
        ]

        all_invocations: list[ToolInvocation] = []
        verdict = "exhausted"
        performer_reason = ""
        hypothesis_tool_count = 0  # local budget per hypothesis

        all_tools = self.registry.tool_schemas(only=run.grant) if self.registry else []
        # Tools restricted to mutation-only after force-commit fires
        _MUTATION_TOOLS = {"str_replace", "write", "multi_edit", "apply_patch",
                           "done", "give_up"}
        mutation_tools = [t for t in all_tools
                          if t["function"]["name"] in _MUTATION_TOOLS]
        force_committed = False  # track if force-commit has fired

        for attempt in range(1, task.max_attempts + 1):
            if hypothesis_tool_count >= MAX_TOOLS_PER_HYPOTHESIS:
                verdict = "exhausted"
                performer_reason = f"per-hypothesis tool budget ({MAX_TOOLS_PER_HYPOTHESIS}) reached"
                break
            if not self._check_budget(contract, state, run):
                verdict = "exhausted"
                performer_reason = "budget exhausted"
                break

            # Use mutation-only tools only after 3+ attempts with no change.
            # At 2 attempts we use text-only nudge (allows give_up if truly stuck).
            # At 3+ attempts we enforce mechanistically.
            active_tools = mutation_tools if (force_committed and attempt >= 3) else all_tools

            # Direct LLM call with isolated message thread
            messages = [
                {"role": "system", "content": PERFORMER_V3_SYSTEM_PROMPT},
            ] + inner_messages

            # Performer LLM call via kernel (toolcall mode with self-heal).
            # The kernel handles: reask on validation error, retry on truncation,
            # and model escalation on repeated failures.
            performer_res = performer_kernel.generate_toolcall(
                messages,
                schema=PerformerExecutionStep,
                tools=active_tools,
                parent_event_id=run.last_perform_event_id or run.last_state_event_id,
            )
            self._charge_kernel(contract, performer_res)
            run.last_perform_event_id = f"perf-v3-{uuid.uuid4().hex[:6]}"

            step: Optional[PerformerExecutionStep] = performer_res.value if performer_res.ok else None

            if step is None:
                # Kernel exhausted all retries/escalations — give up this hypothesis
                verdict = "give_up"
                performer_reason = (
                    f"Performer kernel failed after {performer_res.attempts} attempts: "
                    f"{performer_res.failure[:200]}"
                )
                break

            # Append assistant message with thinking + tool calls
            asst_msg: dict = {"role": "assistant", "content": step.thinking or ""}
            if step.tools:
                asst_msg["tool_calls"] = [
                    {
                        "id": f"tc-{uuid.uuid4().hex[:6]}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in step.tools
                ]
            inner_messages.append(asst_msg)

            # Check terminal intents (done/give_up) — no tools to dispatch
            if step.intent == "done":
                verdict = "completed"
                performer_reason = step.reason or "Performer signalled done"
                break
            if step.intent == "give_up":
                verdict = "give_up"
                performer_reason = step.reason or "Performer gave up"
                break

            # Dispatch tool calls
            iter_invocations: list[ToolInvocation] = []
            keep_going = bool(step.tools)

            for tc in step.tools:
                hypothesis_tool_count += 1
                tc_id = f"tc-{uuid.uuid4().hex[:6]}"
                tool_name = tc.name
                args = tc.arguments

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
                exec_status = "error" if is_err else "success"
                durable = not is_err and tool_name in ("str_replace", "multi_edit", "apply_patch", "write")
                files: list[str] = []
                if durable:
                    path = args.get("path") or ""
                    if path:
                        files.append(path)

                inv = ToolInvocation(
                    tool_name=tool_name,
                    args=args,
                    reason="",
                    attempt=attempt,
                    execution_status=exec_status,
                    output_text=content[:3000],
                    error=content[7:300] if is_err else "",
                    durable_change=durable,
                    files_modified=files,
                )
                iter_invocations.append(inv)
                all_invocations.append(inv)

            # Build outcome for this attempt
            inner_outcome = PerformerOutcome(
                text_response=step.thinking or "",
                invocations=iter_invocations,
                finish_reason="tool_calls" if step.tools else "stop",
                any_durable_change=any(inv.durable_change for inv in iter_invocations),
                any_tool_errored=any(not inv.succeeded for inv in iter_invocations),
            )

            # Check for exit signals:
            # - done/give_up intents are handled above (before tool dispatch)
            # - implicit done: model stopped (no tools) after making a durable change
            if not keep_going:
                any_durable_so_far = any(inv.durable_change for inv in all_invocations)
                if any_durable_so_far:
                    verdict = "completed"
                    performer_reason = "Performer stopped after making a change"
                    break
                # No change made — fall through to force-commit check below

            # Auto-give_up: if a modification tool errored (target not found,
            # target doesn't exist as predicted), this hypothesis is wrong.
            # Domain-agnostic: applies to str_replace/write/refund_api/etc.
            # No point retrying — escalate to a new hypothesis.
            last_inv = all_invocations[-1] if all_invocations else None
            if (last_inv and not last_inv.succeeded
                    and last_inv.tool_name in ("str_replace", "write", "multi_edit",
                                               "apply_patch")):
                verdict = "give_up"
                performer_reason = (
                    f"Modification tool '{last_inv.tool_name}' failed: "
                    f"{last_inv.error[:200] or last_inv.output_text[:200]}. "
                    f"Hypothesis target does not match reality."
                )
                break

            # Force-commit: if 2+ attempts with no durable change, inject nudge
            # AND restrict tools to mutation-only for all subsequent attempts.
            any_durable_so_far = any(inv.durable_change for inv in all_invocations)
            if attempt >= 2 and not any_durable_so_far and not force_committed:
                force_committed = True  # switch to mutation-only tools next turn
                inner_messages.append({
                    "role": "user",
                    "content": (
                        "FRAMEWORK: You have gathered information for 2+ attempts "
                        "without applying any change. "
                        "You now have enough context. APPLY THE CHANGE the hypothesis describes. "
                        "Only modification tools are now available — you cannot read more files. "
                        "If the target genuinely does not exist or cannot be changed, "
                        "call give_up(reason=...) with a specific explanation."
                    ),
                })

            # Auto-detect: successful durable change -> treat as "completed"
            # This handles models that don't reliably call done() explicitly.
            # Suppressed when require_explicit_done=True (skill config) — the
            # model must call done() explicitly rather than auto-fulfilling via a file write.
            if (not require_explicit_done
                    and inner_outcome.any_durable_change
                    and not inner_outcome.any_tool_errored):
                verdict = "completed"
                performer_reason = (
                    f"Durable change made via "
                    f"{[i.tool_name for i in inner_outcome.invocations if i.durable_change]}"
                )
                if self._verify_criteria(contract, state, run):
                    performer_reason += " — oracle passed"
                break

            # Build a live PerformerPlanSchema from accumulated reasons (for trace)
            # No extra LLM call — each tool's `reason` IS the Thought step
            if all_invocations:
                steps = []
                for inv in all_invocations:
                    if inv.reason or inv.tool_name not in ("done", "give_up"):
                        from cat.contract.schemas import PerformerStep
                        steps.append(PerformerStep(
                            action=f"{inv.tool_name}: {str(list(inv.args.values())[:1])[:50]}",
                            rationale=inv.reason[:100] if inv.reason else "proceed to next step",
                            status="done" if inv.succeeded else "failed",
                        ))
                if steps:
                    plan = PerformerPlanSchema(
                        current_state=f"attempt {attempt}, last={all_invocations[-1].tool_name}",
                        steps=steps,
                        confidence="high" if not inner_outcome.any_tool_errored else "medium",
                    )

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
                summary = active_llm.chat(summary_msgs, role_tag="performer")
                self._charge_llm(contract, summary)
                performer_reason = summary.text or "Exhausted max attempts"
            except Exception:
                performer_reason = "Exhausted max attempts"

        combined = PerformerOutcome(
            text_response="",
            invocations=all_invocations,
            finish_reason="inner_loop",
            any_durable_change=any(inv.durable_change for inv in all_invocations),
            any_tool_errored=any(not inv.succeeded for inv in all_invocations),
        )
        combined.verdict = verdict
        combined.performer_reason = performer_reason
        return combined

    # ── Helpers ──────────────────────────────────────────────────────────

    def _extract_oracle(self, contract: Any) -> str:
        """Extract the oracle test command from contract criteria or representations.

        Priority:
        1. criterion.command (most reliable — the actual test runner)
        2. representations with eval_command/oracle_command key
        """
        import re

        # 1. Extract from criteria description  "exit_code==0 for: <cmd>"
        for c in getattr(contract, "criteria", []):
            desc = getattr(c, "description", "") or ""
            m = re.search(r"exit_code==0 for:\s*(.+)", desc)
            if m:
                return m.group(1).strip()[:600]
            cmd = getattr(c, "command", "") or getattr(c, "cmd", "")
            if cmd and len(cmd.strip()) > 5:
                return cmd.strip()[:600]

        # 2. Representations
        for rep in (contract.representations or []):
            key = getattr(rep, "key", "")
            if key in ("eval_command", "oracle_command"):
                val = (getattr(rep, "value", "") or getattr(rep, "evidence", "") or "").strip()
                # skip terminal noise
                if val and len(val) > 10 and "TERM environment" not in val:
                    return val[:600]

        return "cd /testbed && python -m pytest --tb=short -q 2>&1 | tail -20"

    def _build_prior_results(self, knowledge: KnowledgeSchema, prior_attempts: list) -> str:
        """Build a prior_results string for the Performer planner.

        Summarises what has already been tried so the planner can continue
        from the last known state rather than restarting from scratch.
        """
        if not prior_attempts:
            return ""
        lines = []
        for i, attempt in enumerate(prior_attempts, 1):
            h = attempt.get("hypothesis")
            verdict = attempt.get("verdict", "?")
            reason = attempt.get("reason", "")[:150]
            loc = getattr(h, "location", "?") if h else "?"
            change = getattr(h, "change", "?")[:80] if h else "?"
            lines.append(f"Attempt {i}: targeted '{loc}' — {change}")
            lines.append(f"  Result: {verdict} — {reason}")
        for key, finding in knowledge.findings.items():
            if key.startswith("modified:"):
                lines.append(f"Already applied: {key.replace('modified:', '')} was changed")
        for f in knowledge.oracle_failures[-1:]:
            lines.append(f"Last oracle failure: {f[:200]}")
        return "\n".join(lines)

    def _build_context(self, knowledge: KnowledgeSchema) -> str:
        """Compress knowledge into context string for the Stipulator.

        Shows NOT_FOUND warnings first (highest priority), then file
        content (so Stipulator sees actual method names), then other findings.
        """
        if not knowledge.findings:
            return ""
        lines = []
        # 1. Negative findings — highest priority
        for loc, finding in knowledge.findings.items():
            if loc.startswith("NOT_FOUND:"):
                lines.append(f"WARNING: {finding}")

        # 2. File reads — show enough to reveal actual method names
        for loc, finding in knowledge.findings.items():
            if not loc.startswith("NOT_FOUND:") and ("client.py" in loc or "read" == loc):
                lines.append(f"FILE CONTENT of {loc}:\n{str(finding)[:500]}")
                break  # one file is enough

        # 3. Oracle failures — two-part extraction (language-agnostic):
        #
        # Part A: signal-based +/-2 context around known error indicators.
        #   Covers: pytest (E   ), Jest (dot), Go (--- FAIL), Rust (panicked at),
        #           C/make (error:), and generic Error/Exception/assert patterns.
        #
        # Part B: always append last 8 lines of output.
        #   Catches: test summary lines ("1 failed in 0.5s"), assertion counts,
        #   and any test runner format that doesn't match Part A signals.
        _ERROR_SIGNALS = (
            "E   ", "FAILED", "FAIL\t", "--- FAIL",   # pytest / go
            "Error:", "error:", "TypeError", "ValueError",
            "AssertionError", "Exception",              # general
            "panicked at",                              # rust
            "undefined reference", "undefined symbol",  # C/linker
        )
        TAIL_LINES = 15
        for f in knowledge.oracle_failures[-2:]:
            all_lines = f.splitlines()
            e_indices = [i for i, l in enumerate(all_lines)
                         if any(sig in l for sig in _ERROR_SIGNALS)]

            parts = []
            if e_indices:
                ctx = 2
                seen = set()
                snippet = []
                for idx in e_indices[:4]:
                    for i in range(max(0, idx - ctx), min(len(all_lines), idx + ctx + 1)):
                        if i not in seen:
                            seen.add(i)
                            snippet.append(all_lines[i])
                    snippet.append("--")
                parts.append("\n".join(snippet[:20]))

            # Always append tail (deduped from signal section)
            tail_start = max(0, len(all_lines) - TAIL_LINES)
            shown = set(e_indices) if e_indices else set()
            extra = [l for i, l in enumerate(all_lines[tail_start:], tail_start)
                     if i not in shown]
            if extra:
                parts.append("...\n" + "\n".join(extra))

            raw = ("ORACLE FAILURE:\n" + "\n\n".join(parts)) if parts else f"ORACLE FAILURE: {f[:400]}"
            # Hard cap at 3000 bytes — consistent with rg tool limit
            if len(raw.encode("utf-8")) > 3000:
                raw = raw.encode("utf-8")[:3000].decode("utf-8", errors="replace") + "\n... [truncated]"
            lines.append(raw)

        return "\n".join(lines[:8])

    def _update_knowledge(
        self,
        knowledge: KnowledgeSchema,
        result: PerformerOutcome,
        oracle_cmd: str,
    ) -> None:
        """Extract key findings from the Performer's tool results."""
        import re
        for inv in result.invocations:
            if inv.tool_name in ("read", "rg", "bash", "goto_def") and inv.succeeded:
                key = inv.args.get("path") or inv.args.get("pattern") or inv.tool_name
                out = inv.output_text
                if out and len(out) > 20:
                    knowledge.findings[str(key)[:60]] = out[:600]

                    # Extract actual method/function/class names from file reads
                    # so the Stipulator knows WHAT IS THERE, not just what isn't
                    if inv.tool_name == "read":
                        names = re.findall(r'\bdef (\w+)\b', out)
                        classes = re.findall(r'\bclass (\w+)\b', out)
                        if names or classes:
                            found_str = ", ".join(
                                [f"class {c}" for c in classes[:3]] +
                                [f"def {n}" for n in names[:5]]
                            )
                            file_key = str(key)[:40]
                            knowledge.findings[f"FOUND_IN:{file_key}"] = (
                                f"Actual names in file: {found_str}. "
                                f"Use THESE names in your hypothesis, not others."
                            )

                    # Explicit negative: rg 0 matches
                    if inv.tool_name == "rg" and "0 matches" in out:
                        pattern = inv.args.get("pattern", "?")
                        knowledge.findings[f"NOT_FOUND:{pattern}"] = (
                            f"'{pattern}' does NOT exist anywhere. "
                            f"Do NOT generate hypotheses using this name."
                        )

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
HypothesisLoopActivator = HypothesisAgent
Activator = HypothesisAgent  # backward-compat alias
