# CAT — Contract Activation Theory

A domain-agnostic agent framework. An agent run is a **contract discharge**: the agent receives an obligation, generates hypotheses, executes them with tools, and the oracle decides when the contract is fulfilled.

Works for SWE-bench, CRM, web automation, database tasks, conversational chat — any domain. Domain-specific knowledge lives only in task adapters, never in the core framework.

---

## Architecture

CAT has four core packages with clear separation of concerns:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        cat/agent/                                   │
│  HypothesisAgent (the loop)                                         │
│    Outer: Stipulator → hypothesis → Performer → oracle → repeat     │
│    Inner: Performer implements hypothesis in K attempts              │
│  BaseActivator (shared scaffolding: budget, state, admission)       │
│  AgentRegistry (loads profile.yaml → model_array, intelligence)     │
│  profiles/  (built-in profile YAML: chat, task, swebench)           │
└──────────┬──────────────────────┬───────────────────┬───────────────┘
           │                      │                   │
           ▼                      ▼                   ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│  cat/contract/   │  │    cat/llm/      │  │   cat/capability/    │
│                  │  │                  │  │                      │
│ Contract         │  │ LLM (wire)       │  │ CapabilityRegistry   │
│ State, Cost      │  │ LLMKernel (brain)│  │ Capability           │
│ Criterion        │  │ LLMRouter        │  │ ToolResult           │
│ Representation   │  │ Adapters (Kimi)  │  │ tools/               │
│ Schemas          │  │ Stipulator prompt│  │ full_registry        │
│ Representer      │  │ Performer prompt │  │                      │
│ AdmissionDecision│  │                  │  │                      │
│                  │  │ "how to think"   │  │ "how to act"         │
│ "what to do"     │  └──────────────────┘  └──────────────────────┘
└──────────────────┘
```

| Package | Role | Key types |
|---------|------|-----------|
| **`cat/contract/`** | The **legal document** — what must be done, how to verify it, what it costs | `Contract`, `State`, `Cost`, `Criterion`, `Representation`, `HypothesisSchema`, `PerformerTaskSchema` |
| **`cat/llm/`** | The **brain** — talking to models, self-healing on bad output, per-model quirks | `LLM`, `LLMKernel`, `LLMRouter`, `LLMModelAdapter` |
| **`cat/capability/`** | The **hands** — executing tool calls (bash, read, write, edit) and returning structured results | `CapabilityRegistry`, `Capability`, `ToolResult` |
| **`cat/agent/`** | The **orchestrator** — the hypothesis loop that binds contract + brain + hands together | `HypothesisAgent`, `BaseActivator`, `AgentRegistry` |

**Data flow:**

1. `HypothesisAgent.run()` receives a `Contract` (from `cat/contract/`)
2. **Representer** probes the workspace and populates `contract.representations`
3. **Stipulator** (via `LLMKernel`) generates a `HypothesisSchema` — what to change and why
4. **Performer** (via `LLMKernel`) executes the hypothesis by calling tools through `CapabilityRegistry.invoke()`
5. Each tool returns a `ToolResult` — the Performer sees results and decides next action
6. After durable changes, the **Oracle** (criterion predicate from `cat/contract/`) checks if the contract is fulfilled
7. If not, a new hypothesis is generated with accumulated knowledge — repeat

**Profile system** (`cat/agent/profiles/`): each profile is a `profile.yaml` that declares a `model_array` (weakest → strongest) and an `intelligence` value (0.0–1.0). The `LLMKernel` resolves intelligence to a concrete model index. Profiles control loop behavior via `LoopConfig` flags (`skip_stipulator`, `skip_oracle`, etc.).

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
cat-cli --profile chat --chat
```

```
😸: explain what a hypothesis loop is
😸: search for papers on reinforcement learning
😸: what files are in src/cat/
😸: can you fix the typo in design.md
```

One-shot:

```bash
cat-cli --profile chat --task "summarise design.md in 3 bullet points"
```

The chat profile uses `web_search(query)` for lookups (Wikipedia API, no bot-blocking) and `web_browse(url)` for fetching pages. Responses render as Markdown.

---

## Task agent

