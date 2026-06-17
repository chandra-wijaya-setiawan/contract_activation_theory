# CAT Refactor Plan v2

> Date: 2026-06-16 (updated 2026-06-17)
> Goal: Clean, modular Python → ready for Rust rewrite
> Strategy: Scaffold first, move code, delete old
> References: pi (earendil-works), hermes-agent (nousresearch), claude-code-leak

---

## Current Status

| Phase | Status | Notes |
|-------|--------|-------|
| Phase 0: Scaffold | ✅ DONE | Directory structure + `__init__.py` files created |
| Phase 1: Extract leaf modules | ✅ DONE | trace.py, utils/, contract/model.py, contract/schemas.py |
| Phase 2: Extract LLM layer | ✅ DONE | stipulator.py, attestor.py, performer_plan.py fully implemented |
| Phase 3: Extract capability | ✅ DONE | 17 tool files + `full_registry.py` split; `capability/registry.py` real impl |
| Phase 4: Extract engine | ✅ DONE | `HypothesisLoopActivator` + `BaseActivator` fully implemented |
| Phase 5: Create adapters | ✅ DONE | `adapters/` structure in place |
| Phase 6: Create tooling | ✅ DONE | `tooling/` structure in place |
| Phase 7: Create CLI | ⚠️ PARTIAL | `cli/` has scripts but no structured CLI framework |
| Phase 8: Update imports | ✅ DONE | 68 tests pass; backward-compat shims in place (incl. `cat.test_signals`) |
| Phase 9: Delete old code | ❌ BLOCKED | `_legacy/` (30 files) still present; wait for SWE run confirmation |
| Phase 10: Rename + swap | ❌ BLOCKED | Wait for Phase 9 |

**Shims in `src/cat/` (backward-compat redirects):**
- `activator.py` → `cat.engine.activator.HypothesisLoopActivator`
- `activator_v3.py` → same
- `capabilities.py` → `cat.capability.full_registry`
- `checkpoint.py` → `cat.engine.checkpoint` + `cat.capability.git_checkpoint`
- `contracts.py` → `cat.contract.model`
- `schemas.py` → `cat.contract.schemas`
- `tb_agents.py` → `cat._legacy.tb_agents`
- `test_signals.py` → `cat._legacy.test_signals`

**Next priority:** Confirm SWE benchmark end-to-end, then Phase 9 delete `_legacy/` (30 files).

---

## 0. Reference Architecture Analysis

### 0.1 pi (earendil-works/pi) — TypeScript monorepo

**Structure:**
```
packages/
├── agent/          # Core agent loop (framework-agnostic)
│   ├── src/
│   │   ├── agent.ts           # Agent class — loop orchestration
│   │   ├── agent-loop.ts      # runAgentLoop / runAgentLoopContinue
│   │   ├── types.ts           # AgentState, AgentEvent, AgentTool
│   │   └── harness/           # System prompt, session, compaction
│   └── test/
├── ai/             # LLM provider layer
│   ├── src/
│   │   ├── models.ts          # Model definitions
│   │   ├── providers/         # OpenAI, Anthropic, Google, etc.
│   │   ├── stream.ts          # Streaming abstraction
│   │   └── types.ts
│   └── test/
├── coding-agent/   # The "app" — interactive coding agent
│   ├── src/
│   │   ├── core/
│   │   │   ├── agent-session.ts    # AgentSession — lifecycle + state
│   │   │   ├── tools/              # Tool implementations
│   │   │   ├── compaction/         # Context compaction
│   │   │   ├── extensions/         # Plugin system
│   │   │   ├── prompt-templates.ts
│   │   │   └── system-prompt.ts
│   │   ├── modes/                  # Interactive, print, RPC
│   │   ├── cli/                    # CLI entry points
│   │   └── config.ts
│   └── test/
└── tui/            # Terminal UI ( Ink-based)
```

**Key design patterns:**
1. **Clean separation: `agent/` (loop) → `ai/` (providers) → `coding-agent/` (app)**
2. **Agent class is ~500 lines** — delegates to `agent-loop.ts` for the actual loop
3. **Tools are individual files** in `tools/` — each exports `createXxxTool()`
4. **Provider abstraction** — `pi-ai` package with per-provider backends
5. **Session management** is a separate concern from the loop
6. **Compaction** is a module, not inline in the loop

### 0.2 hermes-agent (nousresearch) — Python agent framework

