"""trace_view.py — compact human-readable decision trace for CAT and ReAct runs.

Reads a JSONL file from the Recorder and renders a tree/DAG view:
  - CAT: shows plan, per-iteration tool + judge + criterion, replans, final state
  - ReAct: shows per-iteration tool + outcome, final state

Usage:
    python -m cat.trace_view <path/to/run.jsonl>
    python -m cat.trace_view <path/to/run.jsonl> --full    # full tool output lines
    python -m cat.trace_view <path/to/run.jsonl> --width 120
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── helpers ──────────────────────────────────────────────────────────────────

def _clip(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\r", "")
    return s if len(s) <= n else s[:n - 1] + "…"


def _arg_preview(tool: str, args: dict) -> str:
    if tool == "bash":
        return _clip(str(args.get("command", "")), 70)
    if tool == "write":
        path = args.get("path", "?")
        content = str(args.get("content", ""))
        preview = _clip(content.splitlines()[0] if content.strip() else "", 35)
        return f"{path}  ← {repr(preview)}"
    if tool == "str_replace":
        path = args.get("path", "?")
        old = _clip(str(args.get("old_str", "")), 30)
        new = _clip(str(args.get("new_str", "")), 30)
        return f"{path}  {repr(old)} → {repr(new)}"
    if tool == "read":
        return str(args.get("path", "?"))
    if tool == "list":
        return str(args.get("path", "."))
    # fallback
    first_val = next(iter(args.values()), "") if args else ""
    return _clip(str(first_val), 60)


def _out_preview(output: str, n: int = 70) -> str:
    lines = [l for l in output.splitlines() if l.strip()]
    first = lines[0] if lines else ""
    return _clip(first, n)


# ── event parsing ─────────────────────────────────────────────────────────────

def _load(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def _is_cat(events: list[dict]) -> bool:
    kinds = {e["kind"] for e in events}
    return bool(kinds & {
        "contract_state",
        # accept both new (PRD 1.6.2.9) and legacy event names:
        "contract_stipulate", "contract_decompose",
        "capability_perform", "capability_invoke", "capability_activate",
    })


# ── data structures ───────────────────────────────────────────────────────────

@dataclass
class ToolStep:
    tool: str
    args: dict
    output: str
    is_error: bool
    wall_s: float = 0.0


@dataclass
class JudgeCheck:
    step_id: str
    description: str
    complete: bool


@dataclass
class CriterionCheck:
    satisfied: bool
    evidence: str


@dataclass
class Iteration:
    num: int
    tool: Optional[ToolStep] = None
    judges: list[JudgeCheck] = field(default_factory=list)
    criterion: Optional[CriterionCheck] = None
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


@dataclass
class PlanVersion:
    replan_idx: int          # 0 = initial (CAT v1.5 only emits the initial plan; replan removed)
    steps: list[tuple[str, str]]  # (id, description)
    first_iter: int          # iteration number when this plan was issued


@dataclass
class Representation:
    key: str
    value: str


@dataclass
class StateChange:
    from_state: Optional[str]
    to_state: str
    reason: str
    obligation: str = ""


@dataclass
class ParsedTrace:
    is_cat: bool
    agent: str
    model: str
    task_id: str
    obligation: str
    state_changes: list[StateChange] = field(default_factory=list)
    representations: list[Representation] = field(default_factory=list)
    represent_probes: list[ToolStep] = field(default_factory=list)   # representer tool calls
    plan_versions: list[PlanVersion] = field(default_factory=list)
    iterations: list[Iteration] = field(default_factory=list)
    final_state: str = ""
    final_reason: str = ""
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_usd: float = 0.0
    total_wall_s: float = 0.0
    n_iters: int = 0


# ── CAT parser ────────────────────────────────────────────────────────────────

def _parse_cat(events: list[dict]) -> ParsedTrace:
    trace = ParsedTrace(is_cat=True, agent="cat", model="", task_id="", obligation="")

    current_iter: Optional[Iteration] = None
    iter_map: dict[int, Iteration] = {}

    # pending tool call waiting for its tool_result
    pending_tool: Optional[dict] = None
    pending_tool_args: dict = {}

    # track which capability_activate tool_call_id is the "active" one
    active_call_id: Optional[str] = None

    # to accumulate llm tokens per iteration
    current_iter_num = 0

    # representer phase: tool calls before the first capability_activate
    in_represent_phase = True

    for ev in events:
        kind = ev["kind"]
        p = ev.get("payload", {})

        if kind == "contract_state":
            sc = StateChange(
                from_state=p.get("from"),
                to_state=p.get("to", ""),
                reason=p.get("reason", ""),
                obligation=p.get("obligation", ""),
            )
            trace.state_changes.append(sc)
            if sc.obligation:
                trace.obligation = sc.obligation
            if sc.to_state in ("fulfilled", "failed", "expired"):
                trace.final_state = sc.to_state
                trace.final_reason = sc.reason

        elif kind == "contract_represent":
            for r in p.get("representations", []):
                trace.representations.append(Representation(r["key"], r["value"]))

        elif kind in ("contract_stipulate", "contract_decompose"):
            steps = [(s["id"], s["description"]) for s in p.get("steps", [])]
            replan_idx = p.get("replan", 0) or 0
            # first_iter: next iteration that hasn't been assigned yet
            first_iter = current_iter_num + 1
            trace.plan_versions.append(PlanVersion(
                replan_idx=replan_idx,
                steps=steps,
                first_iter=first_iter,
            ))

        elif kind in ("capability_perform", "capability_invoke"):
            in_represent_phase = False
            num = int(p.get("iteration", current_iter_num + 1))
            current_iter_num = num
            active_call_id = p.get("tool_call_id")
            it = Iteration(num=num)
            iter_map[num] = it
            trace.iterations.append(it)
            current_iter = it
            # store pending info so tool_call/tool_result can fill it
            pending_tool = {"tool": p.get("tool", "?"), "args": p.get("arguments", {})}

        elif kind == "tool_call":
            if in_represent_phase:
                # representer probe — stash for pairing with tool_result
                pending_tool = {"tool": p.get("tool", "?"), "args": p.get("args", {})}
                pending_tool_args = p.get("args", {})
            elif pending_tool is not None:
                pending_tool_args = p.get("args", {})

        elif kind == "tool_result":
            if in_represent_phase and pending_tool is not None:
                args = p.get("args", pending_tool_args) or pending_tool.get("args", {})
                tid = str(p.get("tool_call_id", ""))
                tool_name = pending_tool["tool"]
                # label the probe by its call_id (rep-write, rep-read-workdir, etc.)
                probe_label = tid.replace("rep-", "") if tid.startswith("rep-") else tool_name
                trace.represent_probes.append(ToolStep(
                    tool=probe_label,
                    args=args,
                    output=str(p.get("output", "")),
                    is_error=bool(p.get("is_error", False)),
                    wall_s=float(p.get("wall_s", 0)),
                ))
                pending_tool = None
                pending_tool_args = {}
            elif current_iter is not None and pending_tool is not None:
                args = p.get("args", pending_tool_args) or pending_tool.get("args", {})
                current_iter.tool = ToolStep(
                    tool=pending_tool["tool"],
                    args=args,
                    output=str(p.get("output", "")),
                    is_error=bool(p.get("is_error", False)),
                    wall_s=float(p.get("wall_s", 0)),
                )
                pending_tool = None
                pending_tool_args = {}

        elif kind == "criterion_check":
            if p.get("judge"):
                # per-step advisory judge check
                jc = JudgeCheck(
                    step_id=p.get("step_id", ""),
                    description=p.get("description", ""),
                    complete=bool(p.get("complete", False)),
                )
                if current_iter is not None:
                    current_iter.judges.append(jc)
            else:
                # real deterministic criterion check
                cc = CriterionCheck(
                    satisfied=bool(p.get("satisfied", False)),
                    evidence=str(p.get("evidence") or ""),
                )
                if current_iter is not None:
                    current_iter.criterion = cc

        elif kind == "llm_usage":
            t_in = int(p.get("in", 0))
            t_out = int(p.get("out", 0))
            usd = float(p.get("usd", 0))
            trace.total_tokens_in += t_in
            trace.total_tokens_out += t_out
            trace.total_usd += usd
            if current_iter is not None:
                current_iter.tokens_in += t_in
                current_iter.tokens_out += t_out
                current_iter.usd += usd

        elif kind == "step_end":
            trace.n_iters = int(p.get("iterations", len(trace.iterations)))
            cost = p.get("cost", {})
            trace.total_wall_s = float(cost.get("wall_s", 0))
            if not trace.final_state:
                trace.final_state = p.get("final_state", "")

    # extract model from first llm_usage
    for ev in events:
        if ev["kind"] == "llm_usage":
            trace.model = ev.get("payload", {}).get("model", "")
            break

    return trace


# ── ReAct parser ──────────────────────────────────────────────────────────────

def _parse_react(events: list[dict]) -> ParsedTrace:
    trace = ParsedTrace(is_cat=False, agent="react", model="", task_id="", obligation="")

    iter_num = 0
    current_iter: Optional[Iteration] = None
    pending_call_id: Optional[str] = None
    pending_tool_name: str = ""
    pending_tool_args: dict = {}

    for ev in events:
        kind = ev["kind"]
        p = ev.get("payload", {})

        if kind == "session_start":
            trace.agent = p.get("agent", "react")
            trace.model = p.get("model", "")
            trace.task_id = p.get("task_id", "")

        elif kind == "user_message":
            trace.obligation = p.get("text", "")

        elif kind == "session_end":
            trace.final_state = p.get("verdict", "")
            trace.final_reason = p.get("failure_reason", "") or ""
            trace.n_iters = int(p.get("iterations", iter_num))
            trace.total_wall_s = float(p.get("wall_s", 0))

        elif kind == "llm_usage":
            t_in = int(p.get("in", 0))
            t_out = int(p.get("out", 0))
            usd = float(p.get("usd", 0))
            trace.total_tokens_in += t_in
            trace.total_tokens_out += t_out
            trace.total_usd += usd
            # new iteration on each llm call that had tool_calls
            if p.get("n_tool_calls", 0) > 0:
                iter_num += 1
                current_iter = Iteration(num=iter_num,
                                         tokens_in=t_in, tokens_out=t_out, usd=usd)
                trace.iterations.append(current_iter)
            elif current_iter is not None:
                current_iter.tokens_in += t_in
                current_iter.tokens_out += t_out
                current_iter.usd += usd

        elif kind == "tool_call":
            pending_call_id = p.get("tool_call_id")
            pending_tool_name = p.get("tool", "?")
            pending_tool_args = p.get("args", {})

        elif kind == "tool_result":
            if current_iter is not None and p.get("tool_call_id") == pending_call_id:
                current_iter.tool = ToolStep(
                    tool=pending_tool_name,
                    args=pending_tool_args,
                    output=str(p.get("output", "")),
                    is_error=bool(p.get("is_error", False)),
                    wall_s=float(p.get("wall_s", 0)),
                )

    # extract model from first llm_usage if not set
    if not trace.model:
        for ev in events:
            if ev["kind"] == "llm_usage":
                trace.model = ev.get("payload", {}).get("model", "")
                break

    # fallbacks when session_end is absent (terminal-bench _MemRecorder)
    if trace.n_iters == 0:
        trace.n_iters = len(trace.iterations)
    if not trace.final_state:
        # infer from whether any tool was used
        trace.final_state = "ended" if trace.iterations else "empty"

    return trace


# ── renderer ──────────────────────────────────────────────────────────────────

def render(trace: ParsedTrace, width: int = 100, full_output: bool = False) -> str:
    W = width
    lines: list[str] = []

    def rule(char="─"):
        return char * W

    def header(text: str, char="═") -> str:
        pad = max(0, W - len(text) - 2)
        return f"{char} {text} {char * pad}"

    # ── title ──
    model_short = trace.model.split("/")[-1] if trace.model else "?"
    agent_label = "CAT" if trace.is_cat else "ReAct"
    task_short = trace.task_id or _clip(trace.obligation, 50)
    final_upper = trace.final_state.upper() if trace.final_state else "?"
    lines.append(header(f"{agent_label}  {task_short}  [{final_upper}]"))
    lines.append(
        f"  model={model_short}"
        f"  iters={trace.n_iters}"
        f"  tokens={trace.total_tokens_in}in/{trace.total_tokens_out}out"
        f"  ${trace.total_usd:.4f}"
        + (f"  {trace.total_wall_s:.0f}s" if trace.total_wall_s else "")
    )

    # ── obligation ──
    if trace.obligation:
        lines.append("")
        lines.append("OBLIGATION:")
        for ln in trace.obligation.strip().splitlines()[:4]:
            lines.append(f"  {_clip(ln, W - 4)}")
        if len(trace.obligation.strip().splitlines()) > 4:
            lines.append("  …")

    # ── representations (CAT only) ──
    if trace.is_cat and (trace.representations or trace.represent_probes):
        lines.append("")
        lines.append("REPRESENT:")
        if trace.represent_probes:
            for p in trace.represent_probes:
                status = "✗ ERR" if p.is_error else "✓"
                out_p = _out_preview(p.output, W - 28)
                label = f"[{p.tool}]"
                lines.append(f"  {label:20s} {status}  {out_p}")
        if trace.representations:
            lines.append("  ──")
            for r in trace.representations:
                lines.append(f"  {r.key:15s}  {_clip(r.value, W - 20)}")

    # ── plan versions (CAT only) ──
    if trace.is_cat and trace.plan_versions:
        for pv in trace.plan_versions:
            lines.append("")
            if pv.replan_idx > 0:
                lines.append(f"PLAN (replan #{pv.replan_idx}, iter {pv.first_iter}+):")
            else:
                lines.append("PLAN (initial):")
            for step_id, desc in pv.steps:
                lines.append(f"  [{step_id}] {_clip(desc, W - 10)}")

    # ── iterations ──
    lines.append("")
    lines.append(rule())

    # figure out which plan version is active at each iter
    def _active_plan(iter_num: int) -> Optional[PlanVersion]:
        active = None
        for pv in trace.plan_versions:
            if pv.first_iter <= iter_num:
                active = pv
        return active

    # track which steps are done (updated as we encounter judge completions)
    completed_steps: set[str] = set()

    for it in trace.iterations:
        active_plan = _active_plan(it.num) if trace.is_cat else None

        # ── iter header ──
        if trace.is_cat and active_plan:
            pending = [sid for sid, _ in active_plan.steps if sid not in completed_steps]
            step_label = f"step={pending[0]}" if pending else "all-steps-done"
            hdr = f"  iter {it.num:02d}  {step_label}"
        else:
            hdr = f"  iter {it.num:02d}"

        tok_info = f"  ({it.tokens_in}in/{it.tokens_out}out)"
        lines.append(f"┌{'─' * (W - 1)}")
        lines.append(f"│{hdr}{tok_info}")

        # ── tool call ──
        if it.tool:
            t = it.tool
            status = "✗ ERR" if t.is_error else "✓"
            arg_str = _arg_preview(t.tool, t.args)
            call_line = f"│  {t.tool}({arg_str})"
            call_line = _clip(call_line, W - 8) + f"  → {status}"
            lines.append(call_line)

            # output
            if full_output:
                for ol in t.output.splitlines()[:8]:
                    lines.append(f"│      {_clip(ol, W - 8)}")
            else:
                out_p = _out_preview(t.output, W - 12)
                if out_p:
                    lines.append(f"│      {out_p}")
        else:
            lines.append("│  (no tool call — model sent text)")

        # ── judge checks (CAT) ──
        for jc in it.judges:
            mark = "✓" if jc.complete else "✗"
            lines.append(f"│  judge[{jc.step_id}]: {mark}")
            if jc.complete:
                completed_steps.add(jc.step_id)

        # ── criterion check ──
        if it.criterion:
            cc = it.criterion
            mark = "PASS ✓" if cc.satisfied else "FAIL ✗"
            ev_str = _clip(cc.evidence, W - 20) if cc.evidence else ""
            lines.append(f"│  criterion: {mark}  {ev_str}")

    lines.append(f"└{'─' * (W - 1)}")

    # ── final result ──
    lines.append("")
    lines.append("RESULT:")
    state_str = trace.final_state.upper() if trace.final_state else "UNKNOWN"
    lines.append(f"  {state_str}" + (f"  — {_clip(trace.final_reason, W - 10)}" if trace.final_reason else ""))

    # ── state machine (CAT) ──
    if trace.is_cat and trace.state_changes:
        lines.append("")
        lines.append("STATE MACHINE:")
        for sc in trace.state_changes:
            arrow = f"{sc.from_state}→{sc.to_state}" if sc.from_state else sc.to_state
            lines.append(f"  {arrow:25s}  {_clip(sc.reason, W - 30)}")

    lines.append(rule("═"))
    return "\n".join(lines)


# ── summary mode: compare two traces side by side ────────────────────────────

def summary_line(trace: ParsedTrace) -> str:
    agent = "CAT  " if trace.is_cat else "ReAct"
    state = (trace.final_state or "?").upper()
    model = trace.model.split("/")[-1] if trace.model else "?"
    n_replans = len(trace.plan_versions) - 1 if trace.is_cat and trace.plan_versions else 0
    n_crit_pass = sum(1 for it in trace.iterations if it.criterion and it.criterion.satisfied)
    n_crit_fail = sum(1 for it in trace.iterations if it.criterion and not it.criterion.satisfied)
    return (
        f"[{agent}] {state:10s}"
        f"  iters={trace.n_iters:3d}"
        f"  tools={len(trace.iterations):3d}"
        f"  tokens={trace.total_tokens_in + trace.total_tokens_out:7d}"
        f"  ${trace.total_usd:.4f}"
        + (f"  replans={n_replans}" if trace.is_cat else "")
        + (f"  crit={n_crit_pass}pass/{n_crit_fail}fail" if trace.is_cat else "")
    )


# ── entry point ───────────────────────────────────────────────────────────────

def view_file(path: Path, width: int = 100, full_output: bool = False) -> ParsedTrace:
    events = _load(path)
    if not events:
        print(f"[trace_view] no events found in {path}", file=sys.stderr)
        sys.exit(1)
    if _is_cat(events):
        trace = _parse_cat(events)
    else:
        trace = _parse_react(events)
    trace.task_id = trace.task_id or path.parent.parent.name
    print(render(trace, width=width, full_output=full_output))
    return trace


def main() -> None:
    ap = argparse.ArgumentParser(prog="cat.trace_view",
                                 description="Human-readable decision trace for CAT/ReAct runs.")
    ap.add_argument("jsonl", nargs="+", help="Path(s) to .jsonl trace file(s)")
    ap.add_argument("--full", action="store_true", help="Show full tool output (up to 8 lines)")
    ap.add_argument("--width", type=int, default=100, help="Output width (default 100)")
    ap.add_argument("--summary", action="store_true",
                    help="One-line summary per file instead of full trace")
    args = ap.parse_args()

    for raw_path in args.jsonl:
        path = Path(raw_path)
        if not path.exists():
            print(f"[trace_view] not found: {path}", file=sys.stderr)
            continue
        events = _load(path)
        if not events:
            print(f"[trace_view] empty: {path}", file=sys.stderr)
            continue
        trace = _parse_cat(events) if _is_cat(events) else _parse_react(events)
        trace.task_id = trace.task_id or path.parent.parent.name
        if args.summary:
            print(f"{path.name:50s}  {summary_line(trace)}")
        else:
            print(render(trace, width=args.width, full_output=args.full))
            if len(args.jsonl) > 1:
                print()


if __name__ == "__main__":
    main()
