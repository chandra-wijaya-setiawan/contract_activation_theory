# Contract Activation Theory (CAT)

A contract-disciplined agent loop that treats execution as the exception, not the default. An agent is a contract-fulfillment system — it activates capabilities only when a contract's conditions are satisfied and only to the minimum extent needed to discharge the obligation.

## Core idea

Standard ReAct loops drift: on a 5-step task, ReAct uses ~6× more tool calls and ~10× more tokens than CAT for the same solve rate. The discipline comes from:

1. **Pre-committed plan** — the planner decomposes the obligation once; the executor follows it step-by-step.
2. **Oracle-first verification** — contracts close on real test results, not model opinion.
3. **Criterion gate** — the executor cannot declare done; a deterministic criterion (or LLM judge as fallback) must agree.
4. **Budget enforcement** — hard caps on tokens, tool calls, wall time, and cost.

## The 8-step loop

```
1. INGEST      receive obligation (LATENT)
2. REPRESENT   establish verified facts (probes, no LLM)
3. DECOMPOSE   planner emits ordered checklist (1 LLM call)
4. ACTIVATE    admission gate (one-shot):
                  boundary check
                  environment fit
                  capacity check
                  capability grant
               → LATENT → ACTIVE on pass;
                 consult ContractPolicy on fail (FAIL / SUSPEND / ESCALATE / ASK_USER)
5. SELECT      pick capability from grant for current step (v1: fixed executor LLM)
6. INVOKE      one LLM call → one tool call → one result
7. VERIFY      run criterion; advisory judge per plan step
8. CLOSE       FULFILLED / SUSPENDED / FAILED / re-enter at 5
```

Steps 1-4 are activation. Steps 5-8 are discharge, looped under the grant from Step 4 until terminal.

The REPRESENT step also decides **how to judge**: it emits a `judge_strategy` representation (`oracle: <cmd>` or `llm_only`) which the Activator uses to auto-inject a deterministic criterion when no oracle was pre-set.

## Phase 1 results (synthetic benchmark)

| task | agent | tool calls (μ) | tokens in (μ) | solve |
|---|---|---:|---:|---:|
| hello-file | ReAct | 2.0 | 2,139 | 3/3 |
| hello-file | CAT | 1.0 | 1,140 | 3/3 |
| multi-step-refactor | ReAct | 21.0 | 57,013 | 3/3 |
| multi-step-refactor | CAT | 3.3 | 5,439 | 3/3 |

CAT uses **0.16× tool calls** and **0.10× tokens** vs ReAct on a 5-step task at the same solve rate.

## Quick start

```bash
pip install -e ".[bench]"
export OPENROUTER_API_KEY=...
export DOCKER_HOST=ssh://cpubox   # or local Docker socket

# terminal-bench tasks (local task dir)
tb runs create \
  --dataset-path tasks \
  --task-id hello-world \
  --agent-import-path cat.tb_agents:TbCatAgent \
  --agent-kwarg model_name=openrouter/deepseek/deepseek-v4-flash \
  --output-path data/tb-runs/my-run --run-id my-run-v1

# SWE-bench verified (uses the benchmarks/ wrapper — see below)
tb runs create \
  --dataset swebench-verified \
  --task-id psf__requests-2931 \
  --agent-import-path benchmarks.swe_bench:TbSweAgent \
  --agent-kwarg model_name=openrouter/deepseek/deepseek-v4-flash \
  --output-path data/tb-runs/swe --run-id swe-v1
```

## Benchmark wrappers (`benchmarks/`)

The `cat/` framework is benchmark-agnostic. Benchmark-specific oracle commands live in `benchmarks/`, separate from the framework code.

```
cat/              # framework — generic, no benchmark knowledge
  tb_agents.py    # TbCatAgent(test_cmd=...) and TbReactAgent

benchmarks/       # one file per benchmark
  swe_bench.py    # SWE-bench verified oracle commands + TbSweAgent
```

### Why wrappers instead of subclasses inside the framework

`TbCatAgent` already accepts `test_cmd: str | dict[task_id, cmd]`. The wrapper just supplies the dict from outside:

```python
# benchmarks/swe_bench.py
from cat.tb_agents import TbCatAgent

ORACLE_CMDS = {
    "psf__requests-2931": "cd /testbed && python -m pytest tests/test_models.py ...",
    "psf__requests-2317": "...",
    # add more task IDs here
}

class TbSweAgent(TbCatAgent):
    def __init__(self, **kwargs):
        kwargs.setdefault("test_cmd", ORACLE_CMDS)
        kwargs.setdefault("max_iterations", 60)
        kwargs.setdefault("max_criterion_fails", 20)
        super().__init__(**kwargs)
```

Adding a new benchmark requires only a new file in `benchmarks/` — zero changes to `cat/`.

### Adding a new benchmark

Create `benchmarks/my_benchmark.py`:

```python
from cat.tb_agents import TbCatAgent

ORACLE_CMDS = {
    "task-id-1": "cd /workspace && python -m pytest tests/ -q",
    "task-id-2": "...",
}

class TbMyBenchAgent(TbCatAgent):
    def __init__(self, **kwargs):
        kwargs.setdefault("test_cmd", ORACLE_CMDS)
        super().__init__(**kwargs)
```

Then use `--agent-import-path benchmarks.my_benchmark:TbMyBenchAgent`.

### Oracle command rules

All oracle commands must follow these rules to avoid false positives:

