"""Migrate legacy data/tb-runs/* into data/runs/<bench>/<date>/<time>-<run-id>/ via symlinks.

Doesn't move or modify anything — creates symlinks only. Safe to re-run.

Usage:
    python scripts/migrate_runs.py            # plan + execute
    python scripts/migrate_runs.py --dry-run  # plan only

After migration the new layout (data/runs/...) becomes browsable while the old
paths (data/tb-runs/...) still work for any references that depend on them.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "data" / "tb-runs"
NEW = ROOT / "data" / "runs"


def _started_at(run_dir: Path) -> datetime | None:
    """Best-effort: read the earliest trial_started_at from results.json."""
    rj = run_dir / "results.json"
    if not rj.exists():
        # Fall back to dir mtime
        return datetime.fromtimestamp(run_dir.stat().st_mtime)
    try:
        data = json.loads(rj.read_text())
        starts = [
            t.get("trial_started_at") for t in data.get("results", [])
            if t.get("trial_started_at")
        ]
        if not starts:
            return datetime.fromtimestamp(run_dir.stat().st_mtime)
        # Strip subsecond + tz for parsing simplicity
        iso = min(starts)
        return datetime.fromisoformat(iso)
    except Exception:
        return datetime.fromtimestamp(run_dir.stat().st_mtime)


def _classify_bench(run_dir: Path) -> str:
    """Heuristic from dir name / contents."""
    name = run_dir.name.lower()
    parent = run_dir.parent.name.lower()
    if "swe" in name or "swe" in parent:
        return "swebench"
    if "react" in name or "react" in parent:
        return "swebench"   # ReAct baseline runs were on swebench
    if "termbench" in name or "tb-" in name:
        return "termbench"
    if "tau" in name:
        return "taubench"
    if "synthetic" in name:
        return "synthetic"
    # Default fallback
    return "swebench"


def _is_trial_dir(p: Path) -> bool:
    """Trial dirs match <taskid>.<rep>-of-<reps>.<runid>; skip them — only top-level runs."""
    import re
    return bool(re.search(r"\.\d+-of-\d+\.", p.name))


def discover_legacy_runs() -> list[Path]:
    """Walk LEGACY and yield top-level RUN directories only.

    A top-level run has `run_metadata.json` (written by `tb runs`) — that's
    the strongest signal. Trial-level dirs (one per task) also contain a
    `results.json` but are NOT runs themselves; filter them out by name.
    """
    if not LEGACY.exists():
        return []
    out: list[Path] = []
    for p in LEGACY.rglob("run_metadata.json"):
        run_dir = p.parent
        if _is_trial_dir(run_dir):
            continue
        out.append(run_dir)
    # Fallback: also accept dirs with results.json but NO run_metadata.json
    # AND NOT a trial-dir pattern (some very old runs lack run_metadata.json)
    for p in LEGACY.rglob("results.json"):
        run_dir = p.parent
        if _is_trial_dir(run_dir) or run_dir in out:
            continue
        if not (run_dir / "run_metadata.json").exists():
            out.append(run_dir)
    return sorted(set(out))


def plan_migration(legacy_dirs: list[Path]) -> list[tuple[Path, Path]]:
    """Return list of (legacy_dir, target_symlink_path)."""
    plans: list[tuple[Path, Path]] = []
    for d in legacy_dirs:
        started = _started_at(d)
        if started is None:
            continue
        bench = _classify_bench(d)
        date_part = started.strftime("%Y-%m-%d")
        time_part = started.strftime("%H-%M-%S")
        run_id = d.name
        target = NEW / bench / date_part / f"{time_part}-{run_id}"
        plans.append((d, target))
    return plans


def execute(plans: list[tuple[Path, Path]], dry_run: bool) -> None:
    for src, dst in plans:
        if dst.exists() or dst.is_symlink():
            print(f"  skip (exists)  {dst}")
            continue
        if dry_run:
            print(f"  would link     {dst.relative_to(ROOT)}  →  {src.relative_to(ROOT)}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Relative symlink so the tree is portable
        rel = Path(os.path.relpath(src, dst.parent))
        dst.symlink_to(rel)
        print(f"  linked         {dst.relative_to(ROOT)}  →  {rel}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    legacy = discover_legacy_runs()
    if not legacy:
        print(f"no legacy runs under {LEGACY}")
        return
    plans = plan_migration(legacy)
    print(f"discovered {len(legacy)} legacy runs; planning {len(plans)} symlinks")
    execute(plans, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
