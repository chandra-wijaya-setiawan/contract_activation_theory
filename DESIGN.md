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
| **Representation** | A verified statement of fact about the current state of the world, established before contract formation, upon which the contract's obligations are grounded. A contract formed on absent or false representations is formally incomplete: its obligations are contingent on conditions that were never established. |

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
1. INGEST      receive or detect a contract (external request, event, decomposition)
2. REPRESENT   establish verified facts about the current world state
3. DECOMPOSE   break contract into criteria + sub-contracts, grounded in representations
4. EVALUATE    check eligibility: conditions, constraints, resource availability
5. SELECT      among eligible paths, pick the minimum-cost fulfillment plan
6. ACTIVATE    grant execution to the chosen capabilities/tools
7. VERIFY      measure criteria; confirm contract is satisfied
8. CLOSE       mark FULFILLED, SUSPEND, or re-enter EVALUATE
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
    representations: list[Representation]   # facts established before decomposition
```

Criteria and conditions are **callables over a State snapshot**, not strings to
be interpreted. This keeps the runtime small and forces the model author to be
explicit about what "satisfied" means.

## Representation (Step 2)

Representation is the only step that runs unconditionally before any obligation is
decomposed. It is not planning. It is not exploration. It is **fact-finding** —
the agent probes the environment to establish what is verifiably true right now.

A representation is a `(key, value, evidence)` triple:

- `key` — the fact being established (e.g. `"workdir"`, `"test_command"`, `"repo_state"`)
- `value` — the verified value (e.g. `"/testbed"`, `"pytest tests/ -q"`, `"clean"`)
- `evidence` — the raw output that proves it (bash output, file content, etc.)

```python
@dataclass
class Representation:
    key: str
    value: str
    evidence: str       # raw probe output — the warrant for the claim
    probed_at: Timestamp
```

Representations are **minimal and deterministic** — the agent runs a fixed probe
set, not open-ended exploration. The probe set may be generic (works for any task)
or task-type-aware (SWE-bench vs data pipeline). Either way it completes in a
bounded number of tool calls before the planner sees anything.

The DECOMPOSE step receives the obligation **and** the representations. Plan steps
must be consistent with what representations say is true. A step that contradicts
a representation is invalid before it executes — this is enforced at plan
construction time, not discovered at runtime.

This is what makes the contract non-incomplete in the formal sense: obligations
are grounded in verified facts, not assumptions about a world the agent has not
yet observed.

## Decomposition (Step 3)

Decomposition is the only step where the agent uses *judgment* (typically an
LLM). Everything else is mechanical. The decomposer is itself a contract:

> **DecomposeContract(parent)**
> - condition: parent.state == ELIGIBLE and parent.criteria is empty/abstract
> - criterion: parent.criteria is non-empty AND each criterion is checkable
> - capability: LLM call with a prompt that emits Criterion[] and sub-Contract[]

This makes decomposition uniform with every other capability. There is no
"special" planning code outside the contract abstraction.

## Selection (Step 5) — minimum-cost path

Given a set of eligible contracts and a set of plans per contract, the selector
picks the plan minimizing expected cost subject to constraints. v1 uses a
simple lexicographic order: (1) hard deadline first, (2) lowest token cost,
(3) lowest tool-call count. No learned policy. No utility function. Just
explicit rules the user can read.

If selection is itself non-trivial (auctions, fairness), it gets its own
contract.

## Verification (Step 7)

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
| Obligation acted on as-received | Obligation grounded in representations before decomposition |

## v1 Scope

The first runnable cut should demonstrate the loop end-to-end with the minimum
that proves the abstraction is load-bearing:

- `Contract`, `Criterion`, `Condition`, `Constraints` dataclasses.
- An in-process `Activator` that drives the 7 steps.
- A `World` / `State` snapshot the predicates read from.
- One worked example: a user goal ("answer question X with citations") that
  decomposes into a retrieval sub-contract and a synthesis sub-contract,
  each with measurable criteria, all under a token budget.

No persistence. No distribution. No concurrency. Just the loop, in memory,
single-threaded, with a print-trace so the activation pattern is visible.

## Open questions (to resolve before v2)

- **Event sourcing**: should every state transition emit an event so the
  history is replayable? Strong yes long-term; not required for v1.
- **Speculative activation**: can a contract pre-activate capabilities while
  still ELIGIBLE to reduce latency? Defer.
- **Contract markets**: when multiple agents bid to fulfill a contract,
  what's the auction protocol? Out of scope for v1.
- **Failure propagation**: does a child's FAILED state automatically fail
  the parent, or does the parent get to re-plan? Default: re-plan once, then fail.
