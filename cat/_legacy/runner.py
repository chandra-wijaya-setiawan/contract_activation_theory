"""Run one task with one agent: `python -m cat.runner --task hello-file --agent cat`.

Drops a full trace + outcome.json into data/runs/{run_id}/. Designed to be
called repeatedly by the comparison harness (cat.compare).
"""
from __future__ import annotations
import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

from cat._legacy.agents import Agent, AgentResult, CatAgent, ReactAgent
from cat.tasks import Outcome, TaskSpec, all_task_ids, grade as synthetic_grade, resolve_task
from cat.trace import Recorder


AGENTS: dict[str, Type[Agent]] = {
    "cat": CatAgent,
    "react": ReactAgent,
}


def _load_env(env_path: Path = Path(".env")) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def run_one(
    task_id: str,
    agent_name: str = "cat",
    model: str = "openrouter/deepseek/deepseek-v4-flash",
    out_dir: Path = Path("data/runs"),
    max_iterations: int = 40,
) -> Outcome:
    _load_env()
    if agent_name not in AGENTS:
        raise ValueError(f"unknown agent {agent_name!r}; available: {sorted(AGENTS)}")
    task = resolve_task(task_id)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_id = f"{agent_name}-{task_id}-{run_id}"

    run_root = out_dir / run_id
    work = run_root / "work"
    work.mkdir(parents=True, exist_ok=True)

    seed = task.meta.get("seed")
    if isinstance(seed, dict):
        for rel_path, content in seed.items():
            p = work / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content.encode("utf-8"))

    agent_cls = AGENTS[agent_name]
    agent = agent_cls(model=model, max_iterations=max_iterations)

    recorder = Recorder(
        run_id=run_id,
        task_id=task.task_id,
        agent=agent.name,
        model=model,
        out_dir=run_root,
    )
    t0 = time.time()
    result: AgentResult = agent.run(task, recorder, workdir=work)
    wall = time.time() - t0

    # Final oracle (synthetic only for now)
    if task.bench == "synthetic":
        ok, score = synthetic_grade(task.task_id, work)
    else:
        ok, score = (result.verdict == "solved"), (1.0 if result.verdict == "solved" else 0.0)

    # Final verdict reconciles agent-reported + oracle
    verdict = result.verdict
    if task.bench == "synthetic":
        if ok:
            verdict = "solved"
        elif verdict == "solved" and not ok:
            verdict = "failed"

    traj = recorder.finalize()
    outcome = Outcome(
        task_id=task.task_id,
        run_id=run_id,
        agent=agent.name,
        verdict=verdict,
        score=score,
        wall_s=round(wall, 2),
        usd=round(traj.usd, 5),
        tool_calls=traj.n_tool_calls,
        n_loops=traj.n_loops,
        tokens_in=traj.tokens_in,
        tokens_out=traj.tokens_out,
        error=result.failure_reason,
        meta={
            "model": traj.model,
            "n_steps": traj.n_steps,
            "iterations": result.iterations,
            "started_at": traj.started_at,
            "ended_at": traj.ended_at,
        },
    )
    (run_root / f"{run_id}.outcome.json").write_text(
        outcome.model_dump_json(indent=2), encoding="utf-8"
    )
    return outcome


def main() -> int:
    p = argparse.ArgumentParser(prog="cat.runner")
    p.add_argument("--task", required=True, help="task id (e.g. hello-file)")
    p.add_argument("--agent", default="cat", choices=sorted(AGENTS), help="which agent to run")
    p.add_argument("--model", default="openrouter/deepseek/deepseek-v4-flash")
    p.add_argument("--out", default="data/runs", help="output dir")
    p.add_argument("--max-iterations", type=int, default=40)
    args = p.parse_args()

    outcome = run_one(
        task_id=args.task,
        agent_name=args.agent,
        model=args.model,
        out_dir=Path(args.out),
        max_iterations=args.max_iterations,
    )
    print(outcome.model_dump_json(indent=2))
    return 0 if outcome.verdict == "solved" else 1


if __name__ == "__main__":
    sys.exit(main())
