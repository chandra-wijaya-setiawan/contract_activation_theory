"""Seed the SQLite corpus from existing run dirs.

Usage:
    python scripts/seed_corpus.py <db_path> <run_dir> [<run_dir>...]

For each task in each run_dir, locates the JSONL trace and the harness
oracle outcome (results.json:is_resolved), ingests via Corpus.ingest_trace.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cat._legacy.corpus import Corpus


def seed_one_run(corpus: Corpus, run_dir: Path) -> dict:
    results_path = run_dir / "results.json"
    if not results_path.exists():
        return {"run_dir": str(run_dir), "skipped": "no results.json"}
    results = json.loads(results_path.read_text())
    oracle = {r["task_id"]: bool(r.get("is_resolved")) for r in results.get("results", [])}

    n_ingested = 0
    n_skipped = 0
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith("_"):
            continue
        for trial_dir in task_dir.iterdir():
            if not trial_dir.is_dir():
                continue
            logs = (trial_dir / "agent-logs")
            if not logs.exists():
                continue
            for jsonl in logs.glob("*.jsonl"):
                reached = oracle.get(task_dir.name, False)
                tid = corpus.ingest_trace(jsonl, reached_goal=reached)
                if tid is not None:
                    n_ingested += 1
                else:
                    n_skipped += 1
    return {"run_dir": run_dir.name, "ingested": n_ingested, "skipped_duplicate": n_skipped}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db_path")
    ap.add_argument("run_dir", nargs="+")
    args = ap.parse_args()
    corpus = Corpus(Path(args.db_path))
    for d in args.run_dir:
        info = seed_one_run(corpus, Path(d))
        print(info)
    print()
    print("Corpus stats:", corpus.stats())


if __name__ == "__main__":
    main()
