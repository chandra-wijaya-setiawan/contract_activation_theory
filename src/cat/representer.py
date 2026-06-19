"""Representer — Step 1 of the CAT agentic loop.

Four modes, declared in skill.yaml under `representer.mode`:

  llm_call            — single LLM call: raw_request → ContractHeaderSchema
                        Updates contract.obligation and returns representations.

  probes_then_llm_call — runs probes first (workspace skeleton), then passes
                        their full output as context into the LLM call.
                        Used by: chat — gets repo tree / git log cheaply, then
                        the LLM classifies intent with full workspace awareness.

  bash_probes         — runs YAML-defined {key, command} probe list via bash.
                        Probe definitions live in skill.yaml representer.probes.
                        Used by: task, swebench

  adapter             — no-op; adapter already populated contract before run().
                        Used by: swebench with custom adapter
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from cat.capability.registry import CapabilityRegistry
from cat.contract.model import Contract, Representation, State
from cat.contract.schemas import ContractHeaderSchema
from cat.trace import Recorder, TraceEvent
from cat.utils.time import utcnow_iso


_ANSI_ESC = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _first_meaningful_line(text: str) -> str:
    """Return the first non-empty, non-prompt line of text."""
    for ln in text.splitlines():
        stripped = ln.strip()
        # Skip shell prompts (e.g. "(testbed) root@...# " or "$ ")
        if stripped and not stripped.endswith("# ") and not stripped.endswith("$ "):
            return stripped[:200]
    return "(empty)"


def _derive_judge_strategy(reps: list) -> Representation:
    """Decide how criteria should be verified, based on discovered probe outputs.

    Priority (highest to lowest):
        1. /tests/run-tests.sh (terminal-bench harness evaluation script) — most targeted
        2. run_tests.sh / run-tests.sh in the project root — project-level test runner
        3. pytest infrastructure found (pytest.ini, pyproject.toml, etc.) — fallback generic pytest
        4. Nothing found → llm_only

    Returns a Representation with key="judge_strategy":
        value starts with "oracle: <cmd>"  — Activator should inject an oracle criterion
        value == "llm_only"               — fall back to LLM judge

    IMPORTANT: oracle commands must NOT use pipes (e.g. | tail -N) because piping
    makes $? reflect the last command in the pipe (usually exit 0) instead of the
    test runner's exit code, producing false positives.
    """
    eval_cmd_rep  = next((r for r in reps if r.key == "eval_command"),  None)
    test_cmd_rep  = next((r for r in reps if r.key == "test_command"),  None)
    test_scripts_rep = next((r for r in reps if r.key == "test_scripts"), None)

    oracle_cmd = None
    reason = ""

    # Strip ANSI escape codes before processing — `clear` emits them even in
    # non-TTY subprocesses, causing false positives in startswith/contains checks.

    # Priority 1: harness evaluation script (/tests/run-tests.sh)
    if eval_cmd_rep:
        eval_clean = _ANSI_ESC.sub('', eval_cmd_rep.evidence)
        if "(no /tests/run-tests.sh)" not in eval_clean:
            for line in eval_clean.splitlines():
                stripped = line.strip()
                if stripped and ("pytest" in stripped or "python" in stripped):
                    # Remove any pipes to preserve exit code integrity
                    oracle_cmd = stripped.split("|")[0].strip()[:200]
                    reason = "extracted from /tests/run-tests.sh (evaluation harness)"
                    break

    # Priority 2: project-level run_tests.sh
    if oracle_cmd is None and test_cmd_rep:
        cmd_clean = _ANSI_ESC.sub('', test_cmd_rep.evidence)
        if "(no run_tests.sh)" not in cmd_clean:
            for line in cmd_clean.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("set ") and not stripped.startswith("#!/"):
                    # Remove any pipes to preserve exit code integrity
                    oracle_cmd = stripped.split("|")[0].strip()[:200]
                    reason = "extracted from run_tests.sh"
                    break

    # Priority 3: pytest infrastructure present — generic fallback (no pipe, no -x)
    if oracle_cmd is None and test_scripts_rep:
        scripts_clean = _ANSI_ESC.sub('', test_scripts_rep.evidence).strip()
        if scripts_clean:
            oracle_cmd = "python -m pytest --tb=short -q"
            reason = f"pytest infrastructure found: {scripts_clean[:100]}"

    if oracle_cmd:
        return Representation(
            key="judge_strategy",
            value=f"oracle: {oracle_cmd[:80]}",
            evidence=(
                f"Strategy: oracle-first — run test command; LLM judge is fallback only.\n"
                f"Oracle command: {oracle_cmd}\n"
                f"Reason: {reason}\n\n"
                "Run this command to verify your fix. "
                "The contract closes when it exits 0."
            ),
        )
    return Representation(
        key="judge_strategy",
        value="llm_only",
        evidence=(
            "No test runner found (/tests/run-tests.sh, run_tests.sh, or pytest infrastructure).\n"
            "Verification: LLM judge will assess whether the task is complete."
        ),
    )


# Static tool-strategy hint — flows into the planner's ENVIRONMENT section
# so it writes steps that use the right tools for the task type.
_TOOL_STRATEGY = Representation(
    key="tool_strategy",
    value="bash(search/run) + str_replace(edit existing) + write(new files only)",
    evidence=(
        "Available tools and when to use them:\n"
        "  bash(command)                    — grep/find to locate code, run tests/scripts.\n"
        "  str_replace(path, old_str, new_str) — patch an existing file surgically.\n"
        "  write(path, content)             — create brand-new files only.\n"
        "\n"
        "For code fixes: bash to locate → str_replace to patch → bash to verify.\n"
        "For new scripts/configs: write.\n"
        "For installs/tests: bash."
    ),
)

_LLM_CALL_SYSTEM = """\
You are formalizing a user's raw request into a structured contract header.

