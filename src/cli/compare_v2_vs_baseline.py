#!/usr/bin/env python3
"""Compare a v2 swe-bench run against the v1.7 A.2 fixed baseline.

Usage: python3 scripts/compare_v2_vs_baseline.py <v2_run_dir>

Baseline (hardcoded for this comparison):
  data/runs/swebench/2026-05-30/13-37-39-swe10-v1-7-a2-fixed/

Outputs a markdown comparison table.
"""
import json
import sys
from pathlib import Path


BASELINE = Path("data/runs/swebench/2026-05-30/13-37-39-swe10-v1-7-a2-fixed")


def load_results(run_dir: Path) -> dict:
    rj = run_dir / "results.json"
    if not rj.exists():
        return {"results": []}
    return json.load(open(rj))


def summarize_task(task_id: str, results: list) -> dict:
    for r in results:
        if r.get("instance_id") == task_id or r.get("task_id") == task_id:
            return r
    return {}


def task_tokens(task_id: str, run_dir: Path) -> tuple[int, int]:
    """Find the JSONL trace for this task and sum llm_usage tokens."""
    candidates = list(run_dir.glob(f"**/{task_id}*/agent-logs/*.jsonl"))
    if not candidates:
        return 0, 0
    f = candidates[0]
    tin = tout = 0
    for line in open(f):
        try:
            ev = json.loads(line.strip())
        except Exception:
            continue
        if ev.get("kind") == "llm_usage":
            tin += int(ev.get("payload", {}).get("in", 0))
            tout += int(ev.get("payload", {}).get("out", 0))
    return tin, tout


def main():
    if len(sys.argv) < 2:
        print("usage: compare_v2_vs_baseline.py <v2_run_dir>")
        sys.exit(2)
    v2_dir = Path(sys.argv[1])
    if not v2_dir.exists():
        print(f"error: {v2_dir} does not exist")
        sys.exit(1)

    base = load_results(BASELINE)
    v2 = load_results(v2_dir)

    # Build task lookup
    base_by_task = {r.get("instance_id") or r.get("task_id"): r for r in base.get("results", [])}
    v2_by_task = {r.get("instance_id") or r.get("task_id"): r for r in v2.get("results", [])}

    all_tasks = sorted(set(base_by_task) | set(v2_by_task))

    # Headline
    base_pass = sum(1 for r in base.get("results", []) if r.get("is_resolved"))
    base_total = len(base.get("results", []))
    v2_pass = sum(1 for r in v2.get("results", []) if r.get("is_resolved"))
    v2_total = len(v2.get("results", []))

    print(f"# v2 vs v1.7 A.2 fixed — comparison\n")
    print(f"- **Baseline** (v1.7 A.2 fixed): {base_pass}/{base_total} = {base_pass/max(base_total,1)*100:.0f}%")
    print(f"- **v2** (iteration_loop_redesign): {v2_pass}/{v2_total} = {v2_pass/max(v2_total,1)*100:.0f}%")
    print(f"- **Delta**: {v2_pass - base_pass:+d}")
    print()

    # Per-task table
    print("| task | baseline | v2 | base tokens | v2 tokens | Δ |")
    print("|---|---|---|---|---|---|")
    base_total_tin = base_total_tout = 0
    v2_total_tin = v2_total_tout = 0
    for t in all_tasks:
        b = base_by_task.get(t, {})
        v = v2_by_task.get(t, {})
        b_pass = "✓" if b.get("is_resolved") else "✗" if b else "—"
        v_pass = "✓" if v.get("is_resolved") else "✗" if v else "—"
        b_tin, b_tout = task_tokens(t, BASELINE) if t in base_by_task else (0, 0)
        v_tin, v_tout = task_tokens(t, v2_dir) if t in v2_by_task else (0, 0)
        b_total = b_tin + b_tout
        v_total = v_tin + v_tout
        base_total_tin += b_tin; base_total_tout += b_tout
        v2_total_tin += v_tin; v2_total_tout += v_tout
        delta = ""
        if b_total and v_total:
            d = v_total - b_total
            delta = f"{d:+,} ({100*d/b_total:+.0f}%)"
        print(f"| {t[:35]} | {b_pass} | {v_pass} | {b_total:,} | {v_total:,} | {delta} |")
    print()
    print(f"**Token totals**:")
    print(f"- baseline: in={base_total_tin:,}, out={base_total_tout:,}, total={base_total_tin+base_total_tout:,}")
    print(f"- v2: in={v2_total_tin:,}, out={v2_total_tout:,}, total={v2_total_tin+v2_total_tout:,}")
    print()

    # v2-specific stats: surprise events, tree ops, restip count
    print(f"## v2-specific telemetry\n")
    for t in all_tasks:
        if t not in v2_by_task:
            continue
        candidates = list(v2_dir.glob(f"**/{t}*/agent-logs/*.jsonl"))
        if not candidates:
            continue
        counts = {"surprise": 0, "stipulate_inloop": 0, "plan_commit": 0,
                  "plan_drop": 0, "plan_branch": 0, "plan_commit_new": 0,
                  "plan_refine": 0, "plan_merge": 0}
        for line in open(candidates[0]):
            try:
                ev = json.loads(line.strip())
            except Exception:
                continue
            k = ev.get("kind", "")
            if k in counts:
                counts[k] += 1
        print(f"- {t}: surprise={counts['surprise']}, restip={counts['stipulate_inloop']}, "
              f"commit={counts['plan_commit']}, drop={counts['plan_drop']}, "
              f"refine={counts['plan_refine']}, branch={counts['plan_branch']}, "
              f"new={counts['plan_commit_new']}, merge={counts['plan_merge']}")


if __name__ == "__main__":
    main()