**Structure:**
```
hermes-agent/
├── run_agent.py        # AIAgent class — the main agent loop (~5,400 lines)
├── cli.py              # Interactive CLI (~13,900 lines)
├── toolsets.py         # Tool grouping system
├── model_tools.py      # Tool registry + dispatch
├── tools/              # Individual tool implementations
│   ├── registry.py     # Tool discovery + registration
│   ├── terminal.py
│   ├── read_file.py
│   ├── write_file.py
│   ├── patch.py
│   ├── web_search.py
│   ├── browser_*.py
│   └── ...
├── agent/              # Agent internals
│   ├── lsp/            # Language server protocol
│   ├── transports/     # Communication transports
│   └── secret_sources/ # Secret management
├── hermes_cli/         # CLI framework
│   ├── cli_agent_setup_mixin.py
│   ├── cli_commands_mixin.py
│   └── subcommands/
├── plugins/            # Plugin system (60+ plugins)
├── skills/             # Skill system (100+ skills)
├── gateway/            # API gateway
├── cron/               # Cron job system
└── ui-tui/            # Terminal UI
```

**Key design patterns:**
1. **Tool registry pattern** — tools self-register via `registry.register()`
2. **Toolsets** — groups of tools for different scenarios (research, terminal, full_stack)
3. **Mixin-based CLI** — `CLIAgentSetupMixin` + `CLICommandsMixin` composed into main CLI
4. **Plugin system** — each plugin is a directory with its own code
5. **Skills** — markdown-defined skills loaded at runtime
6. **run_agent.py is the library entry point** — `AIAgent` class with clean API

### 0.3 claude-code-leak — TypeScript (Anthropic's Claude Code)

**Structure:**
```
src/
├── tools/              # All tools as individual files
│   ├── BashTool/       # BashTool.tsx, prompt.ts, utils.ts
│   ├── FileEditTool/
│   ├── FileReadTool/
│   ├── FileWriteTool/
│   ├── AgentTool/      # Sub-agent spawning
│   ├── MCPTool/
│   └── ...
├── services/           # Infrastructure services
│   ├── api/            # Claude API client
│   ├── compact/        # Context compaction
│   ├── mcp/            # MCP protocol
│   ├── lsp/            # Language server
│   └── analytics/
├── state/              # App state management
├── tasks/              # Background tasks
├── skills/             # Skill system
├── utils/              # Utilities (100+ files)
└── types/              # Type definitions
```

**Key design patterns:**
1. **Each tool is a directory** — `ToolNameTool.tsx` + `prompt.ts` + `utils.ts`
2. **Services layer** — API, compaction, MCP are separate services
3. **State management** — centralized `AppState` with selectors
4. **Tool prompts separated** — each tool has its own prompt template
5. **Streaming tool executor** — `StreamingToolExecutor` for live output

---

## 1. Distilled Design Principles for CAT

### What to borrow from each:

| Pattern | Source | Why |
|---------|--------|-----|
| **Separate `engine/` from `llm/` from `adapters/`** | pi | Cleanest separation of concerns |
| **Tool registry with self-registration** | hermes | Decoupled tool discovery |
| **Each tool in its own file** | claude-code | Maintainability at scale |
| **Provider abstraction** | pi | Multi-provider support |
| **Mixin-based CLI** | hermes | Composable CLI features |
| **Compaction as a module** | pi | Not inline in loop |
| **Session management separate** | pi | Clean lifecycle management |
| **Toolsets for different scenarios** | hermes | SWE-bench vs terminal-bench vs chat |
| **Skills as markdown** | hermes | User-extensible |
| **Services layer** | claude-code | Infrastructure separation |

### What CAT-specific patterns to keep:

| Pattern | Why |
|---------|-----|
| **Contract lifecycle** | Core differentiator — not in any reference |
| **Hypothesis loop (v3)** | Unique to CAT |
| **LLMKernel with self-healing** | Unique to CAT |
| **Representer (probes)** | Unique to CAT |
| **Checkpoint branching** | Unique to CAT |
| **Domain-agnostic core** | CAT's design goal |

---

## 2. Target Directory Structure

