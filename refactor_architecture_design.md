# CAT Architecture Refactor — Agent-Contract Decoupling

## Motivation

Today `HypothesisLoopActivator` bundles four distinct concerns into one class:
- accepting a raw request and building context
- drafting a contract from that context
- admitting the contract (feasibility check)
- executing the contract (hypothesis loop)

This makes orchestration impossible without surgery. The goal is a clean seam:
**Contract = what. Agent = who. ContractExecution = what happened.**

---

## Core Model

```
Contract          latent task definition
                  obligation, criteria, representations, constraints
                  — pure data, no execution knowledge

Agent             entity that accepts, drafts, and executes contracts
                  has capabilities, a model, an execution strategy

ContractExecution result of Agent.execute_contract()
                  outcome string, cost, final state (FULFILLED/FAILED), trace

Evaluator         judges execution quality
                  Stipulator (plans), Oracle (tests), Attestor (verdicts)
                  — internal to HypothesisAgent; not part of the Agent interface
```

---

## Agent Interface

```python
class Agent(ABC):

    def accept_request(self, raw: str, context: "AgentContext") -> "Agent":
        """
        Parse intent and gather context from the environment.
        Populates self._request and self._context.
        Returns self for chaining.

        Current equivalent: Representer.run()
        """
        ...

    def draft_contract(self) -> Contract:
        """
        Produce a Contract from the accepted request.
        Sets obligation, criteria, representations, constraints.
        Returns the drafted (LATENT) Contract.

        Current equivalent: Stipulator when skip_stipulator=False,
                            or Representer llm_call when skip_stipulator=True
        """
        ...

    def activate(self) -> AdmissionDecision:
        """
        Validate the drafted contract is feasible.
        Checks: non-empty obligation, criteria present, capabilities available,
                budget not already exceeded, deadline not passed.
        Transitions contract LATENT -> ELIGIBLE on success.

        Current equivalent: BaseActivator._admit()
        """
        ...

    def execute_contract(self) -> "ContractExecution":
        """
        Run the contract. The execution strategy is agent-specific:
          HypothesisAgent    -> Stipulator -> Performer loop -> Oracle -> Attestor
          OrchestrationAgent -> draft sub-contracts -> dispatch to child agents
          PrimitiveAgent     -> single tool call -> done

        Current equivalent: HypothesisLoopActivator.run()
        """
        ...
```

### Fluent usage (chat CLI)

```python
agent = AgentFactory.for_skill("chat")
result = (
    agent
    .accept_request(raw_input, session.context)
    .draft_contract()   # returns self after setting self._contract
)
decision = agent.activate()
if decision.ok:
    execution = agent.execute_contract()
    session.context.append(execution)
    print(execution.outcome)
```

---

## ContractExecution (replaces ActivatorRun in the public API)

```python
@dataclass
class ContractExecution:
    contract:   Contract
    outcome:    str                    # done() reason or failure message
    state:      ContractState          # FULFILLED or FAILED
    cost:       Cost
    trace_id:   str                    # points to Recorder trajectory
    depth:      int                    # nesting depth (0 = top-level)
```

`ActivatorRun` stays as internal mutable scratch state inside `HypothesisAgent`.
`ContractExecution` is the immutable result returned to callers.

---

## Agent Taxonomy

### HypothesisAgent  (today's HypothesisLoopActivator)

```
accept_request  -> Representer.run(mode=skill.representer_mode)
draft_contract  -> Stipulator call (or skip if skip_stipulator=True)
activate        -> BaseActivator._admit()
execute_contract-> hypothesis loop:
                     for each hypothesis:
                       Performer inner loop (tool calls)
                       Oracle check
                       Attestor verdict -> continue / give_up / done
```

File: `cat/agent/hypothesis.py`
Rename: `HypothesisLoopActivator` -> `HypothesisAgent(Agent)`

### No OrchestrationAgent

Orchestration is the **consumer's responsibility**, not CAT's.

CAT provides a library of agents with a clean interface. The consumer decides
how to chain them:

