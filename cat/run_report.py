"""Run-level KPI report — walks a tb runs output directory, aggregates per-task
metrics, runs layer attribution, and emits an HTML report.

Usage:
    python -m cat.run_report data/tb-runs/v1.5/swe10-v1.5-envelope
    python -m cat.run_report <dir> --no-llm      # skip LLM attribution
    python -m cat.run_report <dir> --model openrouter/deepseek/deepseek-v4-flash

Produces, next to results.json:
    _kpi_report.html       — self-contained HTML for human review
    _kpi_report.json       — machine-readable for cross-run comparison
    _attribution_cache/    — cached attributions keyed by trace hash

The HTML is intentionally single-file (no external CSS/JS) so it opens cleanly
from anywhere — local browser, scp'd to laptop, etc.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from cat.attribution import Attribution, attribute_task
from cat.dag import RunDAG
from cat.dag_svg import render_dag_svg
from cat.metrics import RunMetrics
from cat.trace_html import render_trace_html


# ── Schemas ────────────────────────────────────────────────────────────────


@dataclass
class TaskReport:
    task_id: str
    verdict: str               # PASS | FAIL | SUSPENDED | INFRA_ERROR
    failure_mode: str          # raw from results.json: unset | agent_timeout | ...
    iters: int
    tokens_in: int
    tokens_out: int
    usd: float
    wall_s: float
    plan_steps_total: int = 0
    plan_steps_completed: int = 0
    final_reason: str = ""
    attribution: Optional[Attribution] = None
    dag_svg: str = ""           # inline SVG rendering of this task's RunDAG (1.6.3)
    trace_html: str = ""        # chronological reasoning-trace HTML (1.6.3)
    # 1.8.0 — tool-economy: per-task tool usage breakdown
    tool_usage: dict[str, int] = field(default_factory=dict)        # {tool_name: count}
    tool_waste_flags: list[str] = field(default_factory=list)        # human-readable redundancy notes


@dataclass
class RunReport:
    run_id: str
    agent: str
    model: str
    n_tasks: int
    n_pass: int
    n_fail: int
    n_infra_error: int
    pass_rate: float
    started_at: str
    ended_at: str
    wall_s_total: float
    tokens_in_total: int
    tokens_out_total: int
    usd_total: float
    failure_breakdown: dict[str, int] = field(default_factory=dict)
    iter_dist_pass: dict[str, float] = field(default_factory=dict)
    iter_dist_fail: dict[str, float] = field(default_factory=dict)
    role_token_share: dict[str, float] = field(default_factory=dict)
    primary_attribution_counts: dict[str, int] = field(default_factory=dict)
    # 1.6.5: cost by CAT layer (represent / stipulate / activate / perform / verify)
    cost_by_layer: dict[str, dict] = field(default_factory=dict)
    # 1.8.0 — tool-economy: aggregate tool-use stats across the run
    tool_usage_total: dict[str, int] = field(default_factory=dict)   # {tool_name: total_calls}
    tool_calls_per_task_avg: float = 0.0
    tool_waste_count: int = 0                                         # number of tasks with any waste flag
    tasks: list[TaskReport] = field(default_factory=list)


# ── Layer cost computation ─────────────────────────────────────────────────


_ROLE_TO_LAYER = {
    "stipulator": "stipulate", "planner": "stipulate",
    "performer":  "perform",   "executor": "perform",
    "attestor":   "verify",    "judge":    "verify",
    "auditor":    "verify",
}


def _cost_by_layer_from_events(events: list[dict]) -> dict[str, dict]:
    """Aggregate tokens / wall_s / tool_calls / llm_calls / usd per CAT layer.

    Layer assignment rules:
      - llm_usage event → mapped to layer by `payload.role`
      - tool_call / tool_result event → mapped by `tool_call_id` prefix:
          "rep-*"    → REPRESENT (probes)
          "oracle-*" → VERIFY (criterion check)
          otherwise  → PERFORM (regular invocation)
      - contract_admit → ACTIVATE (zero-cost gate, but counted as 1 admission)
    """
    layers = {
        l: {"tokens_in": 0, "tokens_out": 0, "wall_s": 0.0,
            "tool_calls": 0, "llm_calls": 0, "usd": 0.0}
        for l in ("represent", "stipulate", "activate", "perform", "verify")
    }

    def _layer_for_tool_call_id(tc_id: str) -> str:
        if tc_id.startswith("rep-"):
            return "represent"
        if tc_id.startswith("oracle-"):
            return "verify"
        return "perform"

    for ev in events:
        kind = ev.get("kind", "")
        p = ev.get("payload", {}) or {}

        if kind == "llm_usage":
            role = (p.get("role") or "").strip()
            layer = _ROLE_TO_LAYER.get(role, "perform")
            layers[layer]["tokens_in"]  += int(p.get("in", 0))
            layers[layer]["tokens_out"] += int(p.get("out", 0))
            layers[layer]["wall_s"]     += float(p.get("wall_s", 0.0))
            layers[layer]["usd"]        += float(p.get("usd", 0.0))
            layers[layer]["llm_calls"]  += 1
        elif kind == "tool_call":
            layers[_layer_for_tool_call_id(p.get("tool_call_id", ""))]["tool_calls"] += 1
        elif kind == "tool_result":
            layers[_layer_for_tool_call_id(p.get("tool_call_id", ""))]["wall_s"] += float(p.get("wall_s", 0.0))
        elif kind == "contract_admit":
            layers["activate"]["tool_calls"] += 0   # no-op; just a marker
    return layers


def _accumulate_layer_costs(
    total: dict[str, dict], add: dict[str, dict],
) -> dict[str, dict]:
    """Sum two layer-cost dicts in place."""
    for layer, costs in add.items():
        if layer not in total:
            total[layer] = {k: 0 for k in costs}
        for k, v in costs.items():
            total[layer][k] = total[layer].get(k, 0) + v
    return total


# ── Tool-economy (1.8.0) ───────────────────────────────────────────────────
#
# CAT's value proposition: not "more tools" but "the right tool, used once."
# These helpers measure whether the agent is actually being economical with
# its tool surface. They are heuristics — the LLM auditor in attribution.py
# gets the final say on whether usage was wasteful.


def _tool_usage_from_events(events: list[dict]) -> tuple[dict[str, int], list[str]]:
    """Extract per-tool call counts and redundancy flags from a trace.

    Returns:
        (counts, waste_flags)
        counts        — {tool_name: total_perform-layer calls}
        waste_flags   — human-readable strings flagging suspected redundancy:
                          - "read x.py called 4× (consider outline)"
                          - "list . called 3× back-to-back"
                          - "bash 'pytest …' duplicated 2× identically"

    Only counts perform-layer calls — REPRESENT probes (rep-*) and oracle
    checks (oracle-*) are not the agent's fault.
    """
    counts: dict[str, int] = {}
    # Track per-(tool, key-arg) calls; key-arg is path for read/write/outline,
    # command for bash, pattern for rg/fd, name for goto_def/find_refs.
    keyed: dict[tuple[str, str], int] = {}
    last_signature: Optional[tuple[str, str]] = None
    last_signature_run: int = 0  # consecutive-identical streak length
    flags: list[str] = []

    _key_arg = {
        "read": "path", "write": "path", "list": "path",
        "outline": "path", "str_replace": "path", "multi_edit": "path",
        "rg": "pattern", "fd": "pattern",
        "goto_def": "name", "find_refs": "name",
        "bash": "command", "git_status": "workdir", "git_diff": "path",
        "apply_patch": "workdir",
    }

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
        key_field = _key_arg.get(tool, "")
        key_val = str(args.get(key_field, ""))[:80] if key_field else ""
        sig = (tool, key_val)
        if key_val:
            keyed[sig] = keyed.get(sig, 0) + 1
        if last_signature == sig and sig[1]:
            last_signature_run += 1
        else:
            if last_signature_run >= 2 and last_signature is not None:
                flags.append(
                    f"{last_signature[0]} {last_signature[1]!r} called {last_signature_run+1}× back-to-back"
                )
            last_signature = sig
            last_signature_run = 0
    # Tail flush
    if last_signature_run >= 2 and last_signature is not None:
        flags.append(
            f"{last_signature[0]} {last_signature[1]!r} called {last_signature_run+1}× back-to-back"
        )

    # Repeated-same-arg flags (not necessarily consecutive)
    for (tool, key_val), n in keyed.items():
        if n >= 4 and tool in {"read", "list"}:
            suggest = " (consider `outline`)" if tool == "read" else ""
            flags.append(f"{tool} {key_val!r} called {n}× total{suggest}")

    # Dedup, cap length
    seen = set()
    deduped: list[str] = []
    for f in flags:
        if f in seen:
            continue
        seen.add(f)
        deduped.append(f)
    return counts, deduped[:10]


# ── Walker ─────────────────────────────────────────────────────────────────


def summarize_run(
    run_dir: Path,
    *,
    use_llm: bool = True,
    model: str = "openrouter/deepseek/deepseek-v4-flash",
) -> RunReport:
    run_dir = Path(run_dir)
    results_path = run_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"results.json not found in {run_dir}")
    with results_path.open() as f:
        results = json.load(f)

    metadata_path = run_dir / "run_metadata.json"
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())

    # Lazy LLM setup — only construct when first needed
    llm = None
    if use_llm:
        try:
            from cat.llm import LLM
            llm = LLM(model=model)
        except Exception as exc:
            print(f"warning: could not init LLM ({exc}); falling back to heuristic")
            llm = None

    cache_dir = run_dir / "_attribution_cache"

    tasks: list[TaskReport] = []
    failure_breakdown: dict[str, int] = {}
    iters_pass: list[int] = []
    iters_fail: list[int] = []
    primary_counts: dict[str, int] = {}
    role_tokens_in: dict[str, int] = {}
    cost_by_layer_total: dict[str, dict] = {}
    _attr_inputs: list[dict] = []     # 1.6.5.2: inputs for parallel attribution

    for r in results.get("results", []):
        tid = r["task_id"]
        # Find agent JSONL
        trial_dir = run_dir / tid / r["trial_name"] / "agent-logs"
        jsonls = list(trial_dir.glob("*.jsonl")) if trial_dir.exists() else []

        events: list[dict] = []
        metrics: Optional[RunMetrics] = None
        if jsonls:
            jsonl = jsonls[0]
            try:
                with jsonl.open() as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            events.append(json.loads(line))
                metrics = RunMetrics.from_jsonl(jsonl)
            except Exception as exc:
                print(f"warning: failed to parse {jsonl}: {exc}")

        # Outcome classification
        is_resolved = bool(r.get("is_resolved"))
        failure_mode = r.get("failure_mode") or "unset"
        verdict = _verdict(is_resolved, failure_mode, metrics, events)
        failure_breakdown[failure_mode if not is_resolved else "PASS"] = (
            failure_breakdown.get(failure_mode if not is_resolved else "PASS", 0) + 1
        )

        # Iteration count
        iters = 0
        if metrics is not None:
            iters = metrics.iterations
        if is_resolved:
            iters_pass.append(iters)
        elif metrics is not None and iters > 0:
            iters_fail.append(iters)

        # Token share by role
        if metrics is not None:
            for role, n in (metrics.tokens_in_by_role or {}).items():
                role_tokens_in[role] = role_tokens_in.get(role, 0) + int(n)

        # Final state reason
        final_reason = ""
        if events:
            for ev in reversed(events):
                if ev.get("kind") == "contract_state" and not ev.get("payload", {}).get("rejected"):
                    final_reason = ev.get("payload", {}).get("reason", "") or ""
                    break

        # Wall time from harness (more accurate than internal cost.wall_s)
        wall_s = 0.0
        if r.get("agent_started_at") and r.get("agent_ended_at"):
            try:
                s = datetime.fromisoformat(r["agent_started_at"])
                e = datetime.fromisoformat(r["agent_ended_at"])
                wall_s = (e - s).total_seconds()
            except Exception:
                pass

        # Build DAG + reasoning-trace renderings for this task (1.6.3)
        dag_svg = ""
        trace_html_str = ""
        if events:
            try:
                dag = RunDAG.from_events(events)
                dag_svg = render_dag_svg(dag)
            except Exception as exc:
                print(f"warning: DAG render failed for {tid}: {exc}")
            try:
                trace_html_str = render_trace_html(events)
            except Exception as exc:
                print(f"warning: trace HTML render failed for {tid}: {exc}")
            # Accumulate per-layer cost into the run total (1.6.5)
            try:
                _accumulate_layer_costs(cost_by_layer_total, _cost_by_layer_from_events(events))
            except Exception as exc:
                print(f"warning: cost-by-layer failed for {tid}: {exc}")

        # 1.8.0 — tool-economy: extract per-task tool counts + waste flags
        tu_counts: dict[str, int] = {}
        tu_flags: list[str] = []
        if events:
            try:
                tu_counts, tu_flags = _tool_usage_from_events(events)
            except Exception as exc:
                print(f"warning: tool-usage failed for {tid}: {exc}")

        # Build a "shell" TaskReport now; attribution filled in parallel below
        # (1.6.5.2 — was serial LLM call per task, now ThreadPoolExecutor-parallel)
        tasks.append(TaskReport(
            task_id=tid,
            verdict=verdict,
            failure_mode=failure_mode,
            iters=iters,
            tokens_in=int(r.get("total_input_tokens") or 0)
                     or (metrics.tokens_in if metrics else 0),
            tokens_out=int(r.get("total_output_tokens") or 0)
                      or (metrics.tokens_out if metrics else 0),
            usd=(metrics.usd if metrics else 0.0),
            wall_s=wall_s,
            plan_steps_total=(metrics.plan_steps_total if metrics else 0),
            plan_steps_completed=(metrics.plan_steps_completed if metrics else 0),
            final_reason=final_reason,
            attribution=None,   # filled in the parallel phase below
            dag_svg=dag_svg,
            trace_html=trace_html_str,
            tool_usage=tu_counts,
            tool_waste_flags=tu_flags,
        ))
        # Stash inputs for the parallel attribution phase
        _attr_inputs.append({
            "tid": tid,
            "metrics": metrics,
            "events": events,
            "verdict": verdict,
            "task_report_idx": len(tasks) - 1,
        })

    # 1.6.5.2: parallel attribution phase
    # Each task's attribution is independent; fire them in parallel via thread pool
    # (LLM.chat is sync HTTP, so threads work; asyncio would require LLM rewrite).
    runnable = [
        x for x in _attr_inputs
        if x["metrics"] is not None and x["events"]
    ]
    if runnable:
        def _attr_one(x: dict) -> tuple[int, Optional[Attribution]]:
            try:
                a = attribute_task(
                    x["tid"], x["metrics"], x["events"],
                    outcome=x["verdict"],
                    llm=llm, cache_dir=cache_dir,
                )
                return x["task_report_idx"], a
            except Exception as exc:
                print(f"warning: attribution failed for {x['tid']}: {exc}")
                return x["task_report_idx"], None

        max_workers = min(10, len(runnable))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for fut in as_completed([ex.submit(_attr_one, x) for x in runnable]):
                idx, attribution = fut.result()
                if attribution is not None:
                    tasks[idx].attribution = attribution
                    primary_counts[attribution.primary] = (
                        primary_counts.get(attribution.primary, 0) + 1
                    )

    # Aggregate
    n_tasks = len(tasks)
    n_pass = sum(1 for t in tasks if t.verdict == "PASS")
    n_infra = sum(1 for t in tasks if t.verdict == "INFRA_ERROR")
    n_fail = n_tasks - n_pass - n_infra
    wall_total = sum(t.wall_s for t in tasks)
    tokens_in_total = sum(t.tokens_in for t in tasks)
    tokens_out_total = sum(t.tokens_out for t in tasks)
    usd_total = sum(t.usd for t in tasks)

    iter_pass = _percentiles(iters_pass)
    iter_fail = _percentiles(iters_fail)

    total_role_tokens = sum(role_tokens_in.values()) or 1
    role_share = {role: n / total_role_tokens for role, n in role_tokens_in.items()}

    # 1.8.0 — aggregate tool-economy across tasks
    tool_usage_total: dict[str, int] = {}
    total_perform_calls = 0
    tasks_with_waste = 0
    for t in tasks:
        for tool, n in (t.tool_usage or {}).items():
            tool_usage_total[tool] = tool_usage_total.get(tool, 0) + int(n)
            total_perform_calls += int(n)
        if t.tool_waste_flags:
            tasks_with_waste += 1
    avg_calls_per_task = (total_perform_calls / n_tasks) if n_tasks else 0.0

    started = ""
    ended = ""
    if results.get("results"):
        starts = [r.get("trial_started_at") for r in results["results"] if r.get("trial_started_at")]
        ends = [r.get("trial_ended_at") for r in results["results"] if r.get("trial_ended_at")]
        if starts:
            started = min(starts)
        if ends:
            ended = max(ends)

    return RunReport(
        run_id=metadata.get("run_id", run_dir.name),
        agent=metadata.get("agent_name", ""),
        model=str(metadata.get("model_name") or ""),
        n_tasks=n_tasks,
        n_pass=n_pass,
        n_fail=n_fail,
        n_infra_error=n_infra,
        pass_rate=(n_pass / n_tasks if n_tasks else 0.0),
        started_at=started,
        ended_at=ended,
        wall_s_total=wall_total,
        tokens_in_total=tokens_in_total,
        tokens_out_total=tokens_out_total,
        usd_total=usd_total,
        failure_breakdown=failure_breakdown,
        iter_dist_pass=iter_pass,
        iter_dist_fail=iter_fail,
        role_token_share=role_share,
        primary_attribution_counts=primary_counts,
        cost_by_layer=cost_by_layer_total,
        tool_usage_total=tool_usage_total,
        tool_calls_per_task_avg=avg_calls_per_task,
        tool_waste_count=tasks_with_waste,
        tasks=tasks,
    )


def _verdict(is_resolved: bool, failure_mode: str, metrics, events) -> str:
    if is_resolved:
        return "PASS"
    if failure_mode == "unknown_agent_error":
        return "INFRA_ERROR"
    # Check for SUSPENDED in trace
    if events:
        for ev in reversed(events):
            if ev.get("kind") == "contract_state" and not ev.get("payload", {}).get("rejected"):
                to = ev.get("payload", {}).get("to", "")
                if to == "suspended":
                    return "SUSPENDED"
                break
    return "FAIL"


def _percentiles(values: list[int]) -> dict[str, float]:
    if not values:
        return {"n": 0, "min": 0, "p50": 0, "p75": 0, "p90": 0, "max": 0, "mean": 0.0}
    s = sorted(values)
    return {
        "n": len(s),
        "min": s[0],
        "p50": s[len(s) // 2],
        "p75": s[3 * len(s) // 4] if len(s) > 1 else s[0],
        "p90": s[max(0, int(0.9 * len(s)) - 1)] if len(s) > 1 else s[0],
        "max": s[-1],
        "mean": round(statistics.mean(s), 1),
    }


# ── HTML rendering ─────────────────────────────────────────────────────────


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CAT KPI Report — {run_id}</title>
<style>
  body {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         max-width: 1280px; margin: 24px auto; padding: 0 16px;
         background: #fafafa; color: #1a1a1a; line-height: 1.5; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
           gap: 12px; margin-bottom: 24px; }}
  .card {{ background: white; border: 1px solid #e5e5e5; border-radius: 6px;
           padding: 12px 16px; }}
  .card .label {{ color: #888; font-size: 11px; text-transform: uppercase;
                  letter-spacing: 0.05em; }}
  .card .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .card .sub2 {{ font-size: 11px; color: #888; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border: 1px solid #e5e5e5; border-radius: 6px; font-size: 13px;
           overflow: hidden; }}
  th {{ background: #f5f5f5; padding: 8px 12px; text-align: left; font-weight: 600;
        cursor: pointer; user-select: none; border-bottom: 1px solid #e5e5e5; }}
  th:hover {{ background: #ebebeb; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  tr.expandable {{ cursor: pointer; }}
  tr.expandable:hover {{ background: #f8f8f8; }}
  tr.detail {{ background: #fafafa; }}
  tr.detail td {{ padding: 12px 24px; font-size: 12px; }}
  .pass {{ color: #0a7c2f; font-weight: 600; }}
  .fail {{ color: #c0392b; font-weight: 600; }}
  .susp {{ color: #d97706; font-weight: 600; }}
  .infra {{ color: #888; font-weight: 600; }}
  .bar {{ background: #e5e5e5; height: 8px; border-radius: 4px; overflow: hidden;
          margin-top: 4px; }}
  .bar-fill {{ background: #3b82f6; height: 100%; }}
  .layer-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
                 margin-top: 8px; }}
  .layer {{ padding: 6px 8px; border-radius: 4px; font-size: 11px; }}
  .OK {{ background: #d1fae5; color: #065f46; }}
  .DEGRADED {{ background: #fef3c7; color: #92400e; }}
  .FAILED {{ background: #fee2e2; color: #991b1b; }}
  .NA {{ background: #f3f4f6; color: #6b7280; }}
  .primary {{ font-weight: 600; border: 2px solid currentColor; }}
  .rationale {{ font-style: italic; color: #555; margin-top: 8px; }}
  .meta {{ font-size: 11px; color: #888; margin-top: 4px; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #3b82f6; font-size: 11px; }}
  .footer {{ margin-top: 32px; font-size: 11px; color: #888; text-align: center; }}
</style>
</head>
<body>
  <h1>{run_id}</h1>
  <div class="sub">{agent} · model {model} · {started} → {ended}</div>

  <div class="grid">
    <div class="card">
      <div class="label">Pass rate</div>
      <div class="value {pass_class}">{n_pass}/{n_tasks} = {pass_pct}%</div>
      <div class="sub2">{n_infra} infra errors</div>
    </div>
    <div class="card">
      <div class="label">Total wall</div>
      <div class="value">{wall_h}h {wall_m}m</div>
      <div class="sub2">mean {wall_mean_min:.1f} min/task</div>
    </div>
    <div class="card">
      <div class="label">Total tokens</div>
      <div class="value">{tokens_total_h}</div>
      <div class="sub2">in {tokens_in_h} / out {tokens_out_h}</div>
    </div>
    <div class="card">
      <div class="label">Total USD</div>
      <div class="value">${usd_total:.3f}</div>
      <div class="sub2">${usd_per_task:.4f}/task</div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="label">Failure breakdown</div>
      {failure_bd_html}
    </div>
    <div class="card">
      <div class="label">Iteration distribution</div>
      {iter_dist_html}
    </div>
    <div class="card">
      <div class="label">Token share by role</div>
      {role_share_html}
    </div>
    <div class="card">
      <div class="label">Primary attribution (PASS credit / FAIL blame)</div>
      {attribution_html}
    </div>
  </div>

  <div class="grid" style="grid-template-columns: 1fr;">
    <div class="card">
      <div class="label">Cost by CAT layer</div>
      <div class="sub2" style="margin-top:2px;">
        Aggregated across all tasks. LLM costs by role; tool costs by tool_call_id prefix
        (rep-* → REPRESENT, oracle-* → VERIFY, other → PERFORM).
      </div>
      {layer_cost_html}
    </div>
  </div>

  <div class="grid" style="grid-template-columns: 1fr 1fr;">
    <div class="card">
      <div class="label">Where failures cluster (limitation kind)</div>
      <div class="sub2" style="margin-top:2px;">Per-task root-cause categorization (FAIL/SUSPENDED only)</div>
      {limitation_kind_html}
    </div>
    <div class="card">
      <div class="label">Where to invest (improvement vector)</div>
      <div class="sub2" style="margin-top:2px;">Maps each failure to actionable next step</div>
      {improvement_vector_html}
    </div>
  </div>

  <div class="grid" style="grid-template-columns: 1fr;">
    <div class="card">
      <div class="label">Tool economy (1.8.0)</div>
      <div class="sub2" style="margin-top:2px;">
        Total perform-layer tool calls across the run. {tool_calls_per_task_avg:.1f} calls/task average;
        {tool_waste_count}/{n_tasks} tasks flagged for redundancy. CAT's value-add is
        <em>fewer calls per pass</em>, not just pass rate.
      </div>
      {tool_economy_html}
    </div>
  </div>

  <h2 style="font-size: 16px; margin-bottom: 8px;">Per-task</h2>
  <table id="tasks">
    <thead>
      <tr>
        <th onclick="sortBy(0)">Task</th>
        <th onclick="sortBy(1)">Verdict</th>
        <th onclick="sortBy(2)">Iters</th>
        <th onclick="sortBy(3)">Tokens</th>
        <th onclick="sortBy(4)">Wall</th>
        <th onclick="sortBy(5)">Plan</th>
        <th>Primary</th>
        <th>Rationale</th>
      </tr>
    </thead>
    <tbody>
      {task_rows}
    </tbody>
  </table>

  <div class="footer">
    Generated {generated} · CAT v1.5 admission-gate · attribution mode: {attr_mode}
  </div>

<script>
function sortBy(col) {{
  const tbody = document.querySelector('#tasks tbody');
  const rows = Array.from(tbody.querySelectorAll('tr.row'));
  const dir = tbody.dataset.dir === 'asc' ? 'desc' : 'asc';
  tbody.dataset.dir = dir;
  rows.sort((a, b) => {{
    const av = a.children[col].dataset.sortVal || a.children[col].textContent;
    const bv = b.children[col].dataset.sortVal || b.children[col].textContent;
    const an = parseFloat(av), bn = parseFloat(bv);
    let cmp;
    if (!isNaN(an) && !isNaN(bn)) cmp = an - bn;
    else cmp = av.localeCompare(bv);
    return dir === 'asc' ? cmp : -cmp;
  }});
  // Reattach rows with their detail siblings
  rows.forEach(r => {{
    const detail = r.nextElementSibling && r.nextElementSibling.classList.contains('detail')
      ? r.nextElementSibling : null;
    tbody.appendChild(r);
    if (detail) tbody.appendChild(detail);
  }});
}}

document.querySelectorAll('tr.expandable').forEach(r => {{
  r.addEventListener('click', () => {{
    const next = r.nextElementSibling;
    if (next && next.classList.contains('detail')) {{
      next.style.display = next.style.display === 'none' ? '' : 'none';
    }}
  }});
}});
// Start collapsed
document.querySelectorAll('tr.detail').forEach(r => r.style.display = 'none');
</script>
</body>
</html>
"""


