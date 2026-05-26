# Contract Activation Theory — Agentic Loop Design

## Premise

An agent is not a process that "runs." It is a **contract-fulfillment system**: a
host for latent capabilities that are activated only when a contract's conditions
are satisfied, and only to the minimum extent needed to discharge the contract.

Execution is the exception, not the default. The default state is dormancy.

## Core Vocabulary

| Term | Definition |
|---|---|
| **Contract** | A first-class object representing an obligation. Carries activation conditions, fulfillment criteria, constraints, and lifecycle state. |
| **Criterion** | A measurable predicate that, when true, contributes to fulfillment. A contract is fulfilled when its criteria conjunction holds. |
| **Condition** | A predicate over world/state that gates *activation* (distinct from fulfillment, which gates *closure*). |
| **Capability** | A latent action the agent can invoke (tool call, sub-contract spawn, computation). Not invoked unless a contract requires it. |
| **Activation** | The transient grant of execution rights to a capability on behalf of a contract. |
| **Cost** | Scalar (or vector) over resources: tokens, time, money, side-effects. Selection prefers minimum cost. |

## Contract Lifecycle

```
            received
               |
               v
         [ LATENT ] -----------+
               |               | conditions never met,
   conditions met              | TTL expired → EXPIRED
               |
               v
         [ ELIGIBLE ]
               |
   scheduler grants slot
               |
               v
         [ ACTIVE ] ---+--- criteria met ---> [ FULFILLED ] --> closed
               |       |
               |       +--- constraint violated ---> [ FAILED ]
               |       |
               |       +--- preempted / awaiting ---> [ SUSPENDED ]
               v                                          |
         (decompose into sub-contracts)                   |
                                                          v
                                              re-eligible when unblocked
```

A contract is never "running" between transitions. It holds state, waits, and
re-enters the evaluation pool when something changes.

## The Agentic Loop

The loop the user supplied, restated as the runtime's evaluation cycle:

```
1. INGEST    receive or detect a contract (external request, event, decomposition)
2. DECOMPOSE break contract into measurable fulfillment criteria + sub-contracts
3. EVALUATE  check eligibility: conditions, constraints, resource availability
4. SELECT    among eligible paths, pick the minimum-cost fulfillment plan
5. ACTIVATE  grant execution to the chosen capabilities/tools
6. VERIFY    measure criteria; confirm contract is satisfied
7. CLOSE     mark FULFILLED, SUSPEND, or re-enter EVALUATE
```

The loop is **not a thread**. It is a fixed-point evaluator driven by:

- new contracts arriving,
- external events,
- timer ticks,
- completion of an active capability.

Between drivers, the system is idle. This is the central commitment of the theory.

## Contract Schema (concrete)

```python
@dataclass
class Contract:
    id: str
    obligation: str                          # human-readable promise
    criteria: list[Criterion]                # AND'd; all must hold for fulfillment
    conditions: list[Condition]              # AND'd; all must hold for activation
    constraints: Constraints                 # budgets: tokens, time, depth, $
    capabilities: list[CapabilityRef]        # what *may* be activated
    parent: ContractId | None
    children: list[ContractId]
    state: Latent | Eligible | Active | Suspended | Fulfilled | Failed | Expired
    cost_so_far: Cost
    priority: int
    created_at: Timestamp
    deadline: Timestamp | None
```

Criteria and conditions are **callables over a State snapshot**, not strings to
be interpreted. This keeps the runtime small and forces the model author to be
explicit about what "satisfied" means.

## Decomposition (Step 2)

Decomposition is the only step where the agent uses *judgment* (typically an
LLM). Everything else is mechanical. The decomposer is itself a contract:

> **DecomposeContract(parent)**
> - condition: parent.state == ELIGIBLE and parent.criteria is empty/abstract
> - criterion: parent.criteria is non-empty AND each criterion is checkable
> - capability: LLM call with a prompt that emits Criterion[] and sub-Contract[]

This makes decomposition uniform with every other capability. There is no
"special" planning code outside the contract abstraction.

## Selection (Step 4) — minimum-cost path

Given a set of eligible contracts and a set of plans per contract, the selector
picks the plan minimizing expected cost subject to constraints. v1 uses a
simple lexicographic order: (1) hard deadline first, (2) lowest token cost,
(3) lowest tool-call count. No learned policy. No utility function. Just
explicit rules the user can read.

If selection is itself non-trivial (auctions, fairness), it gets its own
contract.

## Verification (Step 6)

Every criterion is a pure predicate `(State) -> bool` plus an evidence
attachment `(State) -> Evidence`. A contract is only marked FULFILLED when
every criterion returns true *and* evidence is recorded. No "the agent says
it's done." This is the safety floor.

For criteria that are inherently subjective (e.g., "the answer is helpful"),
the criterion is satisfied by *another contract* — a judge — that returns a
verifiable structured result.

## Why this is different from a normal agent loop

| Normal agent loop | Contract-activation loop |
|---|---|
| `while True: think(); act(); observe()` | Idle until a driver fires |
| Tool calls dispatched ad hoc | Tool calls are activations on a contract |
| "Done" decided by the model | Done = criteria predicates return true |
| Failure = exception | Failure = explicit contract state with reason |
| Sub-tasks are stack frames | Sub-tasks are contracts with their own lifecycles |
| Budget is loose (max_tokens) | Budget is a constraint on the contract |

## Roadmap

### Why coding first

The coding domain is the right place to harden CAT before claiming generality.
It has **two distinct validation gates** that most other domains lack:

1. **Deterministic oracle** — file existence, content predicates, test-suite
   pass/fail. Unambiguous ground truth. This is the gate Phase 1 already uses.