```
contract_activation_theory/
│
├── cat/                          # CORE AGENT FRAMEWORK (domain-agnostic)
│   ├── __init__.py               # Public surface: Contract, Activator, etc.
│   │
│   ├── engine/                   # Agent loop engine
│   │   ├── __init__.py
│   │   ├── base.py               # BaseActivator — scaffolding (charge, transition, finalize)
│   │   ├── activator.py          # HypothesisLoopActivator (v3, standalone)
│   │   ├── checkpoint.py         # Checkpointer protocol + NullCheckpointer
│   │   └── knowledge.py          # KnowledgeSchema accumulation
│   │
│   ├── llm/                      # LLM layer (provider-agnostic)
│   │   ├── __init__.py
│   │   ├── client.py             # LLM client (OpenAI wrapper, token harvest)
│   │   ├── kernel.py             # LLMKernel (self-healing, intelligence-addressed)
│   │   ├── stipulator.py         # Stipulator prompt + call
│   │   ├── attestor.py           # Attestor prompt + call
│   │   ├── performer_plan.py     # Continuation plan generation
│   │   ├── adapters.py           # Per-model adapters (Kimi, etc.)
│   │   ├── router.py             # Model→backend dispatch
│   │   └── card.py               # Static model metadata
│   │
│   ├── contract/                 # Contract lifecycle
│   │   ├── __init__.py
│   │   ├── model.py              # Contract, Criterion, State, Constraints, Cost
│   │   ├── schemas.py            # All Pydantic schemas (v3 only)
│   │   └── admission.py          # Admission gate logic
│   │
│   ├── capability/               # Tool system
│   │   ├── __init__.py
│   │   ├── registry.py           # CapabilityRegistry, Capability, ToolResult
│   │   ├── tools/                # Individual tool files (one per tool)
│   │   │   ├── __init__.py
│   │   │   ├── bash.py
│   │   │   ├── read.py
│   │   │   ├── write.py
│   │   │   ├── str_replace.py
│   │   │   ├── multi_edit.py
│   │   │   ├── apply_patch.py
│   │   │   ├── rg.py
│   │   │   ├── fd.py
│   │   │   ├── goto_def.py
│   │   │   ├── find_refs.py
│   │   │   ├── outline.py
│   │   │   ├── git_diff.py
│   │   │   ├── git_status.py
│   │   │   ├── done.py
│   │   │   └── give_up.py
│   │   └── git_checkpoint.py     # GitCheckpointer
│   │
│   ├── representer.py            # Environment probes (Step 2)
│   ├── trace.py                  # TraceEvent + Recorder
│   └── utils/                    # Shared utilities
│       ├── __init__.py
│       ├── serialization.py      # extract_json_object, estimate_usd
│       └── time.py               # utcnow_iso
│
├── adapters/                     # DOMAIN ADAPTERS (benchmark-specific)
│   ├── __init__.py
│   ├── swe_bench.py              # SWE-bench oracle config + TbSweAgent
│   ├── terminal_bench.py         # TbCatAgent, TbReactAgent
│   ├── mini_swe.py               # MiniSweOpenRouter
│   ├── tasks.py                  # TaskSpec, BudgetCaps, Outcome
│   └── synthetic.py              # Synthetic task definitions
│
├── tooling/                      # ANALYSIS & CLI TOOLS
│   ├── __init__.py
│   ├── reports/
│   │   ├── __init__.py
│   │   ├── run_report.py         # KPI report generator
│   │   ├── attribution.py        # Layer attribution
│   │   └── metrics.py            # RunMetrics
│   ├── trace/
│   │   ├── __init__.py
│   │   ├── view.py               # CLI trace viewer
│   │   ├── html.py               # HTML trace renderer
│   │   └── svg.py                # DAG SVG renderer
│   └── cli.py                    # Chat CLI mode interface
│
├── cli/                          # ENTRY POINTS
│   ├── __init__.py
│   ├── chat.py                   # Interactive chat mode
│   ├── bench.py                  # Benchmark runner
│   └── report.py                 # Report generator
│
├── tests/                        # Test suite (mirrors cat/ structure)
├── scripts/                      # Analysis/migration scripts
├── docs/                         # Design docs, PRDs
├── data/                         # Run outputs, reports
│
├── DESIGN.md
├── design.v3.md
├── plan.v3.md
├── CLAUDE.md
└── refactor_plan.md              # This file
```

---

## 3. Module Dependency Rules

```
┌─────────────────────────────────────────────────────────────┐
│                     cli/  (entry points)                     │
│                  chat.py, bench.py, report.py                │
└──────────┬──────────────────────────┬───────────────────────┘
           │                          │
           ▼                          ▼
┌──────────────────┐      ┌──────────────────────────────────┐
│   adapters/      │      │           tooling/               │
│ (domain-specific)│      │    (analysis & CLI tools)        │
└────────┬─────────┘      └──────────────┬───────────────────┘
         │                               │
         │  both depend on               │
         ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                        cat/  (core)                          │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  engine/                                            │    │
│  │  base.py ← contract/ (Contract, State)              │    │
│  │  activator.py ← llm/ (LLMKernel, Stipulator, ...)  │    │
│  │  checkpoint.py ← capability/ (Checkpointer)         │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  llm/                                               │    │
│  │  client.py ← utils/ (serialization)                 │    │
│  │  kernel.py ← client.py                              │    │
│  │  stipulator.py ← kernel.py, contract/schemas.py      │    │
│  │  attestor.py ← kernel.py, contract/schemas.py       │    │
│  │  performer_plan.py ← kernel.py                      │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  contract/                                          │    │
│  │  model.py ← (nothing — leaf module)                 │    │
│  │  schemas.py ← (nothing — leaf module)               │    │
│  │  admission.py ← model.py                            │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  capability/                                        │    │
│  │  registry.py ← contract/model.py (State type)       │    │
│  │  tools/*.py ← registry.py                           │    │
│  │  git_checkpoint.py ← utils/                        │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  representer.py ← contract/model.py, capability/registry.py │
│  trace.py ← (nothing — leaf module)                         │
│  utils/ ← (nothing — leaf module)                           │
└─────────────────────────────────────────────────────────────┘
```

