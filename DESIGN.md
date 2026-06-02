# Contract Activation Theory — Agentic Loop Design

> **Companion documents**:
> - `design_schema.md` — Schema library + naming convention (`Schema` suffix for Pydantic models, `Op` suffix for tree-edit verbs, no suffix for roles/dataclasses). Single source of truth for the data shapes that flow between layers.
> - `docs/iteration.md` — per-iter loop semantics (PLAN → ACTION → OBSERVE)
> - `roadmap.md` — deferred ideas (R28 NodeSchema nesting / UpdateStepOp, R29 FixVerify, R31 MutateBudget, R32 RollbackOnCorrupted, R33 MultiCandidatePlan)

## Premise

An agent is a contract-fulfillment system. A contract is a daemon-like object:
**dormant by default, activated only when it has passed an admission gate, and
discharged through a bounded sequence of capability invocations until either
its criteria are satisfied or its envelope is exhausted.**

The theory's central trade is explicit:

> Contract discipline trades exploration for bounded resource use.

A ReAct loop explores freely; CAT pre-commits to a plan and admits the
contract under a fixed resource envelope. The prediction this gives us is
testable: low-path-entropy tasks favor pre-commitment; high-path-entropy
tasks favor exploration. The crossover point is measurable.

---

## Core Vocabulary

CAT adopts contract-law terminology so the framework's mechanics are
self-documenting (see PRD `docs/1.6.2.9_PRD_renaming_and_dijkstra.md` for
the rename rationale). Each term below has a single meaning across code
and theory.

| Term | Definition |
|---|---|
| **Contract** | A first-class object representing an obligation. Carries criteria, constraints, representations, lifecycle state, and an escalation policy. |
| **Criterion** | A measurable predicate over State. A contract closes when its criteria conjunction holds. The predicate runs deterministically; even an "LLM attestor" criterion is wrapped in a predicate the system can call. |
| **Condition** | A predicate gating *activation* (vs Criterion which gates *closure/discharge*). |
| **Capability** | A named action the agent may perform (tool call). Whether the performer sees a capability at all depends on whether the contract's grant includes it. |
| **Representation** | A verified statement of fact about the world, captured by deterministic probes before planning. A contract whose obligations rest on unverified facts is formally incomplete. |
| **Stipulation** | A specific performance term the stipulator emits during Step 3 STIPULATE. The plan is an ordered list of stipulations. |
| **Activation** | The one-shot admission of a contract into the running pool, after the gate validates boundary, environment, and capacity. A successful activation grants a capability set and a discharge envelope. In legal terms this is the contract's *formation/execution* — the point at which it becomes binding. |
| **Performance** | The act of carrying out a granted capability during the execution loop. Each iteration is one performance act. |
| **Attestation** | The advisory verdict by the attestor (the per-step LLM judge in earlier vocabulary) that a stipulation is complete. Final closure still rests on the deterministic Criterion. |
| **Discharge** | The terminal exit of a contract: FULFILLED / FAILED / SUSPENDED / EXPIRED. Legal-aligned name for "close". |
| **Cost** | Scalar struct over resources (tokens, time, USD, calls). Constraints check enforces ceilings. |
| **Policy** | Per-contract declaration of how to respond to each admission-failure type: FAIL, SUSPEND-with-watcher, ESCALATE to parent, or ASK_USER. |

### Roles (who drives which step)

| Role | Step it drives | Kind | LLM role_tag in code |
|---|---|---|---|
| **Stipulator** | 3 STIPULATE | LLM | `stipulator` |
| **Performer** | 5 SELECT + 6 PERFORM | LLM | `performer` |
| **Attestor** | 7 VERIFY (per-stipulation) | LLM | `attestor` |
| **Resolver** | end-of-iter — fires on stall | LLM | `resolver` |
| **PlanPatcher** | invoked by Resolver to mutate the plan tree | LLM | `plan_patcher` |
| **Auditor** | 7 VERIFY (criterion / oracle) | deterministic | bash predicate (no LLM) |
| Surveyor | 2 REPRESENT | deterministic | `Representer` class (no LLM) |
| Registrar | 4 ACTIVATE | deterministic | `Activator._admit` (no LLM) |
| Recorder | cross-cutting | deterministic | `Recorder` class |
| Enforcer | cross-cutting | deterministic | `Constraints.check` + `_check_budget` |

The five bold roles each issue an LLM call; the others are pure code.
Theory-only roles (Surveyor / Registrar / Recorder / Enforcer) are
documented here as conceptual actors but do not exist as separate code
classes — they refer to existing components (`Representer`, `Activator`,
`Recorder`, `Constraints`).

