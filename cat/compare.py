"""Comparison harness: run N reps × M agents × K tasks, roll up into one table.

    python -m cat.compare \\
        --tasks hello-file multi-step-refactor \\
        --agents cat react \\
        --reps 3

Drops per-trial outcome rows in data/runs/, and a summary CSV + Markdown into
data/runs/_summary/.
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cat.runner import AGENTS, run_one
from cat.tasks import Outcome


def _stat(values: list[float]) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (values[0], 0.0)
    return (statistics.mean(values), statistics.stdev(values))


def run_matrix(
    tasks: list[str],
    agents: list[str],
    reps: int,
    model: str,
    out_dir: Path,
    max_iterations: int = 40,
) -> dict[str, Any]:
    """Run reps × agents × tasks; return aggregated rollup."""
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    total = len(tasks) * len(agents) * reps
    i = 0
    for task in tasks:
        for agent in agents:
            for rep in range(1, reps + 1):
                i += 1
                print(f"[{i}/{total}] task={task} agent={agent} rep={rep} ...", flush=True)
                try:
                    outcome = run_one(
                        task_id=task,
                        agent_name=agent,
                        model=model,
                        out_dir=out_dir,
                        max_iterations=max_iterations,
                    )
                    rows.append({
                        "task": task, "agent": agent, "rep": rep,
                        "verdict": outcome.verdict, "score": outcome.score,
                        "wall_s": outcome.wall_s, "usd": outcome.usd,
                        "tool_calls": outcome.tool_calls, "n_loops": outcome.n_loops,
                        "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
                        "iterations": outcome.meta.get("iterations", 0),
                        "error": outcome.error,
                        "run_id": outcome.run_id,
                    })
                    print(f"    → {outcome.verdict} loops={outcome.n_loops} tools={outcome.tool_calls} "
                          f"tokens={outcome.tokens_in}/{outcome.tokens_out} usd=${outcome.usd:.5f} "
                          f"wall={outcome.wall_s:.1f}s", flush=True)
                except Exception as exc:  # noqa: BLE001
                    rows.append({
                        "task": task, "agent": agent, "rep": rep,
                        "verdict": "error", "score": 0.0,
                        "wall_s": 0.0, "usd": 0.0,
                        "tool_calls": 0, "n_loops": 0,
                        "tokens_in": 0, "tokens_out": 0,
                        "iterations": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                        "run_id": None,
                    })
                    print(f"    → ERROR {type(exc).__name__}: {exc}", flush=True)

    # Aggregate per (task, agent)
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["task"], r["agent"])].append(r)
    for (task, agent), trials in by_key.items():
        solved = sum(1 for t in trials if t["verdict"] == "solved")
        tools_mean, tools_sd = _stat([t["tool_calls"] for t in trials])
        loops_mean, loops_sd = _stat([t["n_loops"] for t in trials])
        toks_in_mean, toks_in_sd = _stat([t["tokens_in"] for t in trials])
        toks_out_mean, toks_out_sd = _stat([t["tokens_out"] for t in trials])
        usd_mean, usd_sd = _stat([t["usd"] for t in trials])
        wall_mean, wall_sd = _stat([t["wall_s"] for t in trials])
        agg[(task, agent)] = {
            "solved": solved,
            "total": len(trials),
            "solve_rate": solved / len(trials) if trials else 0.0,
            "tool_calls_mean": round(tools_mean, 2),
            "tool_calls_sd": round(tools_sd, 2),
            "n_loops_mean": round(loops_mean, 2),
            "n_loops_sd": round(loops_sd, 2),
            "tokens_in_mean": round(toks_in_mean, 1),
            "tokens_in_sd": round(toks_in_sd, 1),
            "tokens_out_mean": round(toks_out_mean, 1),
            "tokens_out_sd": round(toks_out_sd, 1),
            "tokens_total_mean": round(toks_in_mean + toks_out_mean, 1),
            "usd_mean": round(usd_mean, 5),
            "usd_sd": round(usd_sd, 5),
            "wall_s_mean": round(wall_mean, 1),
            "wall_s_sd": round(wall_sd, 1),
        }

    return {
        "tasks": tasks,
        "agents": agents,
        "reps": reps,
        "model": model,
        "n_trials": len(rows),
        "wall_s_total": round(time.time() - t0, 1),
        "rows": rows,
        "aggregate": [
            {"task": k[0], "agent": k[1], **v} for k, v in agg.items()
        ],
    }


def render_markdown(summary: dict[str, Any]) -> str:
    """Render the comparison summary as a Markdown report."""
    lines: list[str] = []
    lines.append(f"# CAT vs ReAct — Comparison Report")
    lines.append("")
    lines.append(f"- model: `{summary['model']}`")
    lines.append(f"- tasks: {', '.join(summary['tasks'])}")
    lines.append(f"- agents: {', '.join(summary['agents'])}")
    lines.append(f"- reps per cell: {summary['reps']}")
    lines.append(f"- trials: {summary['n_trials']}")
    lines.append(f"- wall_s total: {summary['wall_s_total']}")
    lines.append("")

    lines.append("## Per-cell aggregate")
    lines.append("")
    lines.append("| task | agent | solve | loops (μ±σ) | tools (μ±σ) | tokens in | tokens out | $ μ | wall_s μ |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in sorted(summary["aggregate"], key=lambda r: (r["task"], r["agent"])):
        lines.append(
            f"| {row['task']} | {row['agent']} | {row['solved']}/{row['total']} "
            f"({row['solve_rate']:.0%}) | "
            f"{row['n_loops_mean']}±{row['n_loops_sd']} | "
            f"{row['tool_calls_mean']}±{row['tool_calls_sd']} | "
            f"{row['tokens_in_mean']} | {row['tokens_out_mean']} | "
            f"${row['usd_mean']:.5f} | {row['wall_s_mean']:.1f} |"
        )
    lines.append("")

    lines.append("## Per-trial detail")
    lines.append("")
    lines.append("| task | agent | rep | verdict | loops | tools | tokens in/out | $ | wall_s | error |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in summary["rows"]:
        err = (r.get("error") or "")[:40].replace("|", "/")
        lines.append(
            f"| {r['task']} | {r['agent']} | {r['rep']} | {r['verdict']} | "
            f"{r['n_loops']} | {r['tool_calls']} | "
            f"{r['tokens_in']}/{r['tokens_out']} | ${r['usd']:.5f} | "
            f"{r['wall_s']:.1f} | {err} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(prog="cat.compare")
    p.add_argument("--tasks", nargs="+", default=["hello-file", "multi-step-refactor"])
    p.add_argument("--agents", nargs="+", default=list(AGENTS.keys()))
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--model", default="openrouter/deepseek/deepseek-v4-flash")
    p.add_argument("--out", default="data/runs")
    p.add_argument("--max-iterations", type=int, default=40)
    args = p.parse_args()

    out_dir = Path(args.out)
    summary = run_matrix(
        tasks=args.tasks,
        agents=args.agents,
        reps=args.reps,
        model=args.model,
        out_dir=out_dir,
        max_iterations=args.max_iterations,
    )

    sum_dir = out_dir / "_summary"
    sum_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    (sum_dir / f"{stamp}_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    md = render_markdown(summary)
    (sum_dir / f"{stamp}_summary.md").write_text(md, encoding="utf-8")

    print()
    print("─" * 60)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