Given the raw_request, produce:
  obligation      — a clear, actionable goal statement (NOT just an echo of the input)
  representations — key-value context items that guide planning and execution

{context}

Output ONLY valid JSON matching this schema:
{{"obligation": "...", "representations": [{{"key": "...", "value": "..."}}, ...]}}
"""


class Representer:
    def __init__(
        self,
        registry: CapabilityRegistry,
        recorder: Recorder,
        llm: Optional[Any] = None,
        skill: Optional[Any] = None,
    ) -> None:
        self.registry = registry
        self.recorder = recorder
        self._llm = llm
        self._skill = skill

    def represent(
        self,
        contract: Contract,
        state: State,
        static_reps: Optional[list] = None,
        parent_event_id: Optional[str] = None,
    ) -> list:
        mode = "bash_probes"
        if self._skill is not None:
            mode = getattr(self._skill, "representer_mode", "bash_probes")

        if mode == "adapter":
            return list(static_reps or [])

        if mode == "llm_call":
            return self._llm_call(contract, state, static_reps or [], parent_event_id)

        if mode == "probes_then_llm_call":
            # Run probes first, then pass their outputs as context into the LLM call.
            probe_reps = self._run_probes(contract, state, parent_event_id)
            return self._llm_call(contract, state, (static_reps or []) + probe_reps, parent_event_id)

        # default: bash_probes
        return self._bash_probes(contract, state, static_reps or [], parent_event_id)

    def _run_probes(self, contract, state, parent_event_id) -> list:
        """Run skill.yaml representer.probes and return Representation list (no judge/tool reps)."""
        probes = []
        if self._skill is not None:
            probes = list(getattr(self._skill, "representer_probes", []))

        reps: list[Representation] = []
        prev_id = parent_event_id or "rep-start"
        for probe in probes:
            key = probe.get("key", "")
            cmd = probe.get("command", "")
            if not key or not cmd:
                continue
            tc_id = f"rep-{key}"
            result = self.registry.invoke(
                name="bash",
                args={"command": cmd, "timeout_s": 15},
                state=state,
                recorder=self.recorder,
                contract_id=contract.id,
                tool_call_id=tc_id,
                parent_event_id=prev_id,
            )
            contract.cost.tool_calls += 1
            contract.cost.wall_s += float(result.wall_s)
            evidence = _ANSI_ESC.sub("", result.output.strip())
            reps.append(Representation(key=key, value=_first_meaningful_line(evidence), evidence=evidence))
            prev_id = tc_id
        return reps

    def _bash_probes(self, contract, state, static_reps, parent_event_id):
        reps = self._run_probes(contract, state, parent_event_id)

        if reps:
            reps.append(_derive_judge_strategy(reps))
            reps.append(_TOOL_STRATEGY)

        self.recorder.append(TraceEvent(
            event_id=f"rep-{contract.id[:8]}",
            parent_id=parent_event_id or "rep-start",
            ts=utcnow_iso(),
            kind="contract_represent",
            payload={
                "contract_id": contract.id,
                "mode": "bash_probes",
                "representations": [{"key": r.key, "value": r.value} for r in reps],
            },
        ))
        return static_reps + reps

    def _llm_call(self, contract, state, static_reps, parent_event_id):
        if self._llm is None:
            return static_reps

        context_parts = []
        for r in static_reps:
            key = getattr(r, "key", "")
            # Prefer evidence (full bash output) over value (truncated first line)
            # so workspace probe outputs flow into the LLM in full.
            body = getattr(r, "evidence", "") or getattr(r, "value", "")
            if key and body:
                context_parts.append(f"{key}:\n{body.strip()[:800]}")
        context = "\n\n".join(context_parts)

        system = _LLM_CALL_SYSTEM.format(context=context)
        raw_req = contract.raw_request or contract.obligation
        # Seed with recent conversation history so the Representer can resolve
        # ambiguous follow-ups ("it", "that", "same thing", etc.) from prior turns.
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in (contract.chat_history or [])[-6:]:   # last 3 exchanges
            messages.append(h)
        messages.append({"role": "user", "content": f"raw_request: {raw_req}"})

        try:
            result = self._llm.chat(
                messages=messages,
                response_format={"type": "json_object"},
                role_tag="representer",
                temperature=0.0,
                parent_event_id=parent_event_id,
            )
            data = json.loads(result.text or "{}")
            header = ContractHeaderSchema(**data)
        except Exception:
            header = ContractHeaderSchema()

        if header.obligation:
            contract.obligation = header.obligation

        header_reps = [
            Representation(key=r.get("key", ""), value=r.get("value", ""))
            for r in (header.representations or [])
            if r.get("key")
        ]

        self.recorder.append(TraceEvent(
            event_id=f"rep-{contract.id[:8]}",
            parent_id=parent_event_id or "rep-start",
            ts=utcnow_iso(),
            kind="contract_represent",
            payload={
                "contract_id": contract.id,
                "mode": "llm_call",
                "obligation": header.obligation,
                "representations": [{"key": r.key, "value": r.value} for r in header_reps],
            },
        ))
        return static_reps + header_reps

    def represent_event_id(self, contract: Contract) -> str:
        """Stable event_id for the contract_represent summary event."""
        return f"rep-{contract.id[:8]}"