**Resolver + PlanPatcher.** When a node stalls (default 3 iters without
`step_done`), the Resolver decides one of four moves:
`forward / patch_plan / backward / give_up`. If `patch_plan`, it hands off
to the PlanPatcher which emits tree-edit ops (`CommitOp`, `RefineOp`,
`BranchOp`, `DropOp`, `CommitNewOp`, `CheckoutOp`, `MergeOp`) against the
plan tree. See `cat/schemas.py` for `ResolverSchema` and `PatcherSchema`.

Future roles introduced in v1.7-v1.8:
- **Librarian** — DAG-corpus retrieval at STIPULATE / DELIBERATE time (v1.7)
- **Advisor** — candidate scoring for the Dijkstra layer (v1.8, see below)

---

## Contract Lifecycle

```
            received
               |
               v
         [ LATENT ] <----------+
            |  |               |
            |  +-- TTL expired -> EXPIRED
            |
   REPRESENT done
   STIPULATE done
            |
            v
       admission gate
        |       |
        |   gate FAILS
        |       |
        |       +--> policy:
        |             FAIL      -> [ FAILED ]
        |             SUSPEND   -> [ SUSPENDED ] -- watcher fires --+
        |             ESCALATE  -> parent decides                   |
        |             ASK_USER  -> [ SUSPENDED + user prompt ]      |
        |                                                           |
        +-- gate passes ----------> [ ACTIVE ]                      |
                                       |                            |
                              execution loop                        |
                                       |                            |
                       +---- criteria met -----> [ FULFILLED ]      |
                       |                                            |
                       +---- envelope exhausted -> [ FAILED ]       |
                       |                                            |
                       +---- criterion unreachable ----> [ SUSPENDED ]
                                                            |
                                                            +-------+
```

A contract spends most of its time **outside** the ACTIVE state — that is the
daemon analogy made operational. The ACTIVE state is bounded; entry is gated;
exit is by either fulfillment or a deliberate transition with a reason.

A contract is never "running" between transitions. It holds state, waits, and
re-enters the evaluation pool only when an external event makes a fresh
admission attempt sensible.

---

## The Agentic Loop — 8 Steps

The loop is **not a thread**. It is a bounded evaluation cycle driven by:
new contracts, external events, timer ticks, or capability completion.

```
PRE-EXECUTION (one-shot — contract formation)
  1. INGEST      receive a contract                            (LATENT)
  2. REPRESENT   surveyor establishes verified facts (probes, no LLM)
  3. STIPULATE   stipulator emits ordered performance terms    (1 LLM call)
  4. ACTIVATE    admission gate (one-shot):                    (LATENT → ACTIVE
                   boundary check                               or LATENT/SUSPENDED
                   environment fit                              or FAILED)
                   capacity check
                   capability grant derived from plan
                 → contract becomes binding (legal "formation/execution")

EXECUTION LOOP (per-iteration — performance phase)
  5. SELECT      pick capability from granted set for current step
  6. PERFORM     performer drives one LLM call → one tool call → one result
  7. VERIFY      run criterion; attestor updates the stipulation checklist
       └─→ continue loop, or → DISCHARGE

DISCHARGE (terminal — one-shot)
  8. DISCHARGE   FULFILLED / SUSPENDED / FAILED / EXPIRED
```

Steps 1–4 are activation (setup + admission). Steps 5–7 are the **execution
loop** — the contract's performance phase, repeated under the grant from
Step 4 until a terminal condition triggers Step 8 DISCHARGE.

---

### Step 1 — INGEST

**What:** Accept a contract from an external caller (user, parent contract, event).

**Formula:**
```
C = Contract(
    id        = uuid(),
    obligation = raw_task_text,
    criteria  = [],           # filled in STIPULATE or injected by caller
    conditions = [],
    constraints = Constraints(max_tokens, max_tool_calls, max_llm_calls, deadline),
    state     = LATENT,
    cost      = Cost(0, 0, 0, 0),
    representations = [],     # filled in REPRESENT
    policy    = ContractPolicy.default(),   # FAIL/SUSPEND/ESCALATE per failure type
)
```

**Real example (scikit-learn-14141):**
```
obligation = "Add joblib in show_versions
              joblib should be added to the dependencies listed in show_versions
              or added to the issue template when sklearn version is > 0.20."
constraints = max_tool_calls=120, max_llm_calls=200, max_tokens=800_000, max_usd=$2.00
state       = LATENT
policy      = on_boundary_fail=FAIL, on_environment_fail=SUSPEND, on_capacity_fail=FAIL
```

---

### Step 2 — REPRESENT

**What:** Run a fixed probe set against the environment. Results are
`(key, value, evidence)` triples — verified facts, not assumptions.
No LLM is called. Every probe is a deterministic tool call.

**Formula:**
```
R = []
for probe in PROBE_SET:
    result = bash(probe.command, timeout=10s)
    R.append(Representation(
        key      = probe.key,
        value    = extract_value(result),
        evidence = result.output[:500],
    ))

# Optional pre-injection by the caller (e.g. SWE-bench oracle pre-run failure)
contract.representations = pre_injected_reps + R
```

