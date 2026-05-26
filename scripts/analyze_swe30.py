"""Analyze SWE-bench 30-task head-to-head: CAT vs ReAct.

Usage:
    python scripts/analyze_swe30.py

Reads from:
    data/tb-runs/swe30/swe30-cat/results.json     (30 tasks, 1 attempt, CAT)
    data/tb-runs/swe30/swe30-react/results.json    (11 tasks, 1 attempt, ReAct partial)
    data/tb-runs/swe30/swe30-react-2/results.json  (19 tasks, 1 attempt, ReAct continuation)

Produces:
    - Per-task win/loss table
    - Token efficiency comparison
    - Pass@1 and Pass@2 (where available) summary
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
SWE30_DIR = ROOT / "data/tb-runs/swe30"

CAT_RESULTS = SWE30_DIR / "swe30-cat/results.json"
REACT1_RESULTS = SWE30_DIR / "swe30-react/results.json"
REACT2_RESULTS = SWE30_DIR / "swe30-react-2/results.json"

# SWE10 head-to-head from swe10 run (for token comparison)
SWE10_CAT_RESULTS = ROOT / "data/tb-runs/swe10/swe10/results.json"


def load(path: Path) -> dict:
    if not path.exists():
        return {"results": [], "n_resolved": 0, "n_unresolved": 0}
    with open(path) as f:
        return json.load(f)


def trial_map(results: dict) -> dict[str, dict]:
    """Map task_id → trial result (most recent if multiple)."""
    out: dict[str, dict] = {}
    for r in results.get("results", []):
        tid = r["task_id"]
        # prefer pass over fail when multiple trials exist
        if tid not in out or (not out[tid]["is_resolved"] and r["is_resolved"]):
            out[tid] = r
    return out


def merge_react(r1: dict, r2: dict) -> dict[str, dict]:
    """Merge two react result sets (r1 first 11, r2 remaining 19)."""
    merged = trial_map(r1)
    merged.update(trial_map(r2))
    return merged


def format_short_id(task_id: str) -> str:
    """Convert django__django-12708 → django-12708."""
    parts = task_id.split("__")
    return parts[-1] if len(parts) == 2 else task_id


def main() -> None:
    cat_raw = load(CAT_RESULTS)
    react1_raw = load(REACT1_RESULTS)
    react2_raw = load(REACT2_RESULTS)

    cat_trials = trial_map(cat_raw)
    react_trials = merge_react(react1_raw, react2_raw)

    all_tasks = sorted(set(cat_trials.keys()) | set(react_trials.keys()))
    react_covered = set(react_trials.keys())
    cat_covered = set(cat_trials.keys())

    print(f"SWE-bench 30-task analysis")
    print(f"  CAT tasks: {len(cat_covered)}")
    print(f"  ReAct tasks done: {len(react_covered)} (of {len(cat_covered)} total)")
    remaining = len(cat_covered) - len(react_covered)
    if remaining:
        print(f"  ReAct tasks remaining: {remaining}")
    print()

    # Per-task win/loss table (only for tasks both agents have completed)
    shared_tasks = sorted(cat_covered & react_covered)
    cat_wins, react_wins, both_win, neither_win = [], [], [], []

    print("## Per-task comparison (shared tasks)")
    print(f"{'Task':<40} {'CAT':>6} {'ReAct':>6} {'Winner'}")
    print("-" * 62)
    for tid in shared_tasks:
        c = cat_trials[tid]["is_resolved"]
        r = react_trials[tid]["is_resolved"]
        tag = "  both" if (c and r) else ("  CAT" if c else ("React" if r else "  none"))
        if c and r:
            both_win.append(tid)
        elif c:
            cat_wins.append(tid)
        elif r:
            react_wins.append(tid)
        else:
            neither_win.append(tid)
        print(f"  {format_short_id(tid):<38} {'✓' if c else '✗':>6} {'✓' if r else '✗':>6} {tag}")

    print()
    print(f"CAT wins only ({len(cat_wins)}): {[format_short_id(t) for t in cat_wins]}")
    print(f"ReAct wins only ({len(react_wins)}): {[format_short_id(t) for t in react_wins]}")
    print(f"Both solve ({len(both_win)}): {[format_short_id(t) for t in both_win]}")
    print(f"Neither ({len(neither_win)}): count={len(neither_win)}")

    cat_solved_shared = len(cat_wins) + len(both_win)
    react_solved_shared = len(react_wins) + len(both_win)
    print()
    print(f"## Solve rate on shared {len(shared_tasks)} tasks")
    print(f"  CAT:   {cat_solved_shared}/{len(shared_tasks)} = {cat_solved_shared/len(shared_tasks):.1%}")
    print(f"  ReAct: {react_solved_shared}/{len(shared_tasks)} = {react_solved_shared/len(shared_tasks):.1%}")

    # Full 30-task solve rate
    cat_total_solved = sum(1 for t in cat_trials.values() if t["is_resolved"])
    print()
    print(f"## Full 30-task solve rate")
    print(f"  CAT 30-task: {cat_total_solved}/30 = {cat_total_solved/30:.1%}")

    if len(react_covered) == 30:
        react_total_solved = sum(1 for t in react_trials.values() if t["is_resolved"])
        print(f"  ReAct 30-task: {react_total_solved}/30 = {react_total_solved/30:.1%}")
    else:
        react_total_solved = sum(1 for t in react_trials.values() if t["is_resolved"])
        print(f"  ReAct partial ({len(react_covered)}/30): {react_total_solved}/{len(react_covered)} = {react_total_solved/len(react_covered):.1%}")

    # Token efficiency on shared tasks
    cat_tokens = sum(
        cat_trials[t].get("total_input_tokens") or 0
        for t in shared_tasks if t in cat_trials
    )
    react_tokens = sum(
        react_trials[t].get("total_input_tokens") or 0
        for t in shared_tasks if t in react_trials
    )
    print()
    print(f"## Token efficiency on shared {len(shared_tasks)} tasks")
    print(f"  CAT total input tokens:   {cat_tokens:,}")
    print(f"  ReAct total input tokens: {react_tokens:,}")
    if react_tokens > 0:
        ratio = react_tokens / cat_tokens
        print(f"  CAT is {ratio:.1f}× cheaper on input tokens")

    # Per-task token detail for shared tasks
    print()
    print(f"## Per-task token detail")
    print(f"{'Task':<40} {'CAT toks':>10} {'React toks':>12} {'Ratio':>8}")
    print("-" * 72)
    for tid in shared_tasks:
        c_tok = cat_trials[tid].get("total_input_tokens") or 0
        r_tok = react_trials[tid].get("total_input_tokens") or 0
        ratio = r_tok / c_tok if c_tok > 0 else float("inf")
        print(f"  {format_short_id(tid):<38} {c_tok:>10,} {r_tok:>12,} {ratio:>7.1f}×")


if __name__ == "__main__":
    main()
