# swe500 — pre-flight checklist

Full SWE-bench Verified run. **Not launched yet** — this is the prep doc.

## Estimates (from R27 swe5x5 baseline: avg 82K in / 6.5K out per trial)

| Setting | Trials | Cost | Wall time (cpubox) |
|---|---|---|---|
| 500 × 1 attempt @ concurrency 10 | 500 | **~$6.70** | **~1.8 hr** |
| 500 × 1 attempt @ concurrency 20 | 500 | ~$6.70 | ~0.9 hr |
| 500 × 3 attempts @ concurrency 10 | 1500 | ~$20.00 | ~5.3 hr |
| 500 × 3 attempts @ concurrency 20 | 1500 | ~$20.00 | ~2.7 hr |

Costs at openrouter/deepseek-v4-flash pricing ($0.14/M in, $0.28/M out).
Wall time assumes 127s avg agent runtime per trial.

## Disk

- ~12 MB per trial (jsonl trace + asciinema cast + tb manifests)
- 500 × 1 ≈ **~6 GB**; 1500 ≈ **~18 GB**
- Local disk: 642 GB free — plenty

## Pre-flight checklist (verify before launching)

- [ ] **cpubox responsive** — `ssh -o ConnectTimeout=5 cpubox uptime` succeeds. (Currently unresponsive while swe10x3 + post-cleanup-smoke run.)
- [ ] **No competing tb runs** — `ps -ef | grep "[t]b runs create"` shows nothing
- [ ] **Docker daemon healthy** — `DOCKER_HOST=ssh://cpubox docker ps` lists < 20 containers
- [ ] **Disk** — at least 10 GB free on local + cpubox (`df -h /`)
- [ ] **API key in .env** — `grep OPENROUTER_API_KEY .env` returns a key
- [ ] **Tests pass** — `python -m pytest tests/ -q` 220/220
- [ ] **Git clean** — `git status --short` empty
- [ ] **HEAD commit** — note `git log -1 --oneline` so we can correlate result with code version

## Launch command (artifact ready)

```bash
# default: 500 × 1 attempt, concurrency 10
scripts/launch_swe500.sh

# variants
scripts/launch_swe500.sh --n-attempts 3 --concurrency 20
scripts/launch_swe500.sh --run-id r27-swe500-pass3 --n-attempts 3
```

`scripts/launch_swe500.sh` builds the 500-task list from
`$HOME/.cache/terminal-bench/swebench-verified/head/` and delegates to
`scripts/run_bench.sh` (which sources `.env`, prunes orphan docker
networks, and creates the structured output dir).

## Monitor command (post-launch)

```bash
RD=data/runs/swebench/$(date -u +%Y-%m-%d)/<TIMESTAMP>-r27-swe500
# Per-task progress
find $RD -name "results.json" | grep -v "/r27-swe500/results.json" | wc -l
# Live pass count
find $RD -name "results.json" | grep -v "/r27-swe500/results.json" | \
  xargs -I{} python3 -c "import json; print(1 if json.loads(open('{}').read()).get('is_resolved') else 0)" \
  2>/dev/null | awk '{s+=$1} END {print s}'
```

## Failure-recovery plan

If the run is killed mid-flight:
1. `tb` writes per-trial `results.json` as each completes; partial progress is preserved.
2. Re-launch with **same `--run-id`** to resume — `tb` skips trials that already have results.
3. To inspect a stuck trial: `find $RD -path "*<task-id>*/agent-logs/*.jsonl"` and tail the trace.

## Why not launch yet

1. **swe10x3 still running** (currently 17/30). Wait for it to finish so cpubox isn't oversubscribed.
2. **post-cleanup-smoke still running** (currently 1/5). Once it lands 5/5 with reasonable pass rate, we know R27 + cleanup didn't break anything.
3. **Recommend 1 attempt first.** 500 × 1 = $6.70 / ~2 hr is a cheap sanity reading. Decide whether to do × 3 based on what we see.

## Open questions before launch

- **Concurrency**: 10 (safer, was used in past runs) or 20 (faster, may stress cpubox)?
- **N attempts**: 1 (cheap baseline) or 3 (pass^3 + pass@k stats for the report)?
- **Run ID**: default `r27-swe500` or something date-tagged?

Default plan if you say "go": `r27-swe500`, n_attempts=1, concurrency=10 → ~$6.70, ~1.8 hr.