1. **`sys.path.insert(0, "/testbed")`** — SWE-bench containers have the package installed in site-packages. Without the path insert, the oracle tests the original buggy code regardless of agent edits.
2. **No pipes** — `cmd | tail -N` masks the exit code (`$?` captures `tail`'s exit, always 0). Write the full command without pipes.
3. **Broad scope** — run the full relevant test module, not just one assertion. Narrow oracles let collateral edits to other files go undetected.
4. **Exclude network fixtures** — `-k 'not httpbin and not http'` prevents flaky failures from network tests.

## Running tests

```bash
pytest tests/ -q
```

Unit tests use a `ScriptedLLM` mock — no real LLM calls, no Docker.

Integration tests (marked `@pytest.mark.integration`) require `OPENROUTER_API_KEY`.
Docker tests (marked `@pytest.mark.docker`) require `DOCKER_HOST`.

## Repository layout

```
cat/
  activator.py      # main loop: REPRESENT → DECOMPOSE → ACTIVATE → INVOKE → VERIFY
  representer.py    # REPRESENT step: probes + judge_strategy derivation
  contracts.py      # Contract, Criterion, State, Representation, ContractPolicy, AdmissionDecision
  capabilities.py   # tool registry (bash, read, write, list, str_replace, rg, fd, multi_edit, apply_patch, git_status, git_diff, goto_def, find_refs, outline)
  tb_agents.py      # TbCatAgent and TbReactAgent for terminal-bench
  llm.py            # LLM wrapper (OpenRouter / OpenAI-compatible)
  trace.py          # TraceEvent recorder
  metrics.py        # RunMetrics — per-task KPI from a trace JSONL
  attribution.py    # layer credit/blame (LLM-primary, heuristic fallback)
  run_report.py     # HTML + JSON run report + cross-run INDEX

benchmarks/
  swe_bench.py      # SWE-bench verified oracle commands and TbSweAgent

tasks/              # local terminal-bench task definitions
tests/              # unit tests (mock LLM, no external deps)
data/runs/          # experiment outputs (see "Run output layout" below)
DESIGN.md           # full theory and architecture
REPORT.md           # Phase 1 synthetic benchmark results
PLAN.md             # project plan v1.0-v2.0
```

## Run output layout

All runs (any benchmark) land under `data/runs/<bench>/<YYYY-MM-DD>/<HH-MM-SS>-<run-id>/`.
Bench-first because most analysis is within a bench; date subdir for daily
browsing; time prefix in the leaf dir name keeps within-day runs sortable
by `ls`.

```
data/runs/
  <bench>/                            # swebench | termbench | taubench | synthetic | webarena | gaia
    <YYYY-MM-DD>/                     # day folder
      <HH-MM-SS>-<run-id>/            # the actual run, time-prefixed
        manifest.json                 # bench, agent, git_sha, task list, started_at, ended_at, pass_rate
        results.json                  # harness output (from tb runs)
        run.log
        run_metadata.json
        _kpi_report.html              # per-run HTML report (1.5.18)
        _kpi_report.json              # machine-readable for cross-run aggregation
        _attribution_cache/           # cached LLM attributions keyed by trace hash
        <task-id>/
          <trial-name>/
            agent-logs/<run>.jsonl    # the full trace
            <run>.task_kpi.json       # per-task heuristic-only attribution (auto-emit)
            panes/
            sessions/
  INDEX.html                          # cross-run dashboard (regenerated by cat.run_report --index)
```

Concrete examples:

```
data/runs/swebench/2026-05-27/06-59-46-swe10-v1.5-envelope/
data/runs/swebench/2026-05-27/12-30-00-swe10-v1.5-envelope-retry2/
data/runs/termbench/2026-05-20/15-22-08-hello-world-fix-pandas/
data/runs/taubench/2026-06-04/09-15-00-airline-30tasks/
```

### Launching a run

Use the launcher (planned for 1.5.20):

```bash
scripts/run_bench.sh swebench \
  --run-id swe10-v1.5-envelope \
  --tasks swe10 \
  --agent benchmarks.swe_bench:TbSweAgent \
  --concurrency 3
```

The launcher:
- Computes the timestamped output path under `data/runs/swebench/<date>/<time>-<run-id>/`
- Writes `manifest.json` with git SHA, agent kwargs, started_at
- Forwards to `tb runs create`
- On completion: backfills `manifest.json` with ended_at + pass_rate; runs `python -m cat.run_report <dir>` to emit the HTML report; regenerates `data/runs/INDEX.html`

### Browsing reports

A local HTTP server on port 8000 (rooted at this repo) lets any browser hit:

```
http://localhost:8000/data/runs/INDEX.html
http://localhost:8000/data/runs/swebench/2026-05-27/06-59-46-swe10-v1.5-envelope/_kpi_report.html
```

Start the server with:

```bash
cd /home/coder/repo && python3 -m http.server 8000 --bind 0.0.0.0 &
```

### Legacy paths

Runs created before 2026-05-27 still live under `data/tb-runs/...` (e.g.
`data/tb-runs/swe30/swe30-cat-v15/`, `data/tb-runs/swe10/swe10-react/`).
Those are not auto-migrated — moving them would break references in PLAN.md
and scripts. They remain readable by `python -m cat.run_report` as-is.
A `scripts/migrate_runs.py` helper (planned in 1.5.20) creates symlinks
under `data/runs/swebench/_legacy/` for the runs we still want to compare
against.