```
cat-cli          → interactive session, one agent turn at a time
benchmark harness→ parallel fan-out over a task list
ticketing system → pulls tickets, calls swe_agent.execute_contract() per ticket
CI pipeline      → runs verify_agent on each PR commit
LangGraph/CrewAI → uses CAT agents as nodes / roles in a larger graph
```

Adding an `OrchestrationAgent` would mean CAT re-inventing topology logic
(sequence, parallel, fan-out, retry) that the consumer already has — and doing
it worse, since the consumer knows its own task structure.

**Rule:** if the orchestration logic is specific to one domain or deployment,
it lives outside CAT. CAT stays focused on being a composable agent library.

### PrimitiveAgent  (today's Capability — YAML-defined)

```
accept_request  -> parse obligation as a single invocation
draft_contract  -> trivial contract (obligation = tool call, no criteria)
activate        -> always ok
execute_contract-> invoke handler (Python / shell / TS / exe) -> ContractExecution
```

No hypothesis loop. No Stipulator. No Attestor.
**Defined entirely by agent.yaml + a handler file — no separate Python class hierarchy.**
The framework discovers and runs them via the same AgentRegistry as all other agents.

---

## Agent Directory Convention

Every agent lives in its own folder. One folder = one agent.

### Where agents live

```
src/cat/agent/built-in/      ← shipped with the package (pip install)
  bash/                        always available, every project
  web_search/
  chat/
  swebench/
  ...

<repo-root>/agents/           ← project-local add-ins (checked into repo)
  google_search/               available to all apps in this project
  sql_runner/
  ...

~/.cat/agents/                ← user-local add-ins (personal, not in repo)
  my_custom_agent/             available on this machine only
  ...

src/app/<app>/agents/         ← app-specific add-ins (one app only)
  patch_validator/
  ...
```

Analogy: `built-in/` = `/usr/lib/` (comes with package). `agents/` at repo root =
project plugins. `~/.cat/agents/` = personal dotfiles.

### AgentRegistry search order

```
1. explicit path passed directly
2. <repo-root>/agents/<name>/       project-local
3. ~/.cat/agents/<name>/            user-defined
4. src/cat/agent/built-in/<name>/   framework defaults
```

### Folder structure (built-in example)

```
src/cat/agent/built-in/
  bash/
    agent.yaml               ← executor: primitive
    agent.py                 ← Python handler (capability wrapper)
  web_search/
    agent.yaml
    agent.py
  workdir_explorer/
    agent.yaml
    agent.py
  chat/
    agent.yaml               ← executor: hypothesis (was skill/built-in/chat/skill.yaml)
  swebench/
    agent.yaml               ← executor: hypothesis
  task/
    agent.yaml
    oracle.sh
```

### agent.yaml format

Same format for all types — `executor` field selects strategy:

```yaml
# Primitive agent — single handler invocation, no loop
name: bash
description: "Execute a bash command in the working directory"
executor: primitive
handler: agent.py           # .py | .sh | .ts | .exe — defaults to agent.py

# Hypothesis agent — full Stipulator → Performer → Oracle loop
name: chat
description: "Conversational coding assistant"
executor: hypothesis
loop_config:
  skip_stipulator: true
  max_hypotheses: 5
representer:
  mode: llm_call
```

### Add-in handler IPC contract

For non-Python handlers (`.sh`, `.ts`, `.exe`) the framework uses the same
stdin/stdout JSON pattern as `oracle.sh` today:

```
stdin:  JSON { "obligation": "...", "representations": [...], "workdir": "..." }
stdout: JSON { "outcome": "...", "state": "fulfilled"|"failed", "cost_usd": 0.0 }
stderr: logged as trace evidence
exit 0: fulfilled, exit 1: failed
```

Zero Python required to ship a new add-in agent.

---

## AgentRegistry  (replaces SkillRegistry)

```python
class AgentRegistry:
    """Finds and loads AgentDef from agent.yaml files.

    Search order (first match wins):
      1. ./agents/<name>/         project-local
      2. ~/.cat/agents/<name>/    user-defined
      3. <package>/agent/built-in/<name>/   framework defaults
      4. <package>/agent/add-in/<name>/     framework add-ins
    """

    def load(self, name: str) -> "AgentDef": ...
    def list_available(self) -> list[str]: ...
```