2. **Multi-turn nondeterministic execution** — the agent must navigate shell
   state, compiler errors, dependency issues, and test failures across turns
   where the next correct action is not fully predictable from the instruction
   alone. This is where ReAct drifts and where discipline should pay off most.

Established coding agents (Pi, Hermes, Claude Code) exercise both gates at
scale. CAT must match or beat them on their home turf before the
"loop discipline, not domain structure" claim is credible.

**Hard gate to v2:** CAT excels in the coding domain — demonstrated parity or
advantage against at least one established coding agent on a recognized
benchmark. Until this gate is met, no resources go toward other domains.

---

### v1.0 — Synthetic proof of mechanism (DONE, 2026-05-14)

- Contract / Criterion / Activator / Capability abstractions, end-to-end.
- ReAct control agent sharing the same LLM + tool stack.
- 2 synthetic tasks (trivial 1-step, moderate 5-step), 12 trials.
- Result: 6× fewer tool calls, 10× fewer tokens on 5-step task, solve parity.
- Thesis confirmed: discipline reduces cost; gap widens with complexity.

### v1.2 — Expand synthetic coverage

- Add 3–5 synthetic tasks spanning coding sub-domains:
  - **Bug fix** — inject a known bug, oracle checks the fix + no regressions.
  - **Multi-file refactor** — rename/move across files, oracle checks
    consistency.
  - **Test generation** — given code, produce tests that hit specified
    coverage, oracle runs pytest + coverage tool.
- Harden the oracle framework: composable predicates (file content, exit code,
  coverage threshold, AST checks).
- Validate that CAT's cost advantage holds as step count increases (5 → 10 →
  15+ steps). Establish the drift curve for ReAct.

### v1.4 — Real benchmark integration

- Integrate a recognized coding benchmark (SWE-bench Lite or terminal-bench
  subset) so results are comparable to published agent scores.
- **τ-bench integration** (Sierra Research, arxiv 2406.12045): add retail and/or
  airline domain as a non-coding benchmark track. τ-bench uses ground-truth
  database-state comparison — structurally identical to CAT's criterion
  predicates (`(State) -> bool`), just over DB rows instead of files. This
  early cross-domain probe pressure-tests generality before v2.0 formally
  opens the door. Criterion adapter: map τ-bench goal-state annotations to
  CAT fulfillment criteria; capability adapter: wrap τ-bench API tools as
  CAT capabilities.
- Adapt the oracle layer to consume external graders (SWE-bench grader,
  τ-bench state comparator).
- Run CAT vs ReAct on ≥ 30 benchmark instances per track. Measure solve rate,
  token cost, tool calls, wall time. Report **pass^k** (k=4) for consistency.
- Baseline comparison: run the same instances through Pi (stateless ReAct
  baseline) and report head-to-head.

### v1.6 — Failure handling and re-planning

- Implement failure propagation: child FAILED → parent re-plans (once), then
  fails. (Open question from v1 design, now resolved.)
- Add SUSPENDED → re-ELIGIBLE flow for contracts blocked on external state
  (e.g., waiting for a test run).
- Stress-test on tasks that require recovery: wrong file written, test fails,
  dependency missing.

### v1.8 — Coding domain hardening (GATE)

- Head-to-head against at least one established coding agent (Pi, Hermes, or
  Claude Code) on a shared benchmark.
- Target: ≥ parity on solve rate, measurable advantage on token efficiency or
  cost.
- **LIFE-Harness comparison** (arxiv 2605.22166, Xu et al. 2026): run CAT and
  LIFE-Harness on shared τ-bench / τ²-bench tasks. LIFE-Harness reaches the
  same 4-layer architecture (environment contracts, trajectory regulation,
  procedural skills, action realization) from an empirical/bottom-up direction;
  CAT prescribes the same structure top-down. Direct comparison tests whether
  principled design matches or beats learned interventions.
- **AgentBench subset** (THUDM, ICLR 2024): add OS (Linux shell, 144 tasks)
  and DBBench (SQL, 261 tasks) as lightweight cross-domain benchmarks. Both
  are deterministic, have published LIFE-Harness numbers, and map cleanly to
  CAT criteria (file/exit-code checks, query-result comparison). ALFWorld
  (household, 134 tasks) is optional low-cost addition.
- Event sourcing: every state transition emits a replayable event. Enables
  post-hoc analysis of where ReAct drifts vs where CAT stays on track.
- Report **pass^k** (k=4) across all environments for consistency measurement.
- Publish results and methodology (internal or external).
- **Gate review:** results justify moving to multi-domain expansion.

---

### v2.0 — Domain-agnostic expansion (gated on v1.8)

- Prestige benchmarks: **WebArena** (web browsing, 812 tasks), **OSWorld**
  (desktop control, 369 tasks), **Spider 2.0** (enterprise SQL, 632 tasks).
  These require heavier integration but are the recognized multi-domain
  agent benchmarks.
- Each new domain adds: task definitions, domain-specific capabilities,
  oracles, and a ReAct baseline comparison.
- Criterion adapters by state type:
  - File system: `file_content(path) == expected` (OS, SWE-bench, OSWorld)
  - Database rows: `db_state == gold_state` (τ-bench, DBBench, Spider 2.0)
  - Web/DOM: `page_element(selector) == expected` (WebArena, WebShop)
  - Object location: `object_at(room, surface)` (ALFWorld)
- Validate that the cost/discipline advantage transfers across domains, not
  just within coding.

### v2.x — Open design questions (deferred)

- **Speculative activation** — pre-activate capabilities while ELIGIBLE to
  reduce latency.
- **Contract markets** — auction protocol when multiple agents bid on a
  contract.
- **Persistence and distribution** — contracts that survive process restarts,
  cross-agent handoff.
