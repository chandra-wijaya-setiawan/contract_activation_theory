# CAT — Contract Activation Theory

## What CAT is

CAT is a **domain-agnostic agent framework** built around one idea: an agent run is a contract discharge. The framework asks, before executing anything, whether the contract can be fulfilled — and tracks whether it was.

A contract names:
- what the agent must accomplish (obligation)
- what condition proves it (criterion / oracle)
- what the world must look like for execution to be valid (representations)
- how much resource the agent may spend (constraints)

The framework is not tied to code repair, web automation, CRM, or any other domain. Domain-specific knowledge enters through task adapters and the `context` field — never in system prompts or core logic.

---

## Roles

Four roles execute a contract. Each is a function from inputs to a structured schema output — no shared mutable state between them.

| Role | Purpose | When called |
|------|---------|-------------|
| **Representer** | Probes the workspace, populates `contract.representations` with context (repo structure, test infrastructure, judge strategy) | Once per hypothesis cycle, before Stipulator |
| **Stipulator** | Generates one testable hypothesis from the obligation + accumulated knowledge | Once per hypothesis cycle |
| **Performer** | Implements the hypothesis using available tools | Inner loop, up to `max_attempts` times |
| **Oracle** | Runs the criterion predicate — deterministic when available, LLM-as-judge (intelligence=1.0) otherwise | After every durable change |
| **Attestor** | When oracle failed: decides whether to generate a new hypothesis or stop. Also serves as the LLM judge when no deterministic oracle exists | Once per failed cycle |

---

## Contract Model

```python
@dataclass
class Contract:
    id:              str
    obligation:      str
    criteria:        list[Criterion]     # oracle predicate(s)
    representations: list[Representation]
    constraints:     Constraints
    capabilities:    list[str]           # which tools are declared
    cost:            Cost                # accrued spend
    state:           ContractState       # PENDING → ACTIVE → FULFILLED | FAILED | SUSPENDED
    depth:           int                 # nesting level (0 = top-level)
    chat_history:    list[dict] = []     # set by chat REPL; propagated to PerformerTaskSchema

@dataclass
class State:
    workdir:  Path
    kv:       dict[str, Any]   # mutable world facts
    env:      dict[str, str]

@dataclass
class Constraints:
    max_tokens_total: int
    max_tool_calls:   int
    max_llm_calls:    int
    max_usd:          float
    max_wall_s:       float
    deadline_ts:      float | None

@dataclass
class Criterion:
    id:          str
    description: str
    predicate:   Callable[[State], tuple[bool, Evidence]]
    is_oracle:   bool = False
```

The **oracle criterion** is the primary judge. Two modes are supported:

- **Deterministic** — a shell command or predicate that returns pass/fail without LLM involvement (e.g. `pytest` on SWE-bench). Preferred when available.
- **LLM-as-judge** — when no deterministic oracle exists (CRM, web automation, open-ended tasks), the Attestor is invoked with `intelligence=1.0` (the highest model tier) as a high-confidence LLM judge. Not yet fully wired for all domains — currently only the deterministic path is exercised on SWE-bench.

---

## Outer Loop (HypothesisLoopActivator)

```
run(contract, state):
    knowledge = KnowledgeSchema()
    checkpointer.snapshot("base")     # original clean state
    last_checkpoint = "base"

    # Two kernels: planner (schema mode) and performer (toolcall mode)
    planner_kernel = LLMKernel(models, mode="schema", schema=HypothesisSchema, ...)
    performer_kernel = LLMKernel(models, mode="toolcall", schema=PerformerExecutionStep, ...)

    for hypothesis_n in range(max_hypotheses):
        # Performer intelligence band: H1=0.0, H2=0.2, ... clamped 1.0
        performer_kernel.set_intelligence(hypothesis_n * PERFORMER_INTELLIGENCE_STEP)

        # Representer probes workspace → contract.representations
        representer.represent(contract, state)

        if skip_stipulator:
            hypothesis = synthetic_hypothesis(contract.obligation)
        else:
            hypothesis = planner_kernel.run(stip_messages)
        # → HypothesisSchema: location, change, predicts, basis, start_from

        checkpointer.restore(hypothesis.start_from)   # "base" or "hK"

        result = _perform_hypothesis(task, performer_kernel=performer_kernel)
        # → PerformerOutcome: verdict ∈ {completed, exhausted, give_up}

        if result.any_durable_change:
            checkpointer.snapshot(f"h{hypothesis_n+1}")
            last_checkpoint = f"h{hypothesis_n+1}"

        knowledge.update(result)

        if skip_oracle and result.verdict == "completed":
            return FULFILLED

        if result.verdict == "give_up":
            continue   # escalate to next hypothesis

        if verify_criteria(contract, state):
            return FULFILLED

    return FAILED   # all hypotheses exhausted
```