`AgentFactory` picks the Python Agent class from `agent_def.executor`:

```python
_EXECUTOR_MAP = {
    "hypothesis": HypothesisAgent,
    "primitive":  PrimitiveAgent,   # generic: delegates to handler file
}

class AgentFactory:
    @classmethod
    def for_agent(cls, name: str) -> Agent:
        defn = AgentRegistry().load(name)
        cls_ = _EXECUTOR_MAP.get(defn.executor, HypothesisAgent)
        return cls_(defn)
```

**Backward compat:** `SkillRegistry` becomes an alias for `AgentRegistry`.
`skill.yaml` files are loaded transparently — `executor` defaults to `"hypothesis"`
so all existing skills work unchanged.

---

## AgentContext  (replaces ad-hoc chat_history + workspace_reps)

```python
@dataclass
class AgentContext:
    workspace_reps: list[Representation]  = field(default_factory=list)
    chat_history:   list[dict]            = field(default_factory=list)
    prior_executions: list[ContractExecution] = field(default_factory=list)
    workdir:        Path                  = field(default_factory=lambda: Path("."))
```

The Session owns one `AgentContext` for the whole session.
Each `execute_contract()` appends its `ContractExecution` to `prior_executions`.
The next `accept_request()` sees the full history.

---

## Repository Layout

```
<repo-root>/
  agents/                       ← project-local add-in agents (any language)
    google_search/
      agent.yaml
      agent.py

  src/
    cat/                        ← library — no UX opinions
      agent/
        base.py                 ← Agent abstract base (was BaseActivator)
        hypothesis.py           ← HypothesisAgent (was HypothesisLoopActivator)
        primitive.py            ← PrimitiveAgent generic handler runner
        registry.py             ← AgentRegistry (was SkillRegistry)
        factory.py              ← AgentFactory
        context.py              ← AgentContext, ContractExecution
        defn.py                 ← AgentDef, LoopConfig (was skill/__init__.py)
        built-in/               ← shipped with pip, always available
          chat/agent.yaml
          swebench/agent.yaml
          task/agent.yaml + oracle.sh
          bash/agent.yaml + agent.py
          web_search/agent.yaml + agent.py
          workdir_explorer/agent.yaml + agent.py
      contract/                 ← unchanged
      llm/                      ← unchanged
      capability/tools/         ← unchanged (bash.py, read.py, etc.)
      representer.py            ← unchanged
      activator.py              ← public alias → HypothesisAgent

    app/                        ← consumer applications
      cat-cli/
        cat_cli.py              ← was src/cat/cli/cat_cli.py
      swebench/
        swe_bench.py            ← was benchmarks/swe_bench.py
        run_bench.sh            ← was src/cli/run_bench.sh

    tooling/                    ← analysis / reporting tools (already exists)
      reports/
      trace/

~/.cat/agents/                  ← user-local add-ins (personal, not in repo)
```

**The boundary rule:**
- `src/cat/` — useful to ALL consumers; no UX, no domain logic
- `src/app/<app>/` — consumer-specific (spinner, docker, markdown rendering)
- `agents/` at repo root — project add-ins shared across all apps in this repo
- `~/.cat/agents/` — personal add-ins, not checked in

## File Renames / Moves

| Current path | New path |
|---|---|
| `src/cat/engine/activator.py` | `src/cat/agent/hypothesis.py` |
| `src/cat/engine/base.py` | `src/cat/agent/base.py` |
| `src/cat/engine/__init__.py` | `src/cat/agent/__init__.py` |
| `src/cat/skill/registry.py` | `src/cat/agent/registry.py` |
| `src/cat/skill/__init__.py` | `src/cat/agent/defn.py` |
| `src/cat/skill/built-in/*/skill.yaml` | `src/cat/agent/built-in/*/agent.yaml` |
| `src/cat/cli/cat_cli.py` | `src/app/cat-cli/cat_cli.py` |
| `benchmarks/swe_bench.py` | `src/app/swebench/swe_bench.py` |
| `src/cli/run_bench.sh` | `src/app/swebench/run_bench.sh` |
| `src/cli/compare_runs.py` etc. | `src/tooling/` (already there) |