**Rules:**
1. `engine/` depends on `llm/`, `contract/`, `capability/`, `utils/`
2. `llm/` depends on `utils/` and `contract/schemas.py` only
3. `contract/` depends on `utils/` only
4. `capability/` depends on `contract/model.py` (State type) and `utils/`
5. `adapters/` depends on all of `cat/`
6. `tooling/` depends on `cat/` (reads traces, computes metrics)
7. `cli/` depends on `cat/`, `adapters/`, `tooling/`
8. **NO circular dependencies**
9. **NO domain-specific code in `cat/`**
10. **NO analysis code in `cat/`**

---

## 4. Class & Method Inventory

### 4.1 `cat/engine/` — Loop Engine

#### `base.py` — `BaseActivator`
The scaffolding that all activators inherit. Extracted from `_legacy/activator.py`.

```
BaseActivator
├── __init__(llm, registry, recorder, max_iterations, criterion_check_interval, window_size)
├── run(contract, state) -> ActivatorRun              # [abstract]
├── _charge_llm(contract, result) -> None
├── _charge_tool(contract, result) -> None
├── _transition(contract, to, reason, run) -> None
├── _record_state(contract, state, run) -> None
├── _finalize(contract, run) -> None
├── _check_budget(contract, state, run) -> bool
├── _verify_criteria(contract, state, run) -> bool
├── _maybe_inject_oracle_criterion(contract) -> None
├── _build_oracle_criterion(contract, command) -> Criterion
├── _init_executor_messages(contract, run) -> None
├── _admit(contract, state, run) -> AdmissionDecision
├── _apply_admission_failure(contract, run, decision) -> None
├── _record_admission(contract, decision, run) -> None
└── _prune_to_window(contract, run, turn_msgs) -> None
```

#### `activator.py` — `HypothesisLoopActivator`
The v3 hypothesis loop. Standalone — inherits ONLY from BaseActivator.

```
HypothesisLoopActivator(BaseActivator)
├── __init__(llm, registry, recorder, checkpointer, ...)
├── _llm_for_model(model) -> LLM
├── _charge_kernel(contract, kernel_result) -> None
├── _cp_snapshot(label) -> bool
├── _cp_restore(label) -> bool
├── _cp_labels() -> list[str]
├── _cp_log_error(op, label, exc) -> None
│
├── run(contract, state) -> ActivatorRun              # v3 outer loop
├── _admit(contract, state, run)                      # skip empty-plan check
├── _perform_hypothesis(contract, state, run, task, ...) -> PerformerOutcome
├── _extract_oracle(contract) -> str
├── _build_prior_results(knowledge, prior_attempts) -> str
├── _build_context(knowledge) -> str
├── _update_knowledge(knowledge, result, oracle_cmd) -> None
├── _format_plan(plan) -> str
└── _get_last_test_output(run) -> str
```

#### `checkpoint.py` — Checkpointer protocol
```
Checkpointer (Protocol)
├── snapshot(label: str) -> bool
├── restore(label: str) -> bool
└── labels() -> list[str]

NullCheckpointer (Checkpointer) — no-op
```

#### `knowledge.py` — Knowledge accumulation
```
KnowledgeAccumulator
├── __init__()
├── update(result, oracle_failure) -> None
├── build_context() -> str
├── build_prior_results(prior_attempts) -> str
└── to_schema() -> KnowledgeSchema
```

### 4.2 `cat/llm/` — LLM Layer

#### `client.py` — `LLM`
```
LLM
├── __init__(model, api_key, base_url, recorder, parent_event_id)
├── chat(messages, tools, tool_choice, response_format, role_tag, temperature, max_tokens, parent_event_id) -> LLMResult
└── _harvest_usage(result) -> None

LLMResult (dataclass)
├── text, tool_calls, finish_reason, tokens_in, tokens_out, usd, model, raw

extract_json_object(text) -> Optional[dict]
estimate_usd(model, tokens_in, tokens_out) -> float
```

#### `kernel.py` — `LLMKernel`
```
LLMKernel
├── __init__(models, mode, schema, temperature, intelligence, role_tag, recorder, client_factory)
├── run(messages, tools, parent_event_id) -> KernelResult
├── set_intelligence(x) -> None
├── increase_intelligence(d) -> None
└── current_model() -> str

KernelResult (dataclass)
├── ok, value, text, tool_calls, model, intelligence, attempts, raw, failure, tokens_in, tokens_out, usd
```