**No plan tree. No stall counter. No Resolver.**

The only stateful decisions per cycle are:
1. Which checkpoint to branch from (`hypothesis.start_from` — chosen by the Stipulator)
2. Which model tier the Performer starts on (intelligence band, auto-escalated per hypothesis)
3. Self-heal escalation inside LLMKernel (reask → retry → escalate on repeated failures)

---

## Inner Loop (Performer)

The Performer receives a narrow task: one hypothesis, one oracle command, compressed context. In chat mode, prior turns are prepended as real message pairs so context survives across REPL turns.

```
_perform_hypothesis(task: PerformerTaskSchema, performer_kernel, max_attempts=5):
    messages = list(task.chat_history) + [task_prompt]

    for attempt in 1..max_attempts:
        # LLMKernel handles: reask on bad JSON, retry on truncation, escalate on exhaustion
        step = performer_kernel.run(messages, tools=active_tools)
        # → PerformerExecutionStep: intent, tools, thinking

        if step.intent == "done":    return completed(step.reason)
        if step.intent == "give_up": return give_up(step.reason)

        # Dispatch tool calls via registry
        results = [registry.invoke(tc.name, tc.args) for tc in step.tools]
        messages += [step, results]

        # Auto-give_up: modification tool errored → hypothesis target is wrong
        if modification_tool_failed(results): return give_up(...)

        # Force-commit: 2+ attempts with no durable change → nudge + restrict tools
        if attempt >= 2 and not any_durable_change:
            restrict_tools_to_mutation_only()

        # Auto-complete: durable change made, no errors, oracle passes
        if durable_change and not errors and oracle_passes():
            return completed(...)

    return exhausted
```

The Performer thinks in `PerformerExecutionStep` units: each step declares intent (`look | change | check | done | give_up`), tool calls, expected outcome, and what comes next. The schema validator enforces consistency — `done/give_up` must have a reason and no tools; `look/change/check` must have tools, expect, and then.

---

## Checkpoint Branching