**Fixed probe set:**

| key | command | purpose |
|---|---|---|
| `workdir` | `pwd` | anchor for relative paths |
| `ls_detail` | `ls -la` | directory structure |
| `file_types` | `file *` | detect binary/archives |
| `notes` | `head -20 README*` | task-specific hints |
| `test_scripts` | `find . -name setup.py -o -name pytest.ini` | test entry points |
| `test_command` | `cat run_tests.sh` | runner script if present |
| `git_state` | `git log --oneline -5` | repo context |
| `eval_command` | `cat /tests/run-tests.sh` | eval harness if present |
| `judge_strategy` | (derived) | `oracle: <cmd>` or `llm_only` |
| `tool_strategy` | (derived) | which tools are available |

The probe set is currently tuned for SWE-bench-style Python repositories.
Across-domain expansion (v2.0) will add probes per benchmark adapter.

**Token cost of REPRESENT:** 0 LLM tokens.

---

### Step 3 — STIPULATE

**What:** The **stipulator** LLM receives the obligation and all
representations; emits an ordered, verifiable checklist of performance
terms (stipulations). Each stipulation must name specific files,
functions, or line numbers where known. The LAST stipulation must be: run
the oracle test command and confirm it exits 0.

**Formula:**
```
steps, planner_result = call_stipulator(llm, obligation,
                                        representations=contract.representations)
run.plan = [PlanStep(id, description, complete=False) for s in steps]
contract.cost += planner_result.cost
```