def render_html(report: RunReport) -> str:
    pass_pct = round(100 * report.pass_rate)
    pass_class = "pass" if pass_pct >= 50 else ("susp" if pass_pct >= 25 else "fail")

    wall_total = report.wall_s_total
    wall_h = int(wall_total // 3600)
    wall_m = int((wall_total % 3600) // 60)
    wall_mean_min = (wall_total / 60 / max(report.n_tasks, 1))

    tokens_total = report.tokens_in_total + report.tokens_out_total
    fmt = lambda n: f"{n/1_000_000:.2f}M" if n >= 1_000_000 else f"{n/1000:.1f}K"

    # Failure breakdown bars
    fbd_rows = []
    total = sum(report.failure_breakdown.values()) or 1
    for mode, n in sorted(report.failure_breakdown.items(), key=lambda x: -x[1]):
        pct = round(100 * n / total)
        fbd_rows.append(
            f'<div style="margin-top:6px;font-size:12px;display:flex;justify-content:space-between;">'
            f'<span>{mode}</span><span>{n} ({pct}%)</span></div>'
            f'<div class="bar"><div class="bar-fill" style="width:{pct}%;'
            + (f'background:#0a7c2f;' if mode == "PASS" else f'background:#c0392b;') +
            f'"></div></div>'
        )
    failure_bd_html = "".join(fbd_rows) or '<div class="sub2">(none)</div>'

    # Iteration distribution
    def fmt_dist(d):
        if not d.get("n"):
            return "<div class=\"sub2\">(no data)</div>"
        return (
            f'<div style="font-size:12px;margin-top:4px;">'
            f'n={d["n"]} · p50={d["p50"]} · p75={d["p75"]} · p90={d["p90"]} · max={d["max"]}</div>'
        )
    iter_dist_html = (
        '<div style="font-size:11px;margin-top:6px;font-weight:600;">PASS</div>'
        + fmt_dist(report.iter_dist_pass)
        + '<div style="font-size:11px;margin-top:8px;font-weight:600;">FAIL</div>'
        + fmt_dist(report.iter_dist_fail)
    )

    # Role share bars
    role_rows = []
    for role, share in sorted(report.role_token_share.items(), key=lambda x: -x[1]):
        pct = round(100 * share)
        role_rows.append(
            f'<div style="margin-top:6px;font-size:12px;display:flex;justify-content:space-between;">'
            f'<span>{role}</span><span>{pct}%</span></div>'
            f'<div class="bar"><div class="bar-fill" style="width:{pct}%;"></div></div>'
        )
    role_share_html = "".join(role_rows) or '<div class="sub2">(no role data)</div>'

    # Primary attribution
    attr_rows = []
    total_attr = sum(report.primary_attribution_counts.values()) or 1
    for layer, n in sorted(report.primary_attribution_counts.items(), key=lambda x: -x[1]):
        pct = round(100 * n / total_attr)
        attr_rows.append(
            f'<div style="margin-top:6px;font-size:12px;display:flex;justify-content:space-between;">'
            f'<span>{layer}</span><span>{n} ({pct}%)</span></div>'
            f'<div class="bar"><div class="bar-fill" style="width:{pct}%;"></div></div>'
        )
    attribution_html = "".join(attr_rows) or '<div class="sub2">(no attribution data)</div>'

    # Cost by CAT layer table (1.6.5)
    layer_order = ("represent", "stipulate", "activate", "perform", "verify")
    layer_swatch = {
        "represent": "#93c5fd",
        "stipulate": "#86efac",
        "activate":  "#fcd34d",
        "perform":   "#fdba74",
        "verify":    "#f87171",
    }
    cbl = report.cost_by_layer or {}
    # Find totals for share-of bars
    total_tokens_layer = sum(
        (cbl.get(l, {}).get("tokens_in", 0) + cbl.get(l, {}).get("tokens_out", 0))
        for l in layer_order
    ) or 1
    total_wall_layer = sum(cbl.get(l, {}).get("wall_s", 0.0) for l in layer_order) or 1.0
    layer_rows = [
        '<table style="width:100%;border:0;margin-top:8px;font-size:11px;">'
        '<tr style="background:#f5f5f5;">'
        '<th style="text-align:left;padding:6px;">layer</th>'
        '<th style="text-align:right;padding:6px;">LLM calls</th>'
        '<th style="text-align:right;padding:6px;">tool calls</th>'
        '<th style="text-align:right;padding:6px;">tokens (in+out)</th>'
        '<th style="text-align:left;padding:6px;width:25%;">token share</th>'
        '<th style="text-align:right;padding:6px;">wall</th>'
        '<th style="text-align:left;padding:6px;width:25%;">wall share</th>'
        '<th style="text-align:right;padding:6px;">USD</th>'
        '</tr>'
    ]
    for layer in layer_order:
        d = cbl.get(layer, {})
        tokens = d.get("tokens_in", 0) + d.get("tokens_out", 0)
        wall = d.get("wall_s", 0.0)
        tok_pct = round(100 * tokens / total_tokens_layer) if tokens else 0
        wall_pct = round(100 * wall / total_wall_layer) if wall else 0
        layer_rows.append(
            f'<tr>'
            f'<td style="padding:6px;"><span style="display:inline-block;width:10px;height:10px;'
            f'background:{layer_swatch[layer]};border-radius:2px;margin-right:6px;vertical-align:middle;'
            f'border:1px solid #888;"></span><b>{layer}</b></td>'
            f'<td style="text-align:right;padding:6px;">{d.get("llm_calls",0)}</td>'
            f'<td style="text-align:right;padding:6px;">{d.get("tool_calls",0)}</td>'
            f'<td style="text-align:right;padding:6px;">{tokens:,}</td>'
            f'<td style="padding:6px;">'
            f'<div class="bar"><div class="bar-fill" style="width:{tok_pct}%;background:{layer_swatch[layer]};"></div></div>'
            f'<span style="color:#888;font-size:10px;">{tok_pct}%</span></td>'
            f'<td style="text-align:right;padding:6px;">{wall:.1f}s</td>'
            f'<td style="padding:6px;">'
            f'<div class="bar"><div class="bar-fill" style="width:{wall_pct}%;background:{layer_swatch[layer]};"></div></div>'
            f'<span style="color:#888;font-size:10px;">{wall_pct}%</span></td>'
            f'<td style="text-align:right;padding:6px;">${d.get("usd",0):.4f}</td>'
            f'</tr>'
        )
    layer_rows.append("</table>")
    layer_cost_html = "".join(layer_rows)

    # 1.6.5.1: limitation_kind + improvement_vector histograms (FAIL/SUSPENDED only)
    lim_counts: dict[str, int] = {}
    imp_counts: dict[str, int] = {}
    for t in report.tasks:
        if t.verdict == "PASS" or not t.attribution:
            continue
        lk = (t.attribution.limitation_kind or "").strip()
        iv = (t.attribution.improvement_vector or "").strip()
        if lk:
            lim_counts[lk] = lim_counts.get(lk, 0) + 1
        if iv:
            imp_counts[iv] = imp_counts.get(iv, 0) + 1

    def _hist_html(counts: dict, color: str = "#fbbf24") -> str:
        if not counts:
            return '<div class="sub2" style="margin-top:8px;">(no FAILs in this run)</div>'
        total = sum(counts.values()) or 1
        rows = []
        for k, n in sorted(counts.items(), key=lambda x: -x[1]):
            pct = round(100 * n / total)
            rows.append(
                f'<div style="margin-top:6px;font-size:12px;display:flex;justify-content:space-between;">'
                f'<span>{_html_escape(k)}</span><span>{n} ({pct}%)</span></div>'
                f'<div class="bar"><div class="bar-fill" style="width:{pct}%;background:{color};"></div></div>'
            )
        return "".join(rows)

    limitation_kind_html = _hist_html(lim_counts, color="#f87171")
    improvement_vector_html = _hist_html(imp_counts, color="#3b82f6")

    # 1.8.0 — tool economy histogram + waste list
    tu = report.tool_usage_total or {}
    if not tu:
        tool_economy_html = '<div class="sub2" style="margin-top:8px;">(no perform-layer tool calls recorded)</div>'
    else:
        total_calls = sum(tu.values()) or 1
        # Sort by usage descending
        sorted_tu = sorted(tu.items(), key=lambda x: -x[1])
        bar_rows = []
        for name, n in sorted_tu:
            pct = round(100 * n / total_calls)
            bar_rows.append(
                f'<div style="margin-top:6px;font-size:12px;display:flex;justify-content:space-between;">'
                f'<span><code>{_html_escape(name)}</code></span><span>{n} ({pct}%)</span></div>'
                f'<div class="bar"><div class="bar-fill" style="width:{pct}%;background:#34d399;"></div></div>'
            )
        # Waste flags across all tasks (cap at 15 entries)
        waste_lines: list[str] = []
        for t in report.tasks:
            for f in (t.tool_waste_flags or []):
                waste_lines.append(f"<li><b>{_short_tid(t.task_id)}</b>: {_html_escape(f)}</li>")
        waste_html = ""
        if waste_lines:
            waste_html = (
                '<div style="margin-top:14px;font-size:12px;">'
                '<b>Redundancy flags (heuristic):</b>'
                f'<ul style="margin:4px 0 0 18px;color:#555;">{"".join(waste_lines[:15])}</ul>'
                f'{"<div style=\"color:#888;\">…and " + str(len(waste_lines)-15) + " more</div>" if len(waste_lines) > 15 else ""}'
                '</div>'
            )
        tool_economy_html = "".join(bar_rows) + waste_html

    # Task rows
    attr_modes_used = set()
    task_rows_html = []
    for t in report.tasks:
        verdict_class = {"PASS": "pass", "FAIL": "fail", "SUSPENDED": "susp",
                         "INFRA_ERROR": "infra"}.get(t.verdict, "infra")
        primary = t.attribution.primary if t.attribution else "-"
        rationale = t.attribution.rationale if t.attribution else "-"
        if t.attribution:
            attr_modes_used.add(t.attribution.method)
        plan_str = f"{t.plan_steps_completed}/{t.plan_steps_total}" if t.plan_steps_total else "-"
        # Main row
        task_rows_html.append(
            f'<tr class="row expandable">'
            f'<td>{_short_tid(t.task_id)}</td>'
            f'<td class="{verdict_class}">{t.verdict}</td>'
            f'<td data-sort-val="{t.iters}">{t.iters}</td>'
            f'<td data-sort-val="{t.tokens_in + t.tokens_out}">{fmt(t.tokens_in + t.tokens_out)}</td>'
            f'<td data-sort-val="{t.wall_s}">{t.wall_s/60:.1f} min</td>'
            f'<td>{plan_str}</td>'
            f'<td>{primary}</td>'
            f'<td style="max-width:380px;font-size:11px;color:#555;">{_html_escape(rationale[:200])}</td>'
            f'</tr>'
        )
        # Detail row
        dag_block = (
            f'<details style="margin-top:12px;"><summary>DAG (structure)</summary>'
            f'<div style="margin-top:8px;overflow-x:auto;">{t.dag_svg}</div></details>'
            if t.dag_svg else ""
        )
        trace_block = (
            f'<details style="margin-top:12px;"><summary>Reasoning trace (content)</summary>'
            f'<div style="margin-top:8px;max-height:600px;overflow-y:auto;">{t.trace_html}</div>'
            f'</details>'
            if t.trace_html else ""
        )
        # 1.8.0 — per-task tool usage block (small inline summary)
        tu_block = ""
        if t.tool_usage:
            tu_items = sorted(t.tool_usage.items(), key=lambda x: -x[1])
            tu_html = " ".join(
                f'<code style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:3px;'
                f'padding:1px 5px;margin-right:3px;font-size:11px;">{_html_escape(n)} ×{c}</code>'
                for n, c in tu_items
            )
            wf_html = ""
            if t.tool_waste_flags:
                wf_html = (
                    '<div style="margin-top:6px;font-size:11px;color:#b45309;">'
                    + " ⚠ ".join(_html_escape(f) for f in t.tool_waste_flags)
                    + '</div>'
                )
            tu_block = (
                f'<div style="margin-top:10px;font-size:12px;">'
                f'<b>Tools used (perform layer):</b><br>{tu_html}{wf_html}'
                f'</div>'
            )

        if t.attribution:
            layer_html = "".join(
                f'<div class="layer {l.status}'
                + (" primary" if l.layer == primary else "") +
                f'"><strong>{l.layer}</strong> · {l.status}<br>'
                f'<span style="color:#444;">{_html_escape(l.evidence)}</span></div>'
                for l in t.attribution.layers
            )
            # 1.6.5.1 Diagnosis block
            diag_block = _diagnosis_html(t.attribution)
            task_rows_html.append(
                f'<tr class="detail"><td colspan="8">'
                f'<div><strong>Final reason:</strong> {_html_escape(t.final_reason or "(none)")}</div>'
                f'<div class="layer-grid">{layer_html}</div>'
                f'<div class="rationale">{_html_escape(t.attribution.rationale)}</div>'
                f'{diag_block}'
                f'{tu_block}'
                f'<div class="meta">attribution.method = {t.attribution.method}'
                f' · failure_mode = {t.failure_mode}'
                f' · USD ${t.usd:.5f}</div>'
                f'{trace_block}'
                f'{dag_block}'
                f'</td></tr>'
            )
        else:
            task_rows_html.append(
                f'<tr class="detail"><td colspan="8">'
                f'<div><strong>Final reason:</strong> {_html_escape(t.final_reason or "(none)")}</div>'
                f'{tu_block}'
                f'<div class="meta">no agent trace available (likely infra failure before agent ran)</div>'
                f'{trace_block}'
                f'{dag_block}'
                f'</td></tr>'
            )

    return _HTML_TEMPLATE.format(
        run_id=_html_escape(report.run_id),
        agent=_html_escape(report.agent),
        model=_html_escape(report.model or "—"),
        started=_html_escape(report.started_at or "?"),
        ended=_html_escape(report.ended_at or "?"),
        pass_class=pass_class,
        n_pass=report.n_pass,
        n_tasks=report.n_tasks,
        pass_pct=pass_pct,
        n_infra=report.n_infra_error,
        wall_h=wall_h,
        wall_m=wall_m,
        wall_mean_min=wall_mean_min,
        tokens_total_h=fmt(tokens_total),
        tokens_in_h=fmt(report.tokens_in_total),
        tokens_out_h=fmt(report.tokens_out_total),
        usd_total=report.usd_total,
        usd_per_task=(report.usd_total / max(report.n_tasks, 1)),
        failure_bd_html=failure_bd_html,
        iter_dist_html=iter_dist_html,
        role_share_html=role_share_html,
        attribution_html=attribution_html,
        layer_cost_html=layer_cost_html,
        limitation_kind_html=limitation_kind_html,
        improvement_vector_html=improvement_vector_html,
        tool_economy_html=tool_economy_html,
        tool_calls_per_task_avg=report.tool_calls_per_task_avg,
        tool_waste_count=report.tool_waste_count,
        task_rows="".join(task_rows_html),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        attr_mode="+".join(sorted(attr_modes_used)) or "none",
    )


def _diagnosis_html(a: Attribution) -> str:
    """Per-task Diagnosis card (1.6.5.1). Empty for PASS tasks unless narrative exists."""
    if not a:
        return ""
    # PASS tasks with no narrative → skip
    if a.outcome == "PASS" and not a.narrative:
        return ""
    conf = a.confidence or "low"
    conf_color = {"high": "#0a7c2f", "medium": "#d97706", "low": "#888"}.get(conf, "#888")
    rows = []
    if a.limitation_kind and a.limitation_kind != "none":
        rows.append(
            f'<div style="margin-top:4px;font-size:12px;">'
            f'<b>Limitation kind:</b> <code>{_html_escape(a.limitation_kind)}</code>'
            + (f'  <span style="color:#888;">(secondary: <code>{_html_escape(a.limitation_secondary)}</code>)</span>'
               if a.limitation_secondary else "")
            + '</div>'
        )
    if a.improvement_vector and a.improvement_vector != "none":
        rows.append(
            f'<div style="margin-top:2px;font-size:12px;">'
            f'<b>Improvement vector:</b> <code>{_html_escape(a.improvement_vector)}</code>'
            + (f' — {_html_escape(a.improvement_specifics)}' if a.improvement_specifics else "")
            + '</div>'
        )
    if a.narrative:
        rows.append(
            f'<div style="margin-top:8px;padding:8px;background:#fffbeb;'
            f'border-left:3px solid #fbbf24;border-radius:4px;font-size:12px;'
            f'line-height:1.5;color:#444;">{_html_escape(a.narrative)}</div>'
        )
    if not rows:
        return ""
    return (
        f'<div style="margin-top:12px;padding:10px 12px;background:#f9fafb;'
        f'border:1px solid #e5e5e5;border-radius:4px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
        f'<span style="font-weight:600;font-size:13px;">Diagnosis</span>'
        f'<span style="font-size:11px;color:{conf_color};">confidence: {conf}</span>'
        f'</div>'
        + "".join(rows) +
        '</div>'
    )


def _short_tid(tid: str) -> str:
    parts = tid.split("__")
    return parts[-1] if len(parts) == 2 else tid


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


# ── CLI ────────────────────────────────────────────────────────────────────


def _attribution_to_dict(a: Optional[Attribution]) -> Optional[dict]:
    if a is None:
        return None
    return {
        "task_id": a.task_id,
        "outcome": a.outcome,
        "layers": [asdict(l) for l in a.layers],
        "primary": a.primary,
        "rationale": a.rationale,
        "method": a.method,
        # 1.6.5.1 diagnostic taxonomy fields
        "limitation_kind": a.limitation_kind,
        "limitation_secondary": a.limitation_secondary,
        "improvement_vector": a.improvement_vector,
        "improvement_specifics": a.improvement_specifics,
        "narrative": a.narrative,
        "confidence": a.confidence,
    }


def write_report(run_dir: Path, report: RunReport) -> None:
    # JSON: machine-readable; strip the inline SVG (it's huge and is duplicated
    # in dag.json alongside each trace).
    report_dict = asdict(report)
    for i, t in enumerate(report.tasks):
        report_dict["tasks"][i]["attribution"] = _attribution_to_dict(t.attribution)
        report_dict["tasks"][i].pop("dag_svg", None)
        report_dict["tasks"][i].pop("trace_html", None)
    (run_dir / "_kpi_report.json").write_text(json.dumps(report_dict, indent=2))
    # HTML: human-readable (DAG SVG embedded inline)
    (run_dir / "_kpi_report.html").write_text(render_html(report))


def main():
    ap = argparse.ArgumentParser(description="Generate KPI report or cross-run INDEX")
    ap.add_argument("path", type=Path, nargs="?",
                    help="path to a run dir, OR with --index, path to the runs/ root")
    ap.add_argument("--index", action="store_true",
                    help="regenerate INDEX.html cross-run dashboard at the given runs/ root")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip LLM attribution; use heuristics only")
    ap.add_argument("--model", default="openrouter/deepseek/deepseek-v4-flash")
    args = ap.parse_args()

    if args.index:
        runs_root = args.path or Path("data/runs")
        if not runs_root.exists():
            print(f"(no runs root at {runs_root})")
            return
        index_html = render_index_html(runs_root)
        index_path = runs_root / "INDEX.html"
        index_path.write_text(index_html)
        print(f"Wrote {index_path}")
        return

    if not args.path:
        ap.error("path required (or use --index)")
    report = summarize_run(args.path, use_llm=not args.no_llm, model=args.model)
    write_report(args.path, report)
    print(f"\n=== {report.run_id} ===")
    print(f"  {report.n_pass}/{report.n_tasks} resolved "
          f"({100*report.pass_rate:.1f}%)  "
          f"infra_errors={report.n_infra_error}")
    print(f"  wall: {report.wall_s_total/60:.1f} min  "
          f"tokens: {(report.tokens_in_total+report.tokens_out_total)/1000:.1f}K  "
          f"usd: ${report.usd_total:.3f}")
    print(f"  failure_breakdown: {report.failure_breakdown}")
    print(f"  primary_attribution: {report.primary_attribution_counts}")
    print(f"\nReport written:")
    print(f"  {args.path}/_kpi_report.html")
    print(f"  {args.path}/_kpi_report.json")


# ── INDEX rendering ────────────────────────────────────────────────────────


def _discover_runs(runs_root: Path) -> list[dict]:
    """Walk `data/runs/<bench>/<date>/<runid>/` and return one row per run.

    Looks specifically two levels deep: `<bench>/<date>/<run>` — that's where
    `manifest.json` or `results.json` lives. Skips dirs that look like trial
    subdirectories (pattern `<task>.<n>-of-<m>.<runid>`).
    """
    import re
    trial_re = re.compile(r"\.\d+-of-\d+\.")
    rows: list[dict] = []
    if not runs_root.exists():
        return rows
    for bench_dir in sorted(runs_root.iterdir()):
        if not bench_dir.is_dir() or bench_dir.name.startswith("_") or bench_dir.name.startswith("."):
            continue
        # <date>/<run> level — follow symlinks (legacy runs are linked here)
        for date_dir in sorted(bench_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for run_dir in sorted(date_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                if trial_re.search(run_dir.name):
                    continue  # leftover trial-level link
                results_path = run_dir / "results.json"
                if not results_path.exists():
                    continue
                row = _row_from_run(run_dir, bench=bench_dir.name)
                if row:
                    rows.append(row)
    return rows


def _row_from_run(run_dir: Path, bench: str) -> Optional[dict]:
    manifest_path = run_dir / "manifest.json"
    results_path = run_dir / "results.json"
    report_path = run_dir / "_kpi_report.html"
    if not results_path.exists():
        return None
    try:
        results = json.loads(results_path.read_text())
    except Exception:
        return None
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            pass
    n = len(results.get("results", []))
    n_res = sum(1 for t in results.get("results", []) if t.get("is_resolved"))
    return {
        "bench": bench,
        "run_id": manifest.get("run_id") or run_dir.name,
        "path": run_dir,
        "started_at": manifest.get("started_at") or "",
        "ended_at": manifest.get("ended_at") or "",
        "n_tasks": n,
        "n_resolved": n_res,
        "pass_rate": (n_res / n) if n else 0.0,
        "git_sha": manifest.get("git_sha", ""),
        "agent": manifest.get("agent_import_path") or manifest.get("agent_name", ""),
        "report_url": str(report_path.relative_to(run_dir.parent.parent.parent)) if report_path.exists() else None,
        "has_report": report_path.exists(),
    }


def render_index_html(runs_root: Path) -> str:
    rows = _discover_runs(runs_root)
    rows.sort(key=lambda r: (r["bench"], r["started_at"] or "0", r["run_id"]), reverse=True)
    by_bench: dict[str, list[dict]] = {}
    for r in rows:
        by_bench.setdefault(r["bench"], []).append(r)

    parts: list[str] = []
    parts.append("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CAT runs INDEX</title>
<style>
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         max-width: 1280px; margin: 20px auto; padding: 0 16px;
         background: #fafafa; color: #1a1a1a; }
  h1 { font-size: 20px; margin-bottom: 8px; }
  h2 { font-size: 16px; margin-top: 28px; padding-bottom: 4px;
       border-bottom: 1px solid #ddd; }
  table { width: 100%; border-collapse: collapse; background: white;
          border: 1px solid #e5e5e5; border-radius: 4px;
          font-size: 12px; margin-top: 8px; }
  th { background: #f5f5f5; padding: 6px 10px; text-align: left;
       font-weight: 600; border-bottom: 1px solid #e5e5e5; }
  td { padding: 6px 10px; border-bottom: 1px solid #f0f0f0; }
  td.r { text-align: right; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .pass { color: #0a7c2f; font-weight: 600; }
  .fail { color: #c0392b; font-weight: 600; }
  .meta { color: #888; font-size: 11px; }
</style></head><body>""")
    parts.append(f"<h1>CAT runs INDEX</h1>")
    parts.append(f'<div class="meta">{len(rows)} runs · regenerated '
                 f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")}</div>')

    if not rows:
        parts.append("<p>(no runs found)</p>")
        parts.append("</body></html>")
        return "\n".join(parts)

    for bench in sorted(by_bench.keys()):
        bench_rows = by_bench[bench]
        parts.append(f"<h2>{bench} ({len(bench_rows)} runs)</h2>")
        parts.append("<table>")
        parts.append(
            "<tr><th>started</th><th>run_id</th>"
            "<th class='r'>tasks</th><th class='r'>resolved</th>"
            "<th class='r'>rate</th><th>git</th><th>report</th></tr>"
        )
        for r in bench_rows:
            rate = r["pass_rate"]
            rate_class = "pass" if rate >= 0.5 else ("fail" if rate < 0.25 else "")
            rate_str = f"{rate*100:.0f}%" if r["n_tasks"] else "—"
            started = (r["started_at"] or "—")[:19].replace("T", " ")
            report_link = (
                f'<a href="{r["bench"]}/' +
                str(r["path"].relative_to(runs_root)).replace(bench + "/", "", 1) +
                '/_kpi_report.html">view</a>'
                if r["has_report"] else "<span class='meta'>—</span>"
            )
            parts.append(
                f"<tr>"
                f"<td class='meta'>{started}</td>"
                f"<td>{r['run_id']}</td>"
                f"<td class='r'>{r['n_tasks']}</td>"
                f"<td class='r'>{r['n_resolved']}</td>"
                f"<td class='r {rate_class}'>{rate_str}</td>"
                f"<td class='meta'>{r['git_sha'][:8]}</td>"
                f"<td>{report_link}</td>"
                f"</tr>"
            )
        parts.append("</table>")

    parts.append("</body></html>")
    return "\n".join(parts)


if __name__ == "__main__":
    main()