Each hypothesis branches from a chosen state, not blindly on top of the previous failed attempt. This eliminates **cumulative patch corruption** (H2 patching H1's broken edits).

```python
class Checkpointer(Protocol):
    def snapshot(self, label: str) -> bool
    def restore(self, label: str) -> bool
    def labels(self) -> list[str]
```

| Implementation | Backing store | Use case |
|---|---|---|
| `NullCheckpointer` | none (no-op) | default; degrades to forward-editing |
| `GitCheckpointer` | git plumbing (`write-tree`/`read-tree`) | SWE-bench, any git repo |

The Stipulator receives the list of available checkpoint labels and the oracle failure for each. It picks `start_from="base"` to discard prior changes or `start_from="hK"` to build on a partially-correct attempt.

Checkpoint calls are always wrapped defensively — a failure emits a `checkpoint_error` trace event and falls back to forward-editing; it never crashes the task.

---

## Model Escalation Tiers

Execution starts on the cheapest model and escalates only when stuck. The Stipulator (planning) uses a separate intelligence level so hypothesis quality is independent of the execution tier.

Each profile defines a `model_array` (ordered weakest → strongest) and an `intelligence` value (0.0–1.0) that picks the starting tier:

```yaml
# cat/agent/profiles/task/profile.yaml
loop_config:
  model_array:
    - openrouter/deepseek/deepseek-v4-flash   # T0
    - openrouter/deepseek/deepseek-v4-pro     # T1
    - openrouter/owl-alpha                     # T2
  intelligence: 1.0   # planner starts at strongest
```

The Performer starts at `intelligence=0.0` (fastest) and escalates by `PERFORMER_INTELLIGENCE_STEP` (default `0.2`) per hypothesis. The Stipulator intelligence is scaled by `PLANNER_INTELLIGENCE` (env var `CAT_PLANNER_INTELLIGENCE`, default `10`).

**Escalation trigger:** Performer returned `give_up` or `exhausted`, **or** the LLMKernel self-heal loop exhausts retries at the current tier. At max tier, the task fails.

---

## Chat REPL (conversation continuity)

`cat-cli --profile chat --chat` maintains a `Session._history` list of `{user, assistant}` dicts across turns. Before each task:

1. `Session.run_task()` converts the last 6 history entries into real `[{role, content}, …]` message pairs and sets `contract.chat_history`.
2. The activator propagates `contract.chat_history` → `PerformerTaskSchema.chat_history`.
3. `_perform_hypothesis()` prepends those pairs to `inner_messages` before the current task prompt.

After the run, the assistant's response is extracted from the last `knowledge_update` trace event (`verdict == "completed"`, field `reason`) and appended to `Session._history`.

**Why real message pairs (not a string blob).** Prepending identical history prefix bytes each turn makes the prefix stable. OpenAI-compatible providers serve stable prefixes from KV cache — verified by `LLMResult.tokens_cached` (see next section).

---

## KV Cache Tracking

Every LLM call records how many prompt tokens were served from cache:

```
API response.usage.prompt_tokens_details.cached_tokens
  → LLMResult.tokens_cached
  → AdapterResult.tokens_cached
  → LLMKernel.usage_records[n]["tokens_cached"]
  → llm_usage trace event payload["cached"]
```

`tokens_cached > 0` confirms the provider reused a prefix from KV cache. For chat REPL turns with a long shared system prompt + history, this typically cuts input-token billing by 40–80 % after the first turn.

---

## Schemas

All Pydantic data shapes end in `Schema`. Runtime dataclasses (no validation) are plain nouns. Operations end in `Op`.

### Data flow between roles

```
Representer output → contract.representations: list[Representation]
    key: str, value: str, evidence: str

Stipulator output → HypothesisSchema
    location:   str        # where to act
    change:     str        # what to do
    predicts:   str        # verifiable outcome
    basis:      str        # why this hypothesis
    start_from: str        # "base" | "hK"

Performer input  → PerformerTaskSchema
    obligation:   str
    hypothesis:   HypothesisSchema
    oracle:       str        # test command
    context:      str        # compressed knowledge
    max_attempts: int = 5
    chat_history: list[dict] = []  # prior turns as [{role, content}, …]

Performer step   → PerformerExecutionStep
    intent:  look | change | check | done | give_up
    tools:   list[ToolCall]
    expect:  str | None    # required for look/change/check
    then:    intent | None # required for look/change/check
    reason:  str | None    # required for done/give_up

Knowledge state  → KnowledgeSchema
    findings:           dict[str, str]   # location → what was found
    failed_hypotheses:  list[str]        # what was tried + why oracle failed
    oracle_failures:    list[str]        # specific test failure messages

Chat judge output → ChatJudgeSchema
    verdict:   accept | retry
    reason:    str

(AttestorV3Schema is retained for backward compat but the Attestor role
is now handled by the LLMKernel self-heal loop + chat_judge in chat mode.)
```

### Naming conventions

| Category | Rule | Examples |
|---|---|---|
| Roles (LLM-backed components) | noun, no suffix | `Stipulator`, `Performer`, `Attestor`, `Activator`, `Representer` |
| Schemas (Pydantic, validated) | `...Schema` suffix | `HypothesisSchema`, `KnowledgeSchema` |
| Runtime objects (dataclasses) | plain noun | `Contract`, `State`, `Cost`, `Criterion` |
| Tree-edit operations | `...Op` suffix | `CommitOp`, `RefineOp` |

---

## Directory Structure

```
src/
├── cat/                        # core framework (domain-agnostic)
│   ├── __init__.py             # top-level exports: Contract, Activator, Recorder
│   ├── trace.py                # Recorder, TraceEvent — shared event stream
│   │
│   ├── agent/                  # agent loop, profiles, registry
│   │   ├── hypothesis.py       # HypothesisAgent — the outer loop (Stipulator→Performer→Oracle)
│   │   ├── base.py             # BaseActivator — cost, budget, criteria, admission
│   │   ├── definition.py       # AgentDef, LoopConfig data models
│   │   ├── registry.py         # AgentRegistry — loads profile.yaml from profiles/ dirs
│   │   ├── runner.py           # oracle/setup/teardown script runner
│   │   ├── checkpoint.py       # Checkpointer protocol + NullCheckpointer
│   │   ├── context.py          # context compression helpers
│   │   ├── knowledge.py        # knowledge extraction helpers
│   │   ├── factory.py          # agent factory helpers
│   │   ├── primitive.py        # primitive operations
│   │   └── profiles/           # built-in profile definitions (profile.yaml)
│   │       ├── chat/           # conversational assistant
│   │       ├── task/           # full hypothesis loop for coding tasks
│   │       └── swebench/       # SWE-bench adapter
│   │
│   ├── contract/               # contract model + lifecycle
│   │   ├── model.py            # Contract, State, Cost, Constraints, Criterion, Representation
│   │   ├── schemas.py          # HypothesisSchema, PerformerTaskSchema, KnowledgeSchema, …
│   │   ├── representer.py      # Representer — probes workspace, populates representations
│   │   └── admission.py        # admission gate logic
│   │
│   ├── llm/                    # LLM client, kernel, router, prompts
│   │   ├── client.py           # LLM (OpenAI SDK wrapper), LLMResult, call_structured
│   │   ├── kernel.py           # LLMKernel — intelligence-addressed, self-healing
│   │   ├── router.py           # model → backend dispatch
│   │   ├── card.py             # static model metadata
│   │   ├── stipulator.py       # Stipulator prompt + call
│   │   ├── attestor.py         # Attestor prompt + call
│   │   ├── chat_judge.py       # LLM-as-judge for chat quality
│   │   ├── performer_plan.py   # continuation plan generation
│   │   └── adapters/           # per-model request/response shaping (Kimi, etc.)
│   │       ├── __init__.py
│   │       └── adapters.py
│   │
│   ├── capability/             # tool registry + built-in tools
│   │   ├── registry.py         # CapabilityRegistry — register, invoke, schema listing
│   │   ├── full_registry.py    # build_default_registry, build_docker_registry
│   │   ├── git_checkpoint.py   # GitCheckpointer (git plumbing, no docker import)
│   │   └── tools/              # one file per tool
│   │       ├── bash.py         # bash, rg, fd
│   │       ├── read.py         # read (offset/limit)
│   │       ├── write.py        # write
│   │       ├── str_replace.py  # str_replace
│   │       ├── multi_edit.py   # multi_edit
│   │       ├── apply_patch.py  # apply_patch
│   │       ├── outline.py      # outline (jedi symbol nav)
│   │       ├── goto_def.py     # goto_def
│   │       ├── find_refs.py    # find_refs
│   │       ├── git_diff.py     # git_diff
│   │       ├── git_status.py   # git_status
│   │       ├── done.py         # done (task complete signal)
│   │       ├── give_up.py      # give_up (hypothesis wrong signal)
│   │       └── _helpers.py     # shared: path resolution, truncation
│   │
│   ├── reports/                # KPI reports, attribution, HTML trace reports
│   │   ├── html_report.py      # HTML trace report generator
│   │   ├── text_report.py      # plain text KPI report generator
│   │   ├── metrics.py          # KPI metric calculations
│   │   └── attribution.py      # token attribution
│   │
│   └── utils/                  # time, hashing, text helpers
│
├── app/
│   ├── cat_cli/                # cat-cli entry point: chat REPL + one-shot runner
│   │   └── cat_cli.py          # main(), Session, StreamingRecorder, Spinner
│   └── swebench/               # SWE-bench runner + domain adapters
│       ├── swe_bench.py        # SWE-bench runner
│       ├── tb_agents.py        # terminal-bench agent base class
│       └── test_signals.py     # test output signal extraction
│
└── experiment/                 # dev analytics tools (no tests)
    ├── trace/                  # HTML/SVG trace renderers, CLI viewer
    ├── compare_runs.py
    ├── extract_excerpts.py
    ├── merge_runs.py
    ├── migrate_runs.py
    ├── notebook.py
    ├── seed_corpus.py
    └── trace_report.py

benchmarks/                     # domain adapters + task fixtures
scripts/                        # operator scripts (run_bench.sh, trace_report.py, ...)
tests/                          # pytest suite (65 tests, no Docker required)
```

### Tool file pattern

Each tool in `capability/tools/` is self-contained:

```python
_SCHEMA = {...}                   # OpenAI tool schema dict

def create_<tool>_capability(workdir: Path) -> Capability:
    return Capability(name="<tool>", schema=_SCHEMA, handler=partial(_handle, workdir))

def _handle(workdir: Path, args: dict, ...) -> ToolResult:
    ...
```

---

## Domain-Agnostic Rule

System prompts, the engine, and all schemas must contain no domain-specific language.

| Wrong | Right |
|---|---|
| "call str_replace to edit the file" | "apply the change" |
| "read the file at path X" | "inspect the target" |
| Checking `bash/rg/read` as read-only | Use `inv.durable_change` |
| "FOUND_IN / NOT_FOUND" in prompts | Knowledge generically |

Domain knowledge enters **only** through:
- Task adapter code (`app/swebench/swe_bench.py`)
- `PerformerTaskSchema.context` (injected per task)
- `contract.representations` (probed by Representer)
