"""Exploration tool: run PlanComposer on a real SWE-bench task obligation
and print the 3 candidate plans side-by-side with their heuristic scores.

  python -m scripts.explore_plan_composer --task-id django__django-11239

What you see: how much do the three personas (conservative / minimal /
exploratory) actually diverge for a given obligation? Useful for sanity-
checking that K-sample composition produces meaningful diversity before
we wire it into the activator.

Makes 3 real LLM calls to OpenRouter (uses OPENROUTER_API_KEY from .env).
Cost: ~10K tokens (~$0.005 at deepseek-v4-flash rates).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Add repo root to path so this works when invoked as `python scripts/explore_plan_composer.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # type: ignore

from cat.llm import LLM
from cat.plan_composer import (
    PlanComposer,
    PersonaStipulator,
    HeuristicPlanScorer,
    PERSONAS,
)


DATASET_ROOT = os.environ.get(
    "TB_DATASET_PATH",
    os.path.expanduser("~/.cache/terminal-bench/swebench-verified/head"),
)


def load_obligation(task_id: str) -> str:
    """Read the `instruction` field from a SWE-bench task.yaml."""
    p = Path(DATASET_ROOT) / task_id / "task.yaml"
    if not p.exists():
        raise FileNotFoundError(f"task.yaml not found at {p}")
    data = yaml.safe_load(p.read_text())
    return (data.get("instruction") or "").strip()


def _summarize_plan(plan: dict) -> str:
    """One-line summary: phase shape + step count."""
    phases = plan.get("phases", [])
    return f"{len(phases)} phases / {sum(len(p['steps']) for p in phases)} steps: " + \
           " → ".join(f"{p['name']}({len(p['steps'])})" for p in phases)


def _print_plan(plan: dict, label: str, score: float):
    print(f"\n{'━' * 80}")
    print(f"{label}  score={score:.2f}  {_summarize_plan(plan)}")
    print('━' * 80)
    for phase in plan["phases"]:
        for step in phase["steps"]:
            sid = step.get("id", "?")
            intent = step.get("intent", "?")
            desc = step.get("description", "")
            tool = step.get("suggested_tool")
            args = step.get("suggested_args") or {}
            expects = step.get("expects") or {}
            result = expects.get("result", "")
            print(f"  [{phase['name']:>10}] {sid} ({intent:>11})  {desc[:80]}")
            if tool:
                args_short = json.dumps(args)[:70]
                print(f"             ⤷ {tool}({args_short})")
            if result:
                print(f"             expects: {result[:120]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True,
                    help="SWE-bench task id, e.g. django__django-11239")
    ap.add_argument("--model", default=os.environ.get(
        "CAT_MODEL", "openrouter/deepseek/deepseek-v4-flash"))
    args = ap.parse_args()

    # Load .env if present (gives us OPENROUTER_API_KEY)
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    obligation = load_obligation(args.task_id)
    print(f"Task: {args.task_id}")
    print(f"Model: {args.model}")
    print(f"Obligation (first 400 chars): {obligation[:400]}…\n")

    # Minimal in-memory recorder
    class _NullRec:
        traj = type("T", (), {"events": []})()
        def append(self, e): pass

    llm = LLM(model=args.model, recorder=_NullRec())

    # Build a stub contract object that the scorer only reads (no representations needed).
    class _StubContract:
        representations = []
        criteria = []
    contract = _StubContract()

    composer = PlanComposer(llm)
    composed = composer.compose(obligation, contract)

    # Print every candidate so we can compare divergence
    print(f"Total LLM calls: {len(composed.all_results)} "
          f"(in={sum(r.tokens_in for r in composed.all_results):,} "
          f"out={sum(r.tokens_out for r in composed.all_results):,})")
    print(f"Winning persona: {composed.winning_persona} (score {composed.winning_score:.2f})\n")

    for plan, score, persona in composed.all_candidates:
        is_winner = persona == composed.winning_persona
        label = f"{'★ WINNER ' if is_winner else '  '}persona={persona}"
        _print_plan(plan, label, score)


if __name__ == "__main__":
    main()