---

## Schema Changes

**Minimal.** The internal schemas (HypothesisSchema, PerformerTaskSchema,
InvocationSchema, AttestorV3Schema) are untouched — they're internal to
HypothesisAgent and invisible to callers.

New schemas needed:

```python
# cat/agent/context.py
@dataclass
class ContractExecution:        # replaces ActivatorRun in public API
    contract:  Contract
    outcome:   str
    state:     ContractState
    cost:      Cost
    trace_id:  str
    depth:     int

# cat/contract/schemas.py — one new Pydantic model for serialization
class ContractExecutionSchema(BaseModel):
    contract_id: str
    obligation:  str
    outcome:     str
    state:       str
    cost_usd:    float
    depth:       int
```

`LoopConfig` in `cat/skill/__init__.py` gains one field:

```python
executor_type: str = "hypothesis"   # "hypothesis" | "orchestration" | "primitive"
```

No other schema changes. `ContractState`, `Contract`, `Criterion`, `Constraints`
are untouched.

---

## Effort Estimate

| Phase | Work | Risk | Hours |
|---|---|---|---|
| 1 — Rename/move files | `BaseActivator->Agent`, `HypothesisLoopActivator->HypothesisAgent`, file moves, aliases | Low — pure rename | 2–3 h |
| 2 — Extract Agent interface | `accept_request`, `draft_contract`, `activate`, `execute_contract` as explicit methods on HypothesisAgent | Medium — touches activator internals | 3–4 h |
| 3 — ContractExecution type | New dataclass, replace ActivatorRun in return types, update cli + benchmarks | Low | 1–2 h |
| 4 — AgentContext | Replace chat_history + workspace_reps with AgentContext in cli and Session | Low–Medium | 2 h |
| 5 — AgentFactory + executor_type YAML field | Registry, factory, skill parsing | Low | 1–2 h |
| 6 — AgentRegistry + agent.yaml | SkillRegistry→AgentRegistry, skill.yaml→agent.yaml, built-in agent dirs | Low | 2–3 h |
| 7 — PrimitiveAgent runner | Generic handler runner for primitive executor type, bash/web_search/workdir_explorer agent.yaml files | Low–Medium | 2–3 h |
| **Total** | | | **13–17 h** |

Phases 1–6 are pure refactor — zero behaviour change, existing tests pass.
Phase 7 adds primitive agents as new capability.

Recommended order: do Phase 1–3 first (one PR), then 4–5 (second PR),
then 6–7 (third PR). Each PR leaves the codebase working.

---

## What does NOT change

- `Contract`, `Criterion`, `Constraints`, `ContractState` — untouched
- `Representer` — stays, called inside `accept_request`
- `Stipulator`, `Oracle`, `Attestor` — stay, internal to `HypothesisAgent`
- All existing `skill.yaml` files — backward-compat: AgentRegistry reads them as agent.yaml
- `executor` field defaults to `"hypothesis"` — all existing skills run unchanged
- SWE-bench benchmark results — same engine, same models, same scores
- Trace format — unchanged
- Internal schemas (HypothesisSchema, PerformerTaskSchema, AttestorV3Schema) — untouched

## Add-in Agent IPC Contract

When executor is `primitive` and the handler is a non-Python file (`.sh`, `.ts`, `.exe`):

```
stdin:  JSON { "obligation": "...", "representations": [...], "workdir": "..." }
stdout: JSON { "outcome": "...", "state": "fulfilled"|"failed", "cost_usd": 0.0 }
stderr: logged as trace evidence
exit 0: treated as fulfilled
exit 1: treated as failed
```

Same pattern as `oracle.sh` today — the framework only speaks JSON+exit-code IPC.
This means a community bash script, a TypeScript tool, or a compiled binary can be
a first-class agent with zero Python.
