"""KPI report generator — walks run output, aggregates metrics, emits HTML."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

_KEY_ARG = {
    "read": "path", "write": "path", "list": "path",
    "outline": "path", "str_replace": "path", "multi_edit": "path",
    "rg": "pattern", "fd": "pattern",
    "goto_def": "name", "find_refs": "name",
    "bash": "command", "git_status": "workdir", "git_diff": "path",
    "apply_patch": "workdir",
}


def _tool_usage_from_events(events: list[dict]) -> tuple[dict[str, int], list[str]]:
    """Extract per-tool call counts and redundancy flags from a trace.

    Only counts perform-layer calls — REPRESENT probes (rep-*) and oracle
    checks (oracle-*) are excluded.
    """
    counts: dict[str, int] = {}
    keyed: dict[tuple[str, str], int] = {}
    last_sig: Optional[tuple[str, str]] = None
    last_run: int = 0
    flags: list[str] = []

    for ev in events:
        if ev.get("kind") != "tool_call":
            continue
        p = ev.get("payload", {}) or {}
        tc_id = str(p.get("tool_call_id", ""))
        if tc_id.startswith("rep-") or tc_id.startswith("oracle-"):
            continue
        tool = str(p.get("tool", ""))
        if not tool:
            continue
        counts[tool] = counts.get(tool, 0) + 1
        args = p.get("args") or {}
        key_field = _KEY_ARG.get(tool, "")
        key_val = str(args.get(key_field, ""))[:80] if key_field else ""
        sig = (tool, key_val)
        if key_val:
            keyed[sig] = keyed.get(sig, 0) + 1
        if last_sig == sig and sig[1]:
            last_run += 1
        else:
            if last_run >= 2 and last_sig is not None:
                flags.append(f"{last_sig[0]} {last_sig[1]!r} called {last_run + 1}× back-to-back")
            last_sig = sig
            last_run = 0
    if last_run >= 2 and last_sig is not None:
        flags.append(f"{last_sig[0]} {last_sig[1]!r} called {last_run + 1}× back-to-back")
    for (tool, key_val), n in keyed.items():
        if n >= 4 and tool in {"read", "list"}:
            suggest = " (consider `outline`)" if tool == "read" else ""
            flags.append(f"{tool} {key_val!r} called {n}× total{suggest}")
    seen: set[str] = set()
    deduped: list[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return counts, deduped[:10]


@dataclass
class TaskReport:
    task_id: str = ""
    verdict: str = ""
    iterations: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    failure_reason: str = ""


@dataclass
class RunReport:
    run_dir: str = ""
    tasks: list[TaskReport] = field(default_factory=list)
    total_tasks: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0


def generate_report(run_dir: str, output_dir: str) -> None:
    """Generate HTML + JSON KPI report from a run directory."""
    ...
