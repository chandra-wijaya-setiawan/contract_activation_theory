"""Representer — Step 2 of the CAT agentic loop.

Establishes verified facts (Representations) about the current world state
before any obligation is decomposed into a plan. Runs a fixed, bounded probe
set. No LLM calls. No exploration.

Design rationale (DESIGN.md §Representation):
    A contract formed without verified representations of the environment is
    formally incomplete — its obligations are contingent on facts the agent has
    never established. The Representer closes that gap unconditionally.

tmux pane-capture note:
    When running via a tmux registry (terminal-bench), sequential bash calls
    share a terminal whose scrollback is fixed in one viewport. Successive
    capture_pane() calls can return the same window content if the new output
    hasn't pushed past the viewport boundary. We work around this by running
    ALL probes in ONE bash call that writes output to temp files, then reading
    each file back with a separate call preceded by `clear` so the viewport is
    unambiguously showing only that file's content.
"""
from __future__ import annotations
import re

from cat.capabilities import CapabilityRegistry
from cat.contracts import Contract, Representation, State
from cat.trace import Recorder, TraceEvent
from cat.utils.time import utcnow_iso


_ANSI_ESC = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


# Phase 1: run all probes in one bash call, write each result to a temp file.
# One tool call total. The only terminal output is the final "PROBES_DONE"
# sentinel, which is short and reliably captured.
_PROBE_WRITE_CMD = (
    "(pwd && echo '---' && ls -1) > /tmp/cat_r_workdir.txt 2>&1; "
    # Detailed file listing: sizes, permissions, hidden files — reveals archives, executables, etc.
    "(ls -la 2>/dev/null | head -35) > /tmp/cat_r_ls_la.txt 2>&1; "
    # File-type probe on non-text files — detects 7z/zip archives, ELF binaries, etc.
    # Runs `file` only on regular files to avoid errors on directories.
    "(find . -maxdepth 1 -type f | head -20 | xargs file 2>/dev/null | head -20) > /tmp/cat_r_filetypes.txt 2>&1; "
    # README / notes files often contain task hints, passwords, or environment setup instructions.
    "(cat README* readme* NOTES* notes* HINTS* hints* 2>/dev/null | head -60) > /tmp/cat_r_notes.txt 2>&1; "
    "find . -maxdepth 3 \\( "
    "-name 'run_tests.sh' -o -name 'run-tests.sh' "
    "-o -name 'pytest.ini' -o -name 'setup.py' "
    "-o -name 'pyproject.toml' -o -name 'tox.ini' "
    "\\) > /tmp/cat_r_scripts.txt 2>/dev/null; "
    "({ cat run_tests.sh 2>/dev/null || cat run-tests.sh 2>/dev/null "
    "|| echo '(no run_tests.sh)'; } | head -20) > /tmp/cat_r_cmd.txt 2>&1; "
    "(git log --oneline -5 2>/dev/null || echo '(not a git repo)') > /tmp/cat_r_git.txt 2>&1; "
    # Probe /tests/run-tests.sh — present in terminal-bench / SWE-bench containers.
    # Use explicit existence check so the sentinel fires reliably even when
    # grep finds nothing (a bare pipe makes || unreachable when the right side exits 0).
    "{ if [ -f /tests/run-tests.sh ]; then "
    "grep -E 'pytest' /tests/run-tests.sh | grep -v '#' | head -5; "
    "else echo '(no /tests/run-tests.sh)'; fi; } > /tmp/cat_r_eval_cmd.txt 2>&1; "
    "echo PROBES_DONE"
)

# Phase 2: read each file in isolation.
# `clear` before each read ensures the viewport shows ONLY this file's
# content, so capture_pane() reliably returns the probe output regardless
# of how much other output preceded it.
_PROBE_READS: list[tuple[str, str]] = [
    ("workdir",      "clear; head -40 /tmp/cat_r_workdir.txt"),
    ("ls_detail",    "clear; head -35 /tmp/cat_r_ls_la.txt"),
    ("file_types",   "clear; head -20 /tmp/cat_r_filetypes.txt"),
    ("notes",        "clear; head -60 /tmp/cat_r_notes.txt"),
    ("test_scripts", "clear; head -15 /tmp/cat_r_scripts.txt"),
    ("test_command", "clear; head -20 /tmp/cat_r_cmd.txt"),
    ("git_state",    "clear; head -10 /tmp/cat_r_git.txt"),
    ("eval_command", "clear; head -10 /tmp/cat_r_eval_cmd.txt"),
]