STIPULATE is part of activation cost (like a daemon's bootstrap). It runs
before the admission gate because the gate needs the plan to compute the
capability grant.

**Trace event:** `contract_stipulate` (legacy alias `contract_decompose`
still accepted for pre-1.6.2.9 traces).

**Real plan (scikit-learn-14141):**
```
s1: "Add 'joblib' to the _dependencies list in sklearn/utils/_show_versions.py."
s2: "Run the oracle test command ... and confirm it exits 0."
```

---

### Step 4 — ACTIVATE (the admission gate)

**What:** One-shot validation that the contract is fit to enter the ACTIVE
state. This is the layer that gives the theory its name. Four checks:

```
admit(contract, representations, plan, registry, resource_pool) -> Decision:

    # (a) Boundary — is the contract well-formed?
    if not contract.obligation: fail("malformed: empty obligation")
    if not contract.criteria:   fail("malformed: no criteria")
    if contract.constraints.bounds_invalid(): fail("malformed: invalid bounds")
    if contract.expired(now):   fail("malformed: deadline already passed")
    if not plan:                fail("malformed: empty plan")

    # (b) Environment — does reality match the contract's assumptions?
    if not contract.conditions_met(representations): fail("env: conditions unmet")
    for cap_name in capabilities_required_by(plan):
        if registry.get(cap_name) is None: fail(f"env: capability '{cap_name}' missing")

    # (c) Capacity — is there room to run?
    if contract.constraints.max_tokens_total < MINIMUM_TO_RUN: fail("capacity: under floor")
    if contract.depth > contract.constraints.max_depth:        fail("capacity: depth exceeded")

    # (d) Grant — derive granted capability set from plan steps
    grant = {cap for step in plan for cap in step.capabilities_needed} \
            or set(registry.names())   # fallback in v1: full registry

    return Admit(grant=grant, envelope=contract.constraints)
```

**Outcomes:**

- **Pass** → contract.state = ACTIVE, grant recorded, discharge begins.
- **Fail** → consult contract.policy; apply matching response.

**Failure response policy** (declared per contract at INGEST):

| Failure type | Default policy | Effect |
|---|---|---|
| `boundary` | FAIL | terminal — contract is broken at definition; only re-submission helps |
| `environment` | SUSPEND(watcher) | contract sleeps; re-attempt admission when watcher resolves |
| `capacity` | FAIL (permanent) or SUSPEND (transient) | depends on whether the shortfall is fixable |

A root contract from a human user can declare `ASK_USER` on environment fail
("I need a path / credential / confirmation"). A sub-contract can declare
`ESCALATE_TO_PARENT`.

**Key property:** admission failure is never silent retry. It is a state
transition with a reason and a re-entry condition. Re-admission is
**event-driven** — a watcher / callback fires when the missing condition
becomes plausibly true. Polling REPRESENT in a tight cycle just burns budget
producing the same refusal.

---

### Step 5 — SELECT

**What:** Choose which capability from the grant to invoke for the current
stipulation. v1 is fixed: always the **performer** LLM with the granted
tool set exposed in its schema. v2 will generalize to a minimum-cost
selector over multiple candidate paths — see *The Dijkstra Layer* section
below.

```
path = PerformerLLM(
    model       = contract.llm_model,
    tools       = registry.tool_schemas(only=grant),
    temperature = 0.2,
    tool_choice = "auto",
)
```

The single-candidate v1 case is honestly named: SELECT today returns a fixed
path. When v1.8 adds candidates via the ADVISOR layer, SELECT becomes a
real choice.

---

### Step 6 — PERFORM

**What:** One **performer** LLM call → one tool call → one tool result.
Strictly one performance act per turn. The result is appended to the
message thread so the next turn sees it.

**Formula:**
```
messages = [
    {role: system,    content: PERFORMER_SYSTEM_PROMPT},
    {role: user,      content: build_user_prompt(obligation, plan_checklist, budget)},
    ...run.messages[-window_size*2:]   # last window_size turns (action + result pairs)
]

result = llm.chat(messages, tools, temperature=0.2, role_tag="performer")
contract.cost += result.tokens

# No silent normalization. If args are malformed, the tool reports an error
# and the performer sees that error in the next turn — same as any other failure.
tool_result = registry.invoke(name, args, state)
contract.cost.tool_calls += 1

run.messages.append({role: assistant, tool_calls: [...]})
run.messages.append({role: tool, content: tool_result.output})
```

There is no Action Realizer. There are no tool preconditions or
precondition_failed flags. There is no failure-attribution taxonomy.
Tool errors are simply tool errors; the performer sees them and decides
the next action.

**Trace event:** `capability_perform` (legacy alias `capability_invoke` still
accepted for pre-1.6.2.9 traces).

---

### Step 7 — VERIFY

**What:** Run criteria after each PERFORM (subject to
`criterion_check_interval` to keep oracle-probe cost bounded).

```
if criterion.is_oracle:
    passed, evidence = criterion.predicate(state)
    state.kv["last_criterion_output"] = evidence.detail["output"]
    if passed:
        contract.state = FULFILLED
```

**Oracle is authoritative — attestor is advisory.** When any oracle criterion
is present, attestor (LLM judge) criteria are skipped at close time. The
contract closes on real test results, not model opinion.

The per-step **attestor** (Step 7 secondary) marks stipulations complete
for the performer's context only. It is advisory.

---

### Step 8 — DISCHARGE

**What:** Determine the terminal transition for this iteration:

```
if all_criteria_met:
    contract.state = FULFILLED
    return

elif envelope_exhausted(budget, depth, deadline):
    contract.state = FAILED
    run.failure_reason = budget_violation
    return

elif criterion_unreachable(progress_stuck):
    # No streak counters, no escape ladder.
    # The contract transitions to SUSPENDED with a reason; re-admission
    # requires fresh representations (the world must have changed).
    contract.state = SUSPENDED
    run.suspend_reason = "criterion_unreachable: progress not detectable"
    return

else:
    continue  # re-enter Step 5
```

A SUSPENDED contract is dormant. It is brought back to LATENT (and back to the
admission gate) only by an external trigger that may have changed the world —
a parent's intervention, a user input, a watcher event. The system does not
auto-retry.

---

## The Trace as a DAG (v1.6+)

Every run produces a trace — an append-only JSONL stream of events. Each
event carries an `event_id` and a `parent_id`. When `parent_id` references
the *immediate causal parent* (not a generic root), the trace becomes a
typed directed acyclic graph: the reasoning path of one task, captured as
structured causal data.

### Node types

| Node | Source event | What it captures |
|---|---|---|
| `StateTransition` | `contract_state` | LATENT → ELIGIBLE → ACTIVE → terminal |
| `Representation` | per-rep in `contract_represent` | one verified fact about the world |
| `Stipulation` | per-step in `contract_stipulate` | one performance term the stipulator emitted |
| `AdmissionDecision` | `contract_admit` | the gate's verdict + grant + reason |
| `Performance` | `capability_perform` | one iteration (LLM + tool call pair) |
| `ToolCall` / `ToolResult` | `tool_call`, `tool_result` | what the tool ran and returned |
| `CriterionCheck` | `criterion_check` | one oracle or attestor verdict |

(Legacy traces from pre-1.6.2.9 use `contract_decompose` and `capability_invoke`;
the DAG parser maps both old and new kinds to the same layers.)

### Edge types

| Edge | From → To | Meaning |
|---|---|---|
| `emitted_by` | Representation → REPRESENT-bash | this fact was probed via that tool call |
| `stipulated_from` | Stipulation → Representations | the stipulator used these representations |
| `gates` | AdmissionDecision → PlanSteps | admission verified these were well-formed |
| `addresses` | Invocation → PlanStep | this iteration was working on this step |
| `produced` | Invocation → ToolResult | this tool call returned this result |
| `verified_by` | Invocation → CriterionCheck | this iteration's state was checked here |
| `succeeded_by` | Invocation N → Invocation N+1 | sequential causality across the loop |
| `closes` | CriterionCheck → StateTransition (terminal) | this verdict closed the contract |

### Why DAG, not Dijkstra

A CAT run's graph is **not** a fixed graph with known weights — it is
constructed turn-by-turn by the stipulator (the initial linear chain) and the
performer (each iteration adds a node + edges). Dijkstra's shortest-path
algorithm doesn't apply: there is no "shortest" path, only the first one
that satisfies the criterion. The graph properties we care about are
different:

- **Linearity vs branching**: how many forks did the agent take? Pre-v1.5
  had F2 replan + F3 free-form — explicit branches. v1.5 has none. So a
  v1.5 DAG is a linear chain unless capability gating + sub-contracts
  (v1.6+) introduce real branches.
- **Convergence depth**: how many invocations until the criterion was
  satisfied? This is "longest chain to FULFILLED" — a structural property.
- **Branch points**: nodes from which a different choice was made (in past
  runs) that led to different outcomes. These are the *learnable
  decisions* — the ones cross-run learning can address.

The right model is **A\* with imitation learning over past DAGs**, not
Dijkstra. Past PASS DAGs are demonstrations; past FAIL DAGs are
anti-demonstrations; the stipulator (or, in v1.8, the ADVISOR) is
conditioned on retrieved past DAGs matching the current task's REPRESENT
fingerprint. Search is LLM-augmented, not algorithmic.

### The DAG as the contract's memory

A daemon that runs once is a script. A daemon that runs many times,
*remembers* what worked, and adjusts its admission policy based on history
is the full daemon model. The DAG corpus (`data/dag_corpus/`) is the
contract layer's persistent memory — separate from the LLM's stateless
context. Stored as JSON next to each trace; indexed by task signature for
retrieval.

This is what makes the daemon analogy operative across runs, not just
within a run. Within one run, the contract's state machine carries the
memory (representations, plan, grant). Across runs, the DAG corpus does.

### Declarative vs procedural memory — the brain analogy

The DAG corpus is **procedural memory** for the contract layer. The brain
analogy is exact:

| Memory type | Brain | CAT |
|---|---|---|
| **Declarative** (facts you can verbalize) | Hippocampus / medial temporal lobe; conscious recall | Representations + plan; in-context, regenerated per run |
| **Procedural** (skills you do automatically) | Basal ganglia / striatum / cerebellum; subconscious | DAG corpus + skill retrieval; persists across runs |

Today (v1.5) CAT has only declarative memory: every task is reasoned from
scratch by the stipulator LLM, conditioned on representations gathered fresh.
The agent is *always thinking hard*, which is correct for novel tasks but
wasteful for recurring patterns.

What v1.7's DAG-corpus retrieval introduces is **automaticity** — the
process by which a skill, after enough successful examples, requires fewer
cognitive resources. Concretely:

1. **Task classification at REPRESENT time** — the REPRESENT fingerprint
   (probe outputs + obligation embedding) is a *cue*.
2. **Cued retrieval of past DAGs** — sub-DAGs from past runs that solved
   tasks with similar fingerprints are surfaced.
3. **Chunking** — sub-DAG patterns that recur across many PASS runs
   ("read file → diagnose → str_replace → run pytest") become reusable
   skills, retrievable as a unit rather than re-planned step-by-step.
4. **Skill execution** — when the stipulator sees a high-confidence match,
   it can emit the retrieved sub-plan directly instead of reasoning from
   primitives. The performer follows it without re-deriving each step.

**Predicted observable**: token cost per task should drop on recurring
patterns. A swe-bench task class the agent has solved 10 times should cost
much less than the first attempt. That's automaticity becoming measurable.

### Three layers of memory in the full theory

| Layer | Lifetime | Carrier | Brain analog |
|---|---|---|---|
| **Working** | one iteration | LLM context window (performer messages) | working memory (prefrontal) |
| **Episodic** | one run | Contract state (representations, plan, cost, run.messages, prior_attempts) | episodic memory (hippocampus) |
| **Procedural** | many runs | DAG corpus + skill library (v1.7+) | procedural memory (basal ganglia) |

The activation gate (Step 4) is where these layers meet: admission consults
working capacity, episodic state, AND (in v1.7+) procedural skill library
to decide whether to admit, and *which capabilities* to grant.

### What the DAG enables (in order of work)

1. **Visualization in the HTML report (1.6.3)** — expand a task row to see
   its reasoning path as an SVG DAG. The "Dijkstra trace" view: look at the
   past run's decision tree visually.
2. **Layer attribution from topology (1.6.3)** — which layer's nodes
   dominate the DAG, which layer's edges are heaviest in cost. Replaces
   substring-matching heuristics with structural signals.
3. **Cross-run retrieval at plan time (1.6.6, v1.7)** — given the current
   task's REPRESENT fingerprint, find K most-similar past DAGs; feed their
   plans + outcomes as few-shot context. The stipulator gets to learn from
   precedent.
4. **DAG-grounded SUSPENDED resume (v1.7)** — when a contract suspends with
   `criterion_unreachable`, find the branch point where the agent committed
   to the failing approach; re-admit with a Representation containing the
   rejected sub-DAG. This restores the "failure → re-plan once" behavior
   promised in v1.4 Open Questions, but grounded in causal structure rather
   than streak counters (which was LIFE).

---

## Residual Information Flow Between Iterations

This is the memory the loop carries from one PERFORM to the next.

### Per-Iteration Residuals

```
ActivatorRun {
    plan:              list[PlanStep]
    messages:          list[dict]          # sliding window of LLM messages
    iterations:        int                 # total tool calls so far
    prior_attempts:    list[str]           # compressed log of past turns
    grant:             set[str]            # capability set granted at admission
}
```

**Message window (P1):**
The performer sees only the last `window_size=4` turns. Older turns are
compressed into `prior_attempts` — one line per turn:
```
format: "t{turn:02d} {tool}({arg_preview}) → {OK|ERR}: {first_line_of_output}"
```

This compression is **deterministic** — no LLM. The full LLM context never
grows unboundedly; old context degrades to a one-liner summary.

---

## Contract Schema (concrete)

```python
@dataclass
class Contract:
    id: str
    obligation: str
    criteria: list[Criterion]                # AND'd
    conditions: list[Condition]              # AND'd (admission gate)
    constraints: Constraints                 # budget envelope
    capabilities: list[CapabilityRef]        # initial declared set
    parent_id: ContractId | None
    children: list[ContractId]
    state: Latent | Eligible | Active | Suspended | Fulfilled | Failed | Expired
    cost: Cost
    priority: int
    created_at: Timestamp
    deadline: Timestamp | None
    representations: list[Representation]    # facts established before admission
    policy: ContractPolicy                   # response to each admission-failure type
    grant: set[str]                          # capabilities granted at admission
    suspend_reason: str | None               # set when state=SUSPENDED
    re_entry_condition: str | None           # what must change to re-admit

@dataclass
class ContractPolicy:
    on_boundary_fail:    Literal["FAIL", "ESCALATE"]                = "FAIL"
    on_environment_fail: Literal["FAIL", "SUSPEND", "ESCALATE", "ASK_USER"] = "SUSPEND"
    on_capacity_fail:    Literal["FAIL", "SUSPEND", "ESCALATE"]     = "FAIL"

@dataclass
class Representation:
    key: str
    value: str
    evidence: str
    probed_at: float

@dataclass
class ActivatorRun:
    plan:                list[PlanStep]
    messages:            list[dict]
    iterations:          int
    prior_attempts:      list[str]
    grant:               set[str]
    suspend_reason:      str | None
```

---

## Why This Is Different from a Normal Agent Loop

| Normal agent loop | Contract-activation loop |
|---|---|
| `while True: think(); act(); observe()` | Idle until a driver fires; ACTIVE state is bounded |
| Tool calls dispatched ad hoc | Tool calls are invocations under a contract grant |
| "Done" decided by the model | Done = oracle predicate returns True |
| Failure = exception or silent hallucination | Failure = explicit contract state with reason |
| Sub-tasks are stack frames | Sub-tasks are contracts with their own lifecycles (v2 work) |
| Budget is loose (max_tokens) | Budget is the envelope granted at admission |
| Obligation acted on as-received | Obligation grounded in verified representations before admission |
| Context grows unboundedly | Context = sliding window + compressed prior_attempts |
| Tools always available | Tools available only when the grant includes them (gating; v2 work) |
| Stuck = silent loop until timeout | Stuck = SUSPENDED with a reason and a re-entry condition |

---

## Open Design Questions (v2 and beyond)

- **Capability gating (real)** — per-plan-step grants restrict the tool schema
  the performer sees. v1 grants the full registry; v2 derives granted set from
  the plan. Then dormancy is measurable as activation rate < 1.
- **Sub-contracts** — plan steps become first-class Contracts with their own
  representations, criteria, constraints, lifecycle. The recursion in the
  vocabulary becomes operational. The DAG (v1.6) naturally captures these as
  nested sub-graphs.
- **Real SELECT** — multiple candidate paths (cached skill vs LLM; restricted
  vs full performer). Today's SELECT returns a fixed path. The DAG corpus
  (v1.7) provides the candidates: retrieved past skills are SELECT inputs.
- **Event-driven re-admission** — watchers fire when SUSPENDED preconditions
  resolve; today the system is single-shot per contract. v1.7
  DAG-grounded SUSPENDED resume operationalizes this.
- **ASK_USER capability** — admission can request user input; user input
  enters as a new Representation; admission re-runs. First step toward
  multi-party conversations (also enables tau-bench's user-simulator pattern).
- **Speculative activation** — pre-warm capabilities while ELIGIBLE.
- **Contract markets** — auction protocol when multiple agents bid on a
  contract.
- **pass^k consistency** — same contract run k times; measure variance.
- **DAG corpus retrieval at plan time (v1.7)** — `data/dag_corpus/` indexed by
  REPRESENT fingerprint; top-K most-similar past DAGs fed as stipulator few-shot
  context. The cross-run learning loop that closes the daemon model.

## Benchmark-adapter pattern

When CAT runs against an external benchmark (terminal-bench, SWE-bench,
tau-bench, AgentBench, WebArena, OSWorld) we adopt the benchmark's *harness
interface* — how tasks are loaded, how results are reported, what verifier
applies. We do **not** inherit the benchmark's execution model wholesale.

Lesson from v1.5 → v1.6: using terminal-bench as the harness for SWE-bench
gave us Docker orchestration + results.json + an oracle pattern for free, but
also locked us into tmux-based tool execution. Tmux is correct for
terminal-bench's native domain (interactive shell tasks); it is overkill and
fragile for SWE-bench's code-edit-test workflow. Result: 4/4 v1.5 FAILs were
diagnosed as `tool_environment` — the harness's shell model bleeding through
into a benchmark that doesn't need a shell.

**Principle:** for each benchmark CAT adopts, **separate the harness adapter
from the execution backend**:

| Layer | Provided by | What we adopt |
|---|---|---|
| Harness interface | Benchmark's runner (e.g. `tb runs create`) | Task loading, result schema, verifier lifecycle |
| Execution backend | CAT (`cat/capabilities.py`) | What primitives the agent uses to act |

For each domain, CAT picks the right execution primitives:

| Domain | Right primitives |
|---|---|
| SWE-bench / code edit + test | `docker exec` for non-interactive commands; structured `pytest_run`; first-class `ripgrep`/`fd`; multi-block edit; patch apply; jedi-backed symbol navigation |
| terminal-bench / interactive shell | tmux session (the harness's native model) |
| tau-bench / customer service | direct API tool wrappers + user-simulator hookup |
| WebArena / browser | Playwright primitives |
| OSWorld / desktop | x11 / accessibility-tree primitives |

The agent's reasoning layers (REPRESENT / STIPULATE / PERFORM / VERIFY) stay
identical across benchmarks. Only the **registered capabilities** swap based
on what's actually appropriate for the domain. This keeps the framework
durable against future benchmark adapters — none should be able to drag CAT
into a shell-shaped trap again.

---

## Tool surface and the tool-economy principle (v1.7+)

A core tension in agent design: more tools means more discoverability for the
LLM (e.g. `outline(file)` is cheaper than reading the file) but also a bigger
prompt and more decision overhead. Three reference points for SWE-bench
agents in 2026:

| Agent | Tool count | Approach |
|---|---|---|
| Pi (Earendil) | 7 (bash / read / write / edit / grep / find / ls) | "Primitives, not features." Multi-block edit done right (`edits[]` atomic against original); no patch, no AST, no git first-class |
| Hermes (Nous, 77.6% SWE-bench Verified) | ~20 (incl. terminal, `patch`, git, browser, vision, **skills**, session_search) | Rich tool set + auto-extracted "skills" as procedural memory; first-class `patch` is a notable structural primitive |
| Claude Code (Anthropic) | ~40 (Bash, Read, Write, Edit, MultiEdit, Grep, Glob, **LSP**, **Monitor**, Task, ...) | Richest surface; the standout 2026 additions are **LSP** (jump-to-def, find-refs, call hierarchy) and **Monitor** (background tail/poll) |

CAT's position (v1.7) sits between Pi's minimalism and Hermes/Claude's
richness — 11 docker-registry capabilities:

```
bash, read, write, list, str_replace          (basic POSIX-ish primitives)
rg(context=N), fd                              (sharper search)
multi_edit(path, edits[])                      (Pi-style atomic multi-block edit)
apply_patch(patch)                             (Hermes-style first-class patch tool)
git_status, git_diff(path, staged)             (Git ops promoted from bash)
goto_def(name), find_refs(name), outline(path) (jedi-backed Python navigation; lazy-installed in container)
```

**The tool-economy principle.** CAT's value-add is not having more or fewer
tools; it is contract discipline picking the *right* tool, *once*.
Concretely:

- The stipulator commits to a plan with verifiable steps before tools fire;
  this is what keeps SELECT/PERFORM bounded rather than ReAct-style
  thrashing.
- The activator grants only the capabilities the contract genuinely needs;
  unused tools never enter the LLM's tool list for that contract.
- The attestor + diagnostic auditor flag tool-use *waste* (e.g. "read X.py
  called 7×, consider `outline`"), so misuse becomes visible and actionable.
- The run report measures `tool_calls_per_task_avg` as a first-class KPI,
  alongside solve rate.

The hypothesis under test: **CAT should achieve equal solve rate at fewer
tool calls than a comparably-equipped agent without contract discipline.**
This is operationalised by the 1.8.0 tool-economy metrics. Baseline
measurement on swe10-v1.6.6-final (before this expansion): 42.7 calls/task
average, with PASSing tasks already showing 4×–7× redundant file reads that
`outline` was designed to subsume.

**Why "tooling is not cheating."** Restricting the tool surface as a way to
prove the framework's worth would conflate two distinct claims: (1) the
framework adds value, and (2) it adds value relative to a competitor's
artificially-degraded baseline. The right comparison is fully-equipped CAT
vs fully-equipped ReAct on the same surface — and there, CAT should win on
*efficiency at fixed solve rate*, not on raw solve rate alone.

---

## What the Theory Predicts (testable claims)

The discipline trade — exploration for bounded resource use — yields three
falsifiable predictions:

1. **Token efficiency** — on tasks where the fix path is constrained, CAT
   should use strictly fewer tokens than ReAct at equal solve rate. Confirmed:
   swe30 v14 = 92K tokens/solved task vs ReAct 354K (3.8×).

2. **Solve-rate gap** — on tasks with high path entropy (many viable
   solutions), ReAct's exploration should outperform CAT's pre-commitment.
   Partially confirmed: swe30 v14 CAT 43.3% vs ReAct 50% — CAT trails on
   exploratory tasks (Django/sympy large codebases), matches or beats on
   constrained ones. **After v1.6.6 (docker exec backend + oracle alignment):
   swe10-v1.6.6-final = 9/10 = 90%**, suggesting the gap was inflated by
   infrastructure failure, not cognitive limit.

3. **Activation rate** — fraction of registered capabilities invoked per turn.
   ReAct's rate ≈ 1.0 (full schema every turn); CAT's rate should be < 1
   once capability gating is real (v2 work). Currently both ≈ 1.0.

4. **Diagnostic correctness** — when the auditor labels a layer as the
   primary failure, fixing that layer should produce the predicted uplift.
   Confirmed (v1.6.6): the LLM auditor labeled 4/4 v1.5 FAILs as
   `tool_environment` (with high confidence); replacing tmux with docker exec
   lifted swe10 PASS rate from 6/10 to 9/10. The diagnostic is now an
   actionable signal, not just attribution.

Claims (1), (2), and (4) survive the ablation. Claim (3) cannot be tested
until the capability grant is wired into the tool schema — that is the next
theory test.

---

## The Dijkstra Layer (v1.8 design)

Today's PERFORMER does both candidate generation and election in one LLM
call (its argmax). To make the framework capable of true cost-aware search
across alternatives, Step 6 PERFORM would split into three sub-steps —
matching Dijkstra's expand-neighbors / compute-f / pop-min triad.

```
EXECUTION LOOP iteration N
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6 PERFORM (would split as):                                     │
│                                                                      │
│  6a CANDIDATE GENERATION   ← Dijkstra's "expand neighbors"          │
│      Generator: PERFORMER samples K candidate (tool, args) pairs    │
│      Optional: LIBRARIAN injects past-DAG moves at similar states   │
│      Output: candidate set {c_1, …, c_K}                            │
│                                                                      │
│  6b PATH EVALUATION        ← Dijkstra's "compute f = g + h"         │
│      Evaluator: ADVISOR scores each candidate                       │
│        g(c) = accumulated cost so far (tokens/wall/USD)             │
│        h(c) = est. remaining cost (from retrieved past DAG outcomes)│
│      Output: ranked candidates with f-scores                        │
│                                                                      │
│  6c ELECTION               ← Dijkstra's "pop min f"                 │
│      Selector: deterministic argmin over scored candidates          │
│      (Legal verb: the obligor ELECTS among means of performance)    │
│                                                                      │
│ Step 7 PERFORM-DISPATCH                                              │
│      Run the elected capability. No choice; pure execution.         │
└──────────────────────────────────────────────────────────────────────┘
```

**Two roles introduced:**

- **LIBRARIAN** (v1.7) — retrieves past DAGs whose REPRESENT fingerprints
  match the current task. Provides procedural-memory hints to the
  performer/advisor.
- **ADVISOR** (v1.8) — scores candidates by `f = g + h`. May be another LLM
  call, or a learned classifier over past-DAG cost signals.

**Implementation phases:**

| Phase | What ships | When |
|---|---|---|
| Option I — implicit | LIBRARIAN injects retrieved moves as hints in PERFORMER prompt; no sub-step split | v1.7 (1.7.1) |
| Option II — explicit | Full 6a/6b/6c split; ADVISOR scoring component | v1.8 (1.8.1 – 1.8.3) |

See `docs/1.6.2.9_PRD_renaming_and_dijkstra.md` §5 for the decision record.

**Why this isn't real Dijkstra:**
Dijkstra needs an admissible heuristic `h()`. Ours is learned from past
DAGs — admissibility is empirical, not proved. The right framing is
**A\* with imitation learning over past DAGs**, where past PASS DAGs are
demonstrations and past FAIL DAGs are anti-demonstrations.