#### `stipulator.py` — Stipulator role
```
STIPULATOR_V3_SYSTEM_PROMPT: str

call_stipulator_v3(llm, obligation, representations, knowledge, prior_attempts, parent_event_id)
    -> tuple[HypothesisSchema, LLMResult]

build_stipulator_messages(obligation, representations, knowledge, prior_attempts)
    -> list[dict]
```

#### `attestor.py` — Attestor role
```
ATTESTOR_V3_SYSTEM_PROMPT: str

call_attestor_v3(llm, result, knowledge, prior_attempts, parent_event_id)
    -> tuple[AttestorV3Schema, LLMResult]
```

#### `performer_plan.py` — Continuation plan
```
call_performer_plan(llm, task, prior_results, parent_event_id)
    -> tuple[Optional[PerformerPlanSchema], LLMResult]

build_performer_plan_messages(task, prior_results) -> list[dict]
```

#### `adapters.py` — Per-model behavior
```
LLMModelAdapter
├── generate_schema(router, model_id, messages, schema) -> AdapterResult
└── generate_text(router, model_id, messages, tools) -> AdapterResult

LLMModelAdapterKimiBase(LLMModelAdapter)
LLMModelAdapterKimi26(LLMModelAdapterKimiBase)

AdapterResult (dataclass)
```

#### `router.py` — Model dispatch
```
LLMRouter
├── __init__(recorder, client_factory)
├── dispatch(model_id, **kwargs) -> LLMResult
└── _client(model_id) -> LLM
```

#### `card.py` — Model metadata
```
LLMModelCard (frozen dataclass)
card_for(model_id) -> LLMModelCard
```

### 4.3 `cat/contract/` — Contract Lifecycle

#### `model.py` — Core types
```
ContractState (Enum): LATENT | ELIGIBLE | ACTIVE | SUSPENDED | FULFILLED | FAILED | EXPIRED
ContractPolicy (dataclass)
AdmissionDecision (dataclass)
Contract (dataclass)
├── id, obligation, criteria, representations, constraints, conditions, policy, state, cost, grant, ...
Criterion (dataclass)
├── id, description, predicate, is_oracle, last_evidence, satisfied
Condition (dataclass)
Constraints (dataclass)
├── max_tokens_total, max_tool_calls, max_llm_calls, max_usd, max_wall_s, deadline_ts
Cost (dataclass)
├── tokens_in, tokens_out, tool_calls, llm_calls, usd, wall_s
State (dataclass)
├── workdir, kv, last_tool_result
Representation (dataclass)
├── key, value, evidence
Evidence (dataclass)
├── summary, detail
```

#### `schemas.py` — All Pydantic schemas
```
HypothesisSchema (BaseModel)
├── location, change, predicts, basis, start_from

PerformerTaskSchema (BaseModel)
├── obligation, hypothesis, oracle, context, max_attempts

PerformerPlanSchema (BaseModel)
├── current_state, steps, next_step, confidence

PerformerStep (BaseModel)
├── action, rationale, status

KnowledgeSchema (BaseModel)
├── findings, failed_hypotheses, oracle_failures

AttestorV3Schema (BaseModel)
├── verdict, reason, diagnosis

InvocationSchema (BaseModel)
├── tool_name, args, reason, attempt, succeeded, execution_status, output_text, error, durable_change, files_modified

OutcomeSchema (BaseModel)
├── text_response, invocations, finish_reason, verdict, performer_reason, any_tool_called, any_durable_change, any_tool_errored

ExecutionSchema (BaseModel)
├── description, intent, suggested_tool, suggested_args, expects

ExpectsSchema (BaseModel)
├── durable_change, files_modified, verifier_outcome, result
```

#### `admission.py` — Admission gate
```
check_admission(contract, state, registry, run) -> AdmissionDecision
# Extracted from base.py _admit() for reuse and testability
```

### 4.4 `cat/capability/` — Tool System

**Design decision (2026-06-17):** Single file per tool — hermes-style, NOT claude-code directory-per-tool.

Rationale: Python tools have no React components or large prompt templates that need separation.
The tool description IS the prompt and fits inline. Directories add boilerplate with no gain.
Shared helpers (jedi, workdir resolution, truncation) go in `tools/_helpers.py`.

#### `registry.py` — Tool registry
```
ToolResult (dataclass)
├── tool, is_error, output, detail, wall_s

Capability (dataclass)
├── name, description, json_schema, handler

CapabilityRegistry
├── register(cap) -> None
├── get(name) -> Optional[Capability]
├── names() -> list[str]
├── tool_schemas(only) -> list[dict]
└── invoke(name, args, state, ...) -> ToolResult
```