def _derive_judge_strategy(reps: list) -> "Representation":
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


def _first_meaningful_line(text: str) -> str:
    """Return the first non-empty, non-prompt line of text."""
    for ln in text.splitlines():
        stripped = ln.strip()
        # Skip shell prompts (e.g. "(testbed) root@...# " or "$ ")
        if stripped and not stripped.endswith("# ") and not stripped.endswith("$ "):
            return stripped[:200]
    return "(empty)"


class Representer:
    """Runs probes via the CapabilityRegistry; returns list[Representation].

    Cost: 1 (write phase) + len(_PROBE_READS) (read phase) = 5 tool calls,
    0 LLM calls, bounded tokens.
    """

    def __init__(self, registry: CapabilityRegistry, recorder: Recorder) -> None:
        self.registry = registry
        self.recorder = recorder

    def represent(
        self, contract: Contract, state: State, parent_event_id: "str | None" = None,
    ) -> list[Representation]:
        # Phase 1: run all probes and write to temp files.
        # tool_call chains to the LATENT state event (parent_event_id) so the
        # trace forms a real DAG, not a flat fan-out from contract.id.
        write_result = self.registry.invoke(
            name="bash",
            args={"command": _PROBE_WRITE_CMD, "timeout_s": 30},
            state=state,
            recorder=self.recorder,
            contract_id=contract.id,
            tool_call_id="rep-write",
            parent_event_id=parent_event_id,
        )
        contract.cost.tool_calls += 1
        contract.cost.wall_s += float(write_result.wall_s)

        # Phase 2: read each file back in isolation. Each read chains from the
        # previous probe (sequential causality) — forms a linear chain of
        # probe events that all summarize into contract_represent.
        prev_tc_id: str = "rep-write"
        reps: list[Representation] = []
        for key, read_cmd in _PROBE_READS:
            tc_id = f"rep-read-{key}"
            result = self.registry.invoke(
                name="bash",
                args={"command": read_cmd, "timeout_s": 10},
                state=state,
                recorder=self.recorder,
                contract_id=contract.id,
                tool_call_id=tc_id,
                parent_event_id=prev_tc_id,
            )
            contract.cost.tool_calls += 1
            contract.cost.wall_s += float(result.wall_s)
            evidence = result.output.strip()
            value = _first_meaningful_line(evidence)
            reps.append(Representation(key=key, value=value, evidence=evidence))
            prev_tc_id = tc_id

        # Derive judge strategy from probe outputs — no new tool calls.
        reps.append(_derive_judge_strategy(reps))

        # Static tool-strategy hint — flows into the planner's ENVIRONMENT section
        # so it writes steps that use the right tools for the task type.
        reps.append(Representation(
            key="tool_strategy",
            value="bash(search/run) + str_replace(edit existing) + write(new files only)",
            evidence=(
                "Available tools and when to use them:\n"
                "  bash(command)                    — grep/find to locate code, sed -n 'X,Yp' to view sections,\n"
                "                                     python/pytest to verify. Use for everything except file edits.\n"
                "  str_replace(path, old_str, new_str) — patch an existing file surgically.\n"
                "                                     No need to read the whole file first.\n"
                "                                     old_str must match exactly once (include enough context).\n"
                "  write(path, content)             — create brand-new files only.\n"
                "                                     Do NOT use to overwrite large existing source files.\n"
                "\n"
                "For code fixes: bash to locate → str_replace to patch → bash to verify.\n"
                "For new scripts/configs: write.\n"
                "For installs/tests: bash."
            ),
        ))

        # contract_represent summarizes all the probes; chain from the last
        # probe (the rep-read-eval_command tool_call).
        self.recorder.append(TraceEvent(
            event_id=f"rep-{contract.id[:8]}",
            parent_id=prev_tc_id,
            ts=utcnow_iso(),
            kind="contract_represent",
            payload={
                "contract_id": contract.id,
                "representations": [
                    {"key": r.key, "value": r.value} for r in reps
                ],
            },
        ))
        return reps

    def represent_event_id(self, contract: Contract) -> str:
        """Stable event_id for the contract_represent summary event."""
        return f"rep-{contract.id[:8]}"
