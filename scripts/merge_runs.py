"""Merge multiple tb runs result directories into a single synthesized "final" run.

Use case: a benchmark run was split across multiple attempts (e.g.,
swe10-v1.5-envelope + retry + retry2 because of infra contention). This script
picks the BEST verdict per task across all source runs and writes a merged
view, with task subdirectories symlinked to wherever the canonical verdict
came from.

Verdict priority (highest wins):
    PASS  >  real FAIL (mode=unset / agent_timeout / executor_quit)  >
    INFRA_ERROR (mode=unknown_agent_error)

Usage:
    python scripts/merge_runs.py \\
        --out data/runs/swebench/2026-05-27/_v1.5-final \\
        --bench swebench --run-id v1.5-final \\
        data/tb-runs/v1.5/swe10-v1.5-envelope \\
        data/tb-runs/v1.5/swe10-v1.5-envelope-retry \\
        data/tb-runs/v1.5/swe10-v1.5-envelope-retry2

After merge, run `python -m cat.run_report <out>` to generate the HTML.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _verdict_priority(t: dict) -> int:
    """Higher number = better verdict (pick the max across sources)."""
    if t.get("is_resolved"):
        return 3
    mode = t.get("failure_mode") or ""
    if mode == "unknown_agent_error":
        return 0  # infra failure — agent never ran
    return 1  # real agent failure (we have data on it)


def _is_real_attempt(t: dict) -> bool:
    """True if the agent actually got to run (vs infra-died before start)."""
    return _verdict_priority(t) >= 1


def _load_results(run_dir: Path) -> dict:
    rp = run_dir / "results.json"
    if not rp.exists():
        return {"results": []}
    return json.loads(rp.read_text())


def _trial_dir_for_task(run_dir: Path, trial_name: str, task_id: str) -> Path | None:
    """Find the task's trial directory inside a source run."""
    candidate = run_dir / task_id / trial_name
    if candidate.exists():
        return candidate
    # fallback: just match by task_id
    for sub in (run_dir / task_id).glob("*") if (run_dir / task_id).exists() else []:
        if sub.is_dir():
            return sub
    return None


def merge_runs(
    source_dirs: list[Path],
    out_dir: Path,
    bench: str,
    run_id: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Gather best entry per task_id across sources
    best: dict[str, tuple[int, dict, Path]] = {}  # task_id → (priority, trial_dict, source_run_dir)
    for src in source_dirs:
        src = Path(src).resolve()
        if not src.exists():
            print(f"warning: source not found: {src}")
            continue
        results = _load_results(src)
        for t in results.get("results", []):
            tid = t["task_id"]
            pri = _verdict_priority(t)
            prev = best.get(tid)
            if prev is None or pri > prev[0]:
                best[tid] = (pri, t, src)

    if not best:
        print("no tasks to merge")
        return

    # Synthesize merged results.json
    merged_results = []
    sources_used: dict[str, str] = {}
    for tid in sorted(best):
        _, t, src = best[tid]
        merged_results.append(t)
        sources_used[tid] = src.name

        # Symlink the task's trial directory into the merged dir
        trial_name = t.get("trial_name")
        if not trial_name:
            continue
        src_trial = _trial_dir_for_task(src, trial_name, tid)
        if src_trial is None or not src_trial.exists():
            continue
        link_parent = out_dir / tid
        link_parent.mkdir(parents=True, exist_ok=True)
        link_target = link_parent / trial_name
        if link_target.exists() or link_target.is_symlink():
            continue
        rel = Path(os.path.relpath(src_trial, link_target.parent))
        link_target.symlink_to(rel)

    n_total = len(merged_results)
    n_resolved = sum(1 for t in merged_results if t.get("is_resolved"))
    n_real_attempts = sum(1 for t in merged_results if _is_real_attempt(t))

    out_results = {
        "id": f"merged-{run_id}",
        "n_resolved": n_resolved,
        "n_unresolved": n_total - n_resolved,
        "accuracy": n_resolved / n_total if n_total else 0.0,
        "results": merged_results,
        "pass_at_k": {},
        "resolved_ids": [t["task_id"] for t in merged_results if t.get("is_resolved")],
        "unresolved_ids": [t["task_id"] for t in merged_results if not t.get("is_resolved")],
    }
    (out_dir / "results.json").write_text(json.dumps(out_results, indent=2))

    # Manifest
    manifest = {
        "bench": bench,
        "run_id": run_id,
        "started_at": _earliest_start(source_dirs),
        "ended_at": _latest_end(source_dirs),
        "merged_from": [str(Path(s).name) for s in source_dirs],
        "merge_strategy": "PASS > real-FAIL > INFRA_ERROR per task_id",
        "n_tasks": n_total,
        "n_resolved": n_resolved,
        "n_real_attempts": n_real_attempts,
        "pass_rate": n_resolved / n_total if n_total else 0.0,
        "pass_rate_real_attempts": n_resolved / n_real_attempts if n_real_attempts else 0.0,
        "sources_used_per_task": sources_used,
        "merge_emitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Minimal run_metadata.json so the report generator can find run_id
    (out_dir / "run_metadata.json").write_text(json.dumps({
        "run_id": run_id,
        "dataset_path": "(merged)",
        "agent_name": "cat-swe-merged",
    }, indent=2))

    print(f"Merged {len(source_dirs)} runs into {out_dir}")
    print(f"  tasks: {n_total}, resolved: {n_resolved}/{n_total} "
          f"({100*n_resolved/n_total:.0f}%)")
    print(f"  real attempts: {n_real_attempts} "
          f"({100*n_resolved/n_real_attempts:.0f}% pass on real attempts)"
          if n_real_attempts else "")
    print(f"\nSources used per task:")
    for tid, src in sources_used.items():
        verdict = "PASS" if any(t.get("task_id") == tid and t.get("is_resolved") for t in merged_results) else "FAIL"
        print(f"  {verdict:4s}  {tid:50s}  from  {src}")


def _earliest_start(dirs: list[Path]) -> str:
    starts = []
    for d in dirs:
        r = _load_results(Path(d))
        for t in r.get("results", []):
            if t.get("trial_started_at"):
                starts.append(t["trial_started_at"])
    return min(starts) if starts else ""


def _latest_end(dirs: list[Path]) -> str:
    ends = []
    for d in dirs:
        r = _load_results(Path(d))
        for t in r.get("results", []):
            if t.get("trial_ended_at"):
                ends.append(t["trial_ended_at"])
    return max(ends) if ends else ""


def main():
    ap = argparse.ArgumentParser(description="Merge multiple tb runs results into one final view")
    ap.add_argument("source_dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--bench", default="swebench")
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    merge_runs(args.source_dirs, args.out, args.bench, args.run_id)


if __name__ == "__main__":
    main()