#### `tools/` — One file per tool
Each file exports one `create_<name>_capability() -> Capability` factory.
Schema, handler, and local helpers are all in the same file.

```
tools/
├── __init__.py           # exports create_default_toolset(), create_docker_toolset()
├── _helpers.py           # shared: _resolve_inside_workdir, _truncate, _jedi helpers
├── bash.py               # _SCHEMA + create_bash_capability() + _handle()
├── read.py
├── write.py
├── list.py
├── str_replace.py
├── multi_edit.py
├── apply_patch.py
├── rg.py
├── fd.py
├── outline.py            # uses _helpers._jedi_script()
├── goto_def.py           # uses _helpers._jedi_script()
├── find_refs.py          # uses _helpers._jedi_script()
├── git_diff.py
├── git_status.py
├── done.py
└── give_up.py
```

```python
# tools/bash.py — canonical pattern
_SCHEMA = {
    "name": "bash",
    "description": "Run a shell command in the working directory. ...",
    "parameters": {"type": "object", "properties": {...}, "required": ["command"]},
}

def create_bash_capability() -> Capability:
    return Capability(name="bash", description=_SCHEMA["description"],
                      json_schema=_SCHEMA, handler=_handle)

def _handle(args: dict, state: State) -> ToolResult: ...
```

#### `git_checkpoint.py` — Git-backed checkpointer (CANONICAL location)
```
GitCheckpointer (Checkpointer)
├── __init__(exec_fn, workdir)
├── snapshot(label) -> bool
├── restore(label) -> bool
└── labels() -> list[str]

build_git_checkpointer(container, workdir) -> GitCheckpointer
```
Note: `engine/checkpoint_git.py` is the CANONICAL full implementation.
`capability/git_checkpoint.py` is a stub — DELETE it, move logic here.

### 4.5 `adapters/` — Domain Adapters

#### `swe_bench.py` — SWE-bench
```
TbSweAgent(BaseAgent)
├── __init__(model_name, ...)
├── run(task) -> AgentResult
└── _build_oracle(config) -> Callable

make_agent(model_name, ...) -> TbSweAgent
```

#### `terminal_bench.py` — Terminal-bench
```
TbCatAgent(BaseAgent)
TbReactAgent(BaseAgent)
```

#### `tasks.py` — Task schemas
```
TaskSpec, BudgetCaps, OracleSpec, Outcome
```

### 4.6 `tooling/` — Analysis & CLI Tools

#### `reports/run_report.py`
```
TaskReport, RunReport
generate_report(run_dir, output_dir) -> None
```

#### `reports/attribution.py`
```
Attribution, attribute_task, attribute_task_llm, attribute_task_heuristic
```

#### `reports/metrics.py`
```
RunMetrics, AggregateMetrics, compare
```

#### `trace/view.py`
```
# CLI: python -m tooling.trace.view <path/to/run.jsonl>
```

#### `trace/html.py`
```
render_trace_html(events) -> str
```

#### `trace/svg.py`
```
render_dag_svg(dag) -> str
```

### 4.7 `cli/` — Entry Points

#### `chat.py` — Interactive chat mode
```
# python -m cli.chat --model <model> --workdir <path>
# Interactive REPL: user types task → CAT runs → shows result
```

#### `bench.py` — Benchmark runner
```
# python -m cli.bench --bench swe-bench --tasks <list> --model <model>
```

#### `report.py` — Report generator
```
# python -m cli.report <run_dir>
```

---

## 5. Refactor Phases

### Phase 0: Scaffold (NO code moving)

Create empty directory structure with `__init__.py` files and docstrings.
Establish module boundaries. **Nothing moves yet.**

**New directories to create:**
```
cat/engine/__init__.py
cat/llm/__init__.py
cat/contract/__init__.py
cat/capability/__init__.py
cat/capability/tools/__init__.py
cat/utils/__init__.py
adapters/__init__.py
tooling/__init__.py
tooling/reports/__init__.py
tooling/trace/__init__.py
cli/__init__.py
```

**Also create:** `cat_archive/` (empty, for old code)

### Phase 1: Extract leaf modules (zero risk)

Move files with no upstream dependencies:

| Source | Target | Notes |
|--------|--------|-------|
| `cat/trace.py` | `cat/trace.py` | Stays — leaf module |
| `cat/utils/time.py` (new) | Extract `utcnow_iso` | From `trace.py` |
| `cat/utils/serialization.py` (new) | Extract `extract_json_object`, `estimate_usd` | From `llm.py` |
| `cat/llm/card.py` | `cat/llm/card.py` | Already exists |
| `cat/llm/router.py` | `cat/llm/router.py` | Already exists |
| `cat/contracts.py` | `cat/contract/model.py` | Rename |
| `cat/schemas.py` | `cat/contract/schemas.py` | Move |
| `cat/admission.py` (new) | Extract from `_legacy/activator.py` | Admission logic |

