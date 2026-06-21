"""Minimal KPI report — reads results.json and emits a plain text + JSON summary.

Usage:
    python -m cat.run_report <run_dir> [--no-llm] [--index <dir>]

This is a lightweight replacement for the legacy cat.run_report that depended
on deleted modules (cat.attribution, cat.dag). Full HTML rendering can be
wired up later via tooling.reports.run_report when generate_report is
implemented.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_results(run_dir: Path) -> dict | None:
    p = run_dir / "results.json"
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def generate_report(run_dir: str, no_llm: bool = False) -> None:
    d = Path(run_dir)
    data = _load_results(d)
    if data is None:
        print(f"[run_report] no results.json found in {d}", file=sys.stderr)
        return

    results = data.get("results", [])
    n = len(results)
    resolved = sum(1 for r in results if r.get("is_resolved"))
    rate = resolved / n if n else 0.0

    summary = {
        "run_dir": str(d),
        "n_tasks": n,
        "n_resolved": resolved,
        "pass_rate": round(rate, 4),
        "tasks": [
            {"task_id": r.get("task_id", "?"), "resolved": r.get("is_resolved", False)}
            for r in results
        ],
    }

    out_json = d / "_kpi_report.json"
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n── KPI Report: {d.name} ──")
    print(f"  tasks:    {n}")
    print(f"  resolved: {resolved}")
    print(f"  pass_rate: {rate:.1%}")
    for r in results:
        mark = "✓" if r.get("is_resolved") else "✗"
        print(f"  {mark} {r.get('task_id', '?')}")
    print(f"\n  Written: {out_json}")


def _rebuild_index(index_dir: str) -> None:
    d = Path(index_dir)
    runs: list[dict] = []
    for manifest in sorted(d.rglob("manifest.json")):
        try:
            with open(manifest) as f:
                m = json.load(f)
            runs.append({
                "run_id": m.get("run_id"),
                "bench": m.get("bench"),
                "started_at": m.get("started_at"),
                "pass_rate": m.get("pass_rate"),
                "n_resolved": m.get("n_resolved"),
                "n_tasks": m.get("n_tasks"),
                "path": str(manifest.parent),
            })
        except Exception:
            continue

    index_json = d / "index.json"
    with open(index_json, "w") as f:
        json.dump(runs, f, indent=2)
    print(f"[run_report] index updated: {index_json} ({len(runs)} runs)")


def main() -> None:
    parser = argparse.ArgumentParser(description="CAT KPI report generator")
    parser.add_argument("run_dir", nargs="?", help="Run output directory")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM attribution")
    parser.add_argument("--index", metavar="DIR", help="Rebuild index from runs directory")
    args = parser.parse_args()

    if args.index:
        _rebuild_index(args.index)
        return

    if not args.run_dir:
        parser.print_help()
        sys.exit(1)

    generate_report(args.run_dir, no_llm=args.no_llm)


if __name__ == "__main__":
    main()