```bash
# One-shot task
cat-cli --profile task --task "add a --verbose flag to scripts/run_bench.sh"

# Interactive REPL, full hypothesis loop per message
cat-cli --profile task --chat
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

Each profile defines a `model_array` (ordered weakest → strongest) and an `intelligence` value (0.0–1.0) that picks the starting tier. The Performer escalates per hypothesis; the Stipulator runs at a fixed intelligence.

| Env var | Default | Description |
|---------|---------|-------------|
| `CAT_PLANNER_INTELLIGENCE` | `10` | Stipulator intelligence multiplier (clamped to 1.0) |
| `CAT_PERFORMER_STEP` | `0.2` | Performer intelligence increment per hypothesis |
| `CAT_MODEL_ARRAY` | (from profile) | Override model array (comma-separated) |

give_up at max tier → task fails.

---

## Project layout

```
src/
  cat/                        # core framework (domain-agnostic)
    __init__.py               # top-level exports: Contract, Activator, Recorder
    trace.py                  # Recorder, TraceEvent — shared event stream
    agent/                    # agent loop, profiles, registry
      hypothesis.py           # HypothesisAgent — the outer loop
      base.py                 # BaseActivator — shared scaffolding
      definition.py           # AgentDef, LoopConfig data models
      registry.py             # AgentRegistry — loads profile.yaml
      runner.py               # oracle/setup/teardown script runner
      checkpoint.py           # Checkpointer protocol + NullCheckpointer
      context.py              # context compression helpers
      knowledge.py            # knowledge extraction helpers
      factory.py              # agent factory helpers
      primitive.py            # primitive operations
      profiles/               # built-in profile definitions (profile.yaml)
        chat/                 # conversational assistant
        task/                 # full hypothesis loop for coding tasks
        swebench/             # SWE-bench adapter
    contract/                 # contract model + lifecycle
      model.py                # Contract, State, Cost, Constraints, Criterion, Representation
      schemas.py              # HypothesisSchema, PerformerTaskSchema, KnowledgeSchema, …
      representer.py          # Representer — probes workspace, populates representations
      admission.py            # admission gate logic
    llm/                      # LLM client, kernel, router, prompts
      client.py               # LLM (OpenAI SDK wrapper), LLMResult, call_structured
      kernel.py               # LLMKernel — intelligence-addressed, self-healing
      router.py               # model → backend dispatch
      card.py                 # static model metadata
      stipulator.py           # Stipulator prompt + call
      attestor.py             # Attestor prompt + call
      chat_judge.py           # LLM-as-judge for chat quality
      performer_plan.py       # continuation plan generation
      adapters/               # per-model request/response shaping (Kimi, etc.)
    capability/               # tool registry + built-in tools
      registry.py             # CapabilityRegistry — register, invoke, schema listing
      full_registry.py        # build_default_registry, build_docker_registry
      git_checkpoint.py       # GitCheckpointer (git plumbing, no docker import)
      tools/                  # one file per tool (bash, read, write, str_replace, etc.)
    reports/                  # KPI reports, attribution, HTML trace reports
    utils/                    # time, hashing, text helpers
  app/
    cat_cli/                  # cat-cli entry point: chat REPL + one-shot runner
    swebench/                 # SWE-bench runner + domain adapters
  experiment/                 # dev analytics tools (no tests)
    trace/                    # HTML/SVG trace renderers, CLI viewer
    ...
benchmarks/                   # domain adapters (swebench.py) + task fixtures
scripts/                      # operator scripts (run_bench.sh, trace_report.py, ...)
tests/                        # pytest suite (65 tests, no Docker required)
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
| `CAT_PLANNER_INTELLIGENCE` | Stipulator intelligence multiplier (default `10`) |
| `CAT_PERFORMER_STEP` | Performer escalation step per hypothesis (default `0.2`) |
| `CAT_MODEL_ARRAY` | Override model array (comma-separated, weakest→strongest) |
| `TB_DATASET_PATH` | Override SWE-bench dataset path |
| `OLLAMA_BASE_URL` | Ollama endpoint (default `http://10.0.0.3:11434/v1`) |