### Phase 2: Extract LLM layer

Split `cat/llm.py` (1,371 lines) into focused modules:

| Source | Target |
|--------|--------|
| `cat/llm.py` → LLM, LLMResult, _PRICING, call_structured | `cat/llm/client.py` |
| `cat/llm_kernel.py` | `cat/llm/kernel.py` |
| `cat/llm.py` → call_stipulator_v3, build_stipulator_messages | `cat/llm/stipulator.py` |
| `cat/llm.py` → call_attestor_v3 | `cat/llm/attestor.py` |
| `cat/llm.py` → call_performer_plan, build_performer_plan_messages | `cat/llm/performer_plan.py` |
| `cat/llm_adapters.py` | `cat/llm/adapters.py` |
| `cat/llm_router.py` | `cat/llm/router.py` |
| `cat/llm_card.py` | `cat/llm/card.py` |

### Phase 3: Extract capability layer

Split `cat/capabilities.py` (1,783 lines):

| Source | Target |
|--------|--------|
| `cat/capabilities.py` → CapabilityRegistry, Capability, ToolResult | `cat/capability/registry.py` |
| `cat/capabilities.py` → all tool handlers | `cat/capability/tools/*.py` (one per tool) |
| `cat/capabilities.py` → build_git_checkpointer | `cat/capability/git_checkpoint.py` |
| `cat/capabilities.py` → build_docker_registry, build_tmux_registry | `cat/capability/__init__.py` |
| `cat/checkpoint.py` | `cat/engine/checkpoint.py` |

### Phase 4: Extract engine layer

Create clean engine package:

| Source | Target |
|--------|--------|
| `_legacy/activator.py` → scaffolding methods | `cat/engine/base.py` |
| `activator_v3.py` (v3 loop, standalone) | `cat/engine/activator.py` |
| `activator_v3.py` → knowledge helpers | `cat/engine/knowledge.py` |
| `activator.py` (v2) | **DELETE** — not ported |
| `_legacy/` (all v1 code) | **DELETE** — not ported |

**Critical:** `HypothesisLoopActivator` inherits from `BaseActivator` (new, clean), NOT from old v2 `Activator`.

### Phase 5: Create adapters package

Move all benchmark-specific code:

| Source | Target |
|--------|--------|
| `benchmarks/swe_bench.py` | `adapters/swe_bench.py` |
| `benchmarks/mini_swe_openrouter.py` | `adapters/mini_swe.py` |
| `cat/tb_agents.py` | `adapters/terminal_bench.py` |
| `cat/tasks.py` | `adapters/tasks.py` |
| (new) | `adapters/synthetic.py` |

### Phase 6: Create tooling package

Move all analysis/CLI tooling:

| Source | Target |
|--------|--------|
| `cat/run_report.py` | `tooling/reports/run_report.py` |
| `cat/attribution.py` | `tooling/reports/attribution.py` |
| `cat/metrics.py` | `tooling/reports/metrics.py` |
| `cat/trace_view.py` | `tooling/trace/view.py` |
| `cat/trace_html.py` | `tooling/trace/html.py` |
| `cat/dag.py` | `tooling/trace/dag.py` |
| `cat/dag_svg.py` | `tooling/trace/svg.py` |

### Phase 7: Create CLI package

New entry points:

| Target | Description |
|--------|-------------|
| `cli/chat.py` | Interactive chat REPL |
| `cli/bench.py` | Benchmark runner |
| `cli/report.py` | Report generator |

### Phase 8: Update all imports

Make every import point to new locations. Update:
- `cat/__init__.py` — re-export from new locations
- All cross-module imports in `cat/`
- All `adapters/` imports
- All `tooling/` imports
- All `cli/` imports
- All `tests/` imports
- All `scripts/` imports

### Phase 9: Delete old code

