# CAT — Contract Activation Theory

A domain-agnostic agent framework. An agent run is a **contract discharge**: the agent receives an obligation, generates hypotheses, executes them with tools, and the oracle decides when the contract is fulfilled.

Works for SWE-bench, CRM, web automation, database tasks, conversational chat — any domain. Domain-specific knowledge lives only in task adapters, never in the core framework.

---

## Install

```bash
pip install -e ".[dev]"
```

Requires Python 3.11+. For benchmarks also install `terminal-bench`:

```bash
pip install terminal-bench
```

---

## Interactive chat

```bash
cat-cli --skill chat --chat
```

```
😸: explain what a hypothesis loop is
😸: search for papers on reinforcement learning
😸: what files are in src/cat/
😸: can you fix the typo in design.md
```

One-shot:

```bash
cat-cli --skill chat --task "summarise design.md in 3 bullet points"
```

The chat skill uses `web_search(query)` for lookups (Wikipedia API, no bot-blocking) and `web_browse(url)` for fetching pages. Responses render as Markdown.

---

## Task agent

```bash
# One-shot task
cat-cli --skill task --task "add a --verbose flag to scripts/run_bench.sh"

# Interactive REPL, full hypothesis loop per message
cat-cli --skill task --chat
```

Uses the full hypothesis loop: Stipulator → Performer → Oracle → Attestor, with git checkpointing between hypotheses.

---

## SWE-bench

### Prerequisites

```bash
# 1. Docker daemon — runs containers on a remote host
export DOCKER_HOST=ssh://cpubox        # or set in .env at repo root

# 2. LLM API key
export OPENROUTER_API_KEY=sk-or-...

# 3. terminal-bench harness
pip install terminal-bench
```

### Task dataset

Tasks are stored in a JSONL file. Each line is one task instance:

```
~/.cache/terminal-bench/swebench-verified/head
```

Each line is a JSON object with fields including `instance_id`, `repo`, `base_commit`, and the test patch. The **Verified** subset (default) contains **500 tasks** across 12 repositories. The full SWE-bench set has 2,294.

```bash
# Count tasks in your local copy
wc -l ~/.cache/terminal-bench/swebench-verified/head

# List all task IDs
jq -r '.instance_id' ~/.cache/terminal-bench/swebench-verified/head | sort -u | head -20
```

Task IDs follow the pattern `repo__repo-number`, e.g.:
- `django__django-11239`
- `scikit-learn__scikit-learn-14141`
- `sympy__sympy-12481`

Override the dataset path with `--dataset-path` or `TB_DATASET_PATH`:

```bash
export TB_DATASET_PATH=~/.cache/terminal-bench/swebench-verified/head
```

### Smoke test (single task)

```bash
scripts/run_bench.sh swebench \
  --run-id smoke \
  --task-id django__django-11239 \
  --concurrency 1 \
  --n-attempts 1
```

### Multiple tasks in parallel

```bash
scripts/run_bench.sh swebench \
  --run-id my-run-v1 \
  --task-id django__django-11239 \
  --task-id scikit-learn__scikit-learn-14141 \
  --task-id sympy__sympy-12481 \
  --concurrency 3
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--run-id` | required | Lowercase slug for this run (used as Docker project name) |
| `--task-id` | required | SWE-bench task ID — repeat for multiple tasks |
| `--concurrency` | `3` | Parallel trials |
| `--n-attempts` | `1` | Hypothesis attempts per task |
| `--agent` | `benchmarks.swe_bench:TbSweAgent` | Agent import path |
| `--dataset-path` | `~/.cache/terminal-bench/swebench-verified/head` | JSONL dataset file |

### Output

Results land at:

```
data/runs/swebench/<YYYY-MM-DD>/<HH-MM-SS>-<run-id>/
  manifest.json        # run metadata + pass_rate
  results.json         # per-task resolved / failed
  launcher.log         # terminal-bench stdout
  _kpi_report.html     # HTML summary with per-task breakdown
  <task-id>/           # per-task trace + patch
```

Top-level index regenerates automatically:

```
data/runs/INDEX.html
```

---

## Model escalation

The agent escalates model tiers on `give_up`:

| Tier | Env var | Default |
|------|---------|---------|
| T0 | `CAT_MODEL` | `deepseek-v4-flash` |
| T1 | `CAT_ESCALATION_MODEL_T1` | `deepseek-v4-pro` |
| T2 | `CAT_ESCALATION_MODEL_T2` | `claude-haiku-4-5` |

give_up at T2 → task fails.

---

## Project layout

```
benchmarks/        # domain adapters (swe_bench.py) + task fixtures
scripts/           # operator scripts (run_bench.sh, trace_report.py, ...)
src/cat/           # core framework library
  engine/          # HypothesisLoopActivator
  capability/      # tool registry + built-in tools
  contract/        # Contract model + schemas
  llm/             # LLM client + adapters
  skill/           # skill YAML loader + built-in skills (chat, task, swebench)
  cli/             # cat-cli entry point
src/tooling/       # trace HTML/SVG renderer + run reports
tests/             # pytest suite (123 tests, no Docker required)
```

---

## Tests

```bash
python -m pytest tests/                        # full suite (~6s)
python -m pytest tests/ -m "not integration"  # skip live LLM tests
```

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `OPENROUTER_API_KEY` | Required for all LLM calls |
| `DOCKER_HOST` | Docker daemon for SWE-bench (default `ssh://cpubox`) |
| `CAT_MODEL` | Base model (T0) |
| `CAT_ESCALATION_MODEL_T1` | Escalation tier 1 |
| `CAT_ESCALATION_MODEL_T2` | Escalation tier 2 |
| `TB_DATASET_PATH` | Override SWE-bench dataset path |
