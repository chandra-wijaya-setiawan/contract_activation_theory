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
    current_tier = 0

    for hypothesis_n in range(MAX_HYPOTHESES):

        hypothesis = call_stipulator(
            obligation, representations, knowledge, prior_attempts
        )
        # → HypothesisSchema: where, what, predicts, basis, start_from

        checkpointer.restore(hypothesis.start_from)   # "base" or "hK"

        result = perform(hypothesis, llm=tier_llm[current_tier])
        # → verdict: completed | exhausted | give_up

        if result.any_durable_change:
            checkpointer.snapshot(f"h{hypothesis_n+1}")

        if oracle_passes(contract, state):
            return FULFILLED

        knowledge.update(result, oracle_failure)

        if stuck(result, prior_attempts, current_tier):
            if current_tier < max_tier:
                current_tier += 1
            else:
                break

    return FAILED
```

**No plan tree. No stall counter. No Resolver.**

The only stateful decisions per cycle are:
1. Which checkpoint to branch from (`hypothesis.start_from` — chosen by the Stipulator)
2. Which model tier to execute on (`current_tier` — chosen by the escalation ladder)

---

## Inner Loop (Performer)

The Performer receives a narrow task: one hypothesis, one oracle command, compressed context. It has no access to prior message history.

```
perform(task: PerformerTaskSchema, max_attempts):
    messages = [system_prompt, task_prompt]

    for attempt in 1..max_attempts:
        step = llm(messages, tools=all_tools + [done, give_up])
        messages += [step, tool_results]

        if step.intent == "done":    return verdict=completed
        if step.intent == "give_up": return verdict=give_up
        if durable_change and oracle_passes(): return verdict=completed

    return verdict=exhausted
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

Execution starts on the cheapest model and escalates only when stuck. The Stipulator (planning) uses a separate fixed model so hypothesis quality is independent of the execution tier.

```python
_ESCALATION_TIERS = [
    (0, "CAT_MODEL",               None),                  # T0 default (flash)
    (2, "CAT_ESCALATION_MODEL_T1", "...mimo-v2.5"),        # T1
    (4, "CAT_ESCALATION_MODEL_T2", "...claude-haiku-4-5"), # T2 strongest
]
# Planning: CAT_PLANNING_MODEL (falls back to CAT_MODEL)
```

**Escalation trigger:** Performer returned `give_up` or `exhausted`, **or** there have been 2 oracle failures at the current tier. At max tier, the task fails.

---

## Schemas

All Pydantic data shapes end in `Schema`. Runtime dataclasses (no validation) are plain nouns. Operations end in `Op`.

### Data flow between roles

```
Stipulator output → HypothesisSchema
    location:   str        # where to act
    change:     str        # what to do
    predicts:   str        # verifiable outcome
    basis:      str        # why this hypothesis
    start_from: str        # "base" | "hK"

Performer input  → PerformerTaskSchema
    obligation: str
    hypothesis: HypothesisSchema
    oracle:     str        # test command
    context:    str        # compressed knowledge
    max_attempts: int = 5

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

Attestor output  → AttestorV3Schema
    verdict:   new_hypothesis | give_up
    reason:    str
    diagnosis: str
```

### Naming conventions

| Category | Rule | Examples |
|---|---|---|
| Roles (LLM-backed components) | noun, no suffix | `Stipulator`, `Performer`, `Attestor`, `Activator` |
| Schemas (Pydantic, validated) | `...Schema` suffix | `HypothesisSchema`, `KnowledgeSchema` |
| Runtime objects (dataclasses) | plain noun | `Contract`, `State`, `Cost`, `Criterion` |
| Tree-edit operations | `...Op` suffix | `CommitOp`, `RefineOp` |

---

## Directory Structure

```
src/
├── cat/                        # core framework (domain-agnostic)
│   ├── contract/
│   │   ├── model.py            # Contract, State, Cost, Constraints, Criterion, Evidence
│   │   └── schemas.py          # HypothesisSchema, PerformerTaskSchema, KnowledgeSchema, …
│   ├── engine/
│   │   ├── activator.py        # HypothesisLoopActivator — the outer loop
│   │   ├── base.py             # BaseActivator — cost, budget, criteria, admission, windowing
│   │   ├── checkpoint.py       # Checkpointer protocol + NullCheckpointer
│   │   └── checkpoint_git.py   # GitCheckpointer (git plumbing, no docker import)
│   ├── llm/
│   │   ├── client.py           # LLM (OpenAI SDK wrapper), LLMResult, call_structured
│   │   ├── kernel.py           # LLMKernel — intelligence-addressed, self-healing
│   │   ├── router.py           # model → backend dispatch
│   │   ├── adapters.py         # per-model request/response shaping (Kimi, etc.)
│   │   ├── card.py             # static model metadata
│   │   ├── stipulator.py       # STIPULATOR_SYSTEM_PROMPT + call_stipulator
│   │   ├── attestor.py         # ATTESTOR_SYSTEM_PROMPT + call_attestor
│   │   └── performer_plan.py   # continuation plan generation
│   ├── capability/
│   │   ├── registry.py         # CapabilityRegistry — register, invoke, schema listing
│   │   ├── full_registry.py    # build_default_registry, build_docker_registry
│   │   ├── git_checkpoint.py   # build_git_checkpointer (docker exec bridge)
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
│   ├── trace.py                # Recorder, TraceEvent, JSONL + parquet emit
│   ├── representer.py          # Representer — probes contract.representations
│   ├── utils/                  # time, hashing, text helpers
│   └── activator.py            # public alias: Activator = HypothesisLoopActivator
│
├── adapters/                   # domain-specific task adapters
│   ├── swe_bench.py            # SWE-bench (placeholder; benchmarks/swe_bench.py is live)
│   └── terminal_bench.py       # terminal-bench adapter (stub)
│
├── tooling/
│   └── reports/
│       └── run_report.py       # KPI report generator
│
└── cli/
    └── essay_experiment.py     # essay-writing smoke test

benchmarks/
└── swe_bench.py                # live SWE-bench adapter (TbSweAgent)
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
- Task adapter code (`benchmarks/swe_bench.py`)
- `PerformerTaskSchema.context` (injected per task)
- `contract.representations` (probed by Representer)