| Delete | Reason |
|--------|--------|
| `cat/activator.py` | Replaced by `cat/engine/activator.py` |
| `cat/activator_v3.py` | Replaced by `cat/engine/activator.py` |
| `cat/_legacy/` | All v1 code |
| `cat/capabilities.py` | Replaced by `cat/capability/` |
| `cat/contracts.py` | Replaced by `cat/contract/model.py` |
| `cat/llm.py` | Replaced by `cat/llm/` |
| `cat/llm_kernel.py` | Replaced by `cat/llm/kernel.py` |
| `cat/schemas.py` | Replaced by `cat/contract/schemas.py` |
| `cat/plan_tree.py` | DEAD |
| `cat/resolver.py` | DEAD |
| `cat/plan_schema.py` | DEAD |
| `cat/surprise.py` | DEAD |
| `cat/state_class.py` | DEAD |
| `cat/obligation_class.py` | DEAD |
| `cat/librarian.py` | DEAD |
| `cat/checkpoint.py` | Moved to `cat/engine/` |
| `cat/tb_agents.py` | Moved to `adapters/` |
| `cat/tasks.py` | Moved to `adapters/` |
| `cat/run_report.py` | Moved to `tooling/` |
| `cat/attribution.py` | Moved to `tooling/` |
| `cat/metrics.py` | Moved to `tooling/` |
| `cat/trace_view.py` | Moved to `tooling/` |
| `cat/trace_html.py` | Moved to `tooling/` |
| `cat/dag.py` | Moved to `tooling/` |
| `cat/dag_svg.py` | Moved to `tooling/` |
| `benchmarks/` | Moved to `adapters/` |

### Phase 10: Rename + swap

```bash
# After all code moved and tests pass:
mv cat/ cat_archive/
# New cat/ was created during phases 1-7
```

---

## 6. Effort Estimate

| Phase | Hours | Cumulative | Risk |
|-------|-------|------------|------|
| Phase 0: Scaffold | 1h | 1h | None |
| Phase 1: Extract leaf modules | 3h | 4h | Low |
| Phase 2: Extract LLM layer | 6h | 10h | Medium |
| Phase 3: Extract capability layer | 5h | 15h | Medium |
| Phase 4: Extract engine layer | 8h | 23h | **High** — v3 must be standalone |
| Phase 5: Create adapters | 3h | 26h | Low |
| Phase 6: Create tooling | 3h | 29h | Low |
| Phase 7: Create CLI | 4h | 33h | Low — new code |
| Phase 8: Update imports | 6h | 39h | **High** — everything breaks |
| Phase 9: Delete old code | 2h | 41h | Medium |
| Phase 10: Rename + swap | 1h | 42h | Low |
| **Buffer** | **8h** | **50h** | |
| **Total** | | **~50 hours (~1.5 weeks)** | |

---

## 7. Risk Mitigation

1. **Don't delete until tests pass.** Old code stays until new structure is green.
2. **Git branch per phase.** Easy rollback.
3. **Keep `cat_archive/` until Rust rewrite starts.** Don't delete history.
4. **Test after every phase.** `python -m pytest tests/` must pass.
5. **Import compatibility shims.** During transition, old import paths re-export from new locations.

---

## 8. Future-Proofing for Other Benchmarks

The `adapters/` package is designed for extension:

```
adapters/
├── __init__.py
├── swe_bench.py          # SWE-bench (existing)
├── terminal_bench.py     # Terminal-bench (existing)
├── mini_swe.py           # Mini-SWE (existing)
├── tasks.py              # Shared task schemas
├── synthetic.py          # Synthetic tasks
├── tau_bench.py          # τ-bench (future)
├── web_arena.py          # WebArena (future)
├── crm_tasks.py          # CRM tasks (future)
└── chat_cli.py           # Interactive chat (future)
```

Each adapter implements a standard interface:
```python
class BenchmarkAdapter(Protocol):
    def make_agent(self, model: str, **kwargs) -> Any: ...
    def load_tasks(self, path: str) -> list[TaskSpec]: ...
    def evaluate(self, task: TaskSpec, agent: Any) -> Outcome: ...
```

---

## 9. Chat CLI Mode Design

Inspired by hermes-agent's `cli.py` (interactive REPL) and pi's `modes/interactive/`.

```
cli/chat.py

Usage:
    python -m cli.chat --model openrouter/deepseek/deepseek-v4-flash
    python -m cli.chat --model ... --workdir /path/to/project
    python -m cli.chat --model ... --checkpoint git

Flow:
    1. Load model + workdir
    2. Print banner (model, workdir, available tools)
    3. REPL loop:
       a. Read user input (task description)
       b. Build Contract(obligation=user_input, criteria=[...])
       c. Run HypothesisLoopActivator.run(contract, state)
       d. Print result (PASS/FAIL, iterations, cost, key findings)
       e. Ask: "Another task? (y/n)"
    4. Print session summary
```

**This is NEW code** — no existing chat interface in CAT.

---

## 10. Approval Checklist

Before executing, confirm:

- [ ] New directory structure is acceptable
- [ ] Module boundaries make sense
- [ ] v3 standalone (not inheriting v2) is the right call
- [ ] `adapters/` split from core is correct
- [ ] `tooling/` split from core is correct
- [ ] Dead code list is accurate
- [ ] CLI design is acceptable
- [ ] Benchmark adapter protocol is acceptable
- [ ] Effort estimate is acceptable
- [ ] Reference patterns (pi, hermes, claude-code) are properly credited
