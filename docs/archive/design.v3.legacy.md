# CAT v3 — Looping Engine Design

> Status: **DESIGN** (not yet implemented)  
> Supersedes: `DESIGN.md` (v2 plan-tree architecture)  
> Companion: `plan.v3.md` (refactor steps)

---

## Core Insight

The v2 architecture treated SWE-bench as a **project management problem** —
a multi-phase plan (MAP → DESIGN → IMPLEMENT → VERIFY) with an Attestor
judging every intermediate step. This created overhead, token bloat, and
obscured the actual contract structure.

v3 treats it as a **hypothesis search problem**:

> The obligation has one criterion (oracle). The Stipulator generates a
> specific, testable hypothesis about how to satisfy it. The Performer
> implements the hypothesis. The Oracle judges. If wrong, generate a
> better hypothesis informed by what was learned.

---

## The Contract Structure

```
Contract {
    obligation:       str        # what the user wants
    criterion:        oracle()   # ONLY thing that closes the contract
    representations:  list       # verified world facts (workdir, test cmd, ...)
    constraints:      Constraints
}
```

The oracle is **deterministic and binary**. It does not require LLM judgment.
Every architectural decision flows from this: the only verdict that matters
is the oracle.

---

## Schemas

### HypothesisSchema — the Stipulator's output

```python
class HypothesisSchema(BaseModel):
    location:   str   # WHERE to act — file, function, API endpoint, record ID
    change:     str   # WHAT to do — specific enough for Performer to execute
    predicts:   str   # WHAT will be true after — verifiable statement
    basis:      str   # WHY this hypothesis — reasoning from obligation + knowledge
    start_from: str   # WHICH checkpoint to branch from — "base" or "h1"/"h2"/...
```

**Not** a step-by-step plan. One specific prediction about the world state
after the fix. The Performer figures out HOW to implement it.

`start_from` lets the Stipulator choose the starting state for this hypothesis
(see **Checkpoint Branching** below): `"base"` = the original unmodified state
(discard prior changes), or a prior checkpoint label = build on that attempt's
changes. This is how the framework avoids cumulative patch corruption.

### PerformerTaskSchema — what Performer receives

```python
class PerformerTaskSchema(BaseModel):
    obligation:   str              # the original task (semantic anchor)
    hypothesis:   HypothesisSchema # specific target to implement
    oracle:       str              # test command — self-verification signal
    context:      str              # compressed knowledge from prior attempts
    max_attempts: int = 5
```

**Narrow context by design.** Performer does NOT receive the full plan tree,
full message history, or prior run context. Just: what to fix, why, and
how to verify. Enables cheaper model tier for Performer.

### KnowledgeSchema — what transfers between hypotheses

```python
class KnowledgeSchema(BaseModel):
    findings:          dict[str, str]  # location → what was actually found there
    failed_hypotheses: list[str]       # what was tried + why oracle still failed
    oracle_failures:   list[str]       # specific test failure messages
```

This is passed to the Stipulator when generating the next hypothesis.
Prevents re-investigating already-known facts. The key insight from
the token analysis: each restipulate was re-reading already-known files
because knowledge wasn't transferred.

### PerformerResultSchema — what Performer returns

```python
class PerformerResultSchema(BaseModel):
    verdict:  Literal["completed", "exhausted", "give_up"]
    reason:   str                      # mandatory explanation
    findings: dict[str, str]           # key facts discovered during execution
    executions: list[ExecutionSchema]  # tool call record
```

`completed` — Performer believes hypothesis is implemented (oracle may still fail)  
`exhausted`  — K attempts without completing (extra summarisation LLM call)  
`give_up`    — hypothesis is demonstrably wrong (location doesn't exist, etc.)

### ExecutionSchema — one tool call in the inner loop

```python
class ExecutionSchema(BaseModel):
    attempt:          int
    tool_name:        str
    args:             dict
    reason:           str   # why this tool (deviation explanation)
    execution_status: Literal["success", "error"]
    output_text:      str
    durable_change:   bool
    files_modified:   list[str]
```

---

## Outer Loop

```
run(contract, state):
    knowledge = KnowledgeSchema()
    prior_attempts = []
    checkpointer.snapshot("base")        # original clean state
    current_tier = 0                     # execution-model tier

    for hypothesis_n in range(MAX_HYPOTHESES):

        # Stipulator (planning_llm) generates ONE hypothesis + picks start_from
        hypothesis = call_stipulator_v3(
            planning_llm,
            obligation     = contract.obligation,
            representations= contract.representations,
            knowledge      = knowledge,
            prior_attempts = prior_attempts,   # includes checkpoint labels
        )

        # Branch: restore the chosen checkpoint before the Performer runs
        checkpointer.restore(hypothesis.start_from)   # "base" or "hN"

        # Performer implements it (inner loop) on the current execution tier
        result = perform(contract.oracle, hypothesis, llm=tier_llm[current_tier])

        # Snapshot the Performer's edits BEFORE the oracle mutates the tree
        if result.any_durable_change:
            checkpointer.snapshot(f"h{hypothesis_n+1}")

        # Oracle is the ONLY judge
        if oracle_passes(contract, state):
            checkpointer.restore(f"h{hypothesis_n+1}")   # clean source-only edits
            return FULFILLED

        knowledge.update(result, oracle_failure)
        prior_attempts.append({... , "checkpoint": f"h{hypothesis_n+1}"})

        # Escalation: when stuck (give_up OR 2 oracle-fails at this tier),
        # move to the next model tier. At max tier → stop.
        if tier_stuck(result, prior_attempts, current_tier):
            if current_tier < max_tier:
                current_tier += 1                  # next hypothesis = stronger model
            else:
                break                              # exhausted all tiers

    return FAILED
```

**No plan tree. No stall_count. No Resolver. No PlanPatcher.**
The only stateful decisions are: (1) which checkpoint to branch from
(`hypothesis.start_from`), and (2) which model tier to execute on (`current_tier`).

---

## Inner Loop (Performer)

```
perform(oracle, hypothesis, max_attempts):
    context = build_task_prompt(hypothesis)
    messages = [system_prompt, context]

    for attempt in 1..max_attempts:

        response = llm(messages, tools=all_tools + [done, give_up])
        messages += [response, tool_results]

        if response.called("done"):
            return PerformerResultSchema(
                verdict="completed",
                reason=done.args.reason,
                findings=extract_findings(messages),
            )

        if response.called("give_up"):
            return PerformerResultSchema(
                verdict="give_up",
                reason=give_up.args.reason,
                findings=extract_findings(messages),
            )

        # Check oracle immediately after any mutation
        if any_durable_change(response) and oracle_passes():
            return PerformerResultSchema(verdict="completed", ...)

    # Loop exhausted — summarisation call
    summary = llm(summarise_prompt(messages))
    return PerformerResultSchema(
        verdict="exhausted",
        reason=summary.text,
        findings=extract_findings(messages),
    )
```

---

## Attestor (called only on oracle failure)

The Attestor is **not** called after every iteration. It is called once per
hypothesis cycle, only when the oracle failed, to decide:

```
attestor_verdict ∈ {
    "new_hypothesis"  → try again with updated knowledge
    "give_up"         → task is unrecoverable
}
```

The Attestor receives: `PerformerResultSchema + KnowledgeSchema + prior_attempts`.
It does NOT receive intermediate step details — only the outcome of the
full hypothesis attempt.

> **Note (current implementation):** the standalone Attestor LLM call is bypassed
> in the live engine — the **escalation ladder** subsumes its decision. "give_up
> or 2 oracle-fails at this tier" triggers a move to the next model tier instead
> of an Attestor vote; only when the top tier is exhausted does the task truly
> fail. See **Model Escalation Tiers**.

---

## Checkpoint Branching

Each hypothesis runs against a chosen starting state, not blindly on top of the
previous (failed) attempt. This eliminates **cumulative patch corruption** —
the failure mode where H2 reads H1's broken edits and patches the patch.

**Abstraction (domain-agnostic).** The activator only sees a `Checkpointer`
protocol — it never knows the backing store:

```python
class Checkpointer(Protocol):
    def snapshot(self, label: str) -> bool   # capture current working state
    def restore(self, label: str) -> bool    # reset working state to a label
    def labels(self) -> list[str]            # available checkpoints
```

- `NullCheckpointer` — no-op default → pure forward-editing (degrades gracefully).
- `GitCheckpointer` — reference impl for git-backed envs (SWE-bench). Uses git
  plumbing (`write-tree`/`commit-tree`/`update-ref` into `refs/cat/<label>`;
  restore via `read-tree`+`checkout-index`+`clean`). Takes an injected `exec_fn`
  so it has **no docker import** — all git/docker specifics stay in the adapter
  (`build_git_checkpointer` in capabilities.py), never in the core loop.

**Lifecycle per task:**

```
snapshot("base")                      # before the loop — original clean tree
for each hypothesis Hn:
    restore(hypothesis.start_from)    # "base" or a prior "hK" the Stipulator chose
    performer edits the tree
    snapshot(f"hn")                   # BEFORE oracle (clean source-only edits)
    run oracle                        # oracle mutates tree (applies test_patch)
    on PASS: restore(f"hn")           # strip test_patch artifacts; harness re-evals
```

**Why snapshot before the oracle:** the SWE-bench oracle applies `test_patch` and
runs tests against the working tree. Snapshotting after the Performer (pre-oracle)
keeps each checkpoint as clean source-only edits, so a later `restore` gives the
next hypothesis a pristine branch point. The harness evaluates the working tree
directly (no `git diff` extraction), so commits/refs are invisible to scoring.

**The Stipulator's choice.** `prior_attempts` passed to the Stipulator includes
each attempt's checkpoint label + oracle failure. The prompt lists "AVAILABLE
STARTING POINTS" and instructs: use `base` when prior changes were wrong (discard);
use a prior `hK` only when its changes were partially correct (extend). Validated:
on django-11433 with an Opus planner, H3 chose `from=h2` (built on prior) while
H4/H5 chose `from=base` (fresh) — genuine, varied branch decisions.

All checkpoint calls are wrapped defensively (`_cp_snapshot/_cp_restore/_cp_labels`)
— a checkpoint failure emits a `checkpoint_error` trace event and degrades to
forward-editing; it **never** crashes the task.

---

## Model Escalation Tiers

Execution starts on the cheapest model and escalates only when stuck. Planning
(Stipulator + continuation plan) uses a **separate, fixed** `planning_llm` so
hypothesis quality stays constant regardless of the execution tier.

```python
_ESCALATION_TIERS = [
    # (min_hypothesis_n, env_var, fallback_model)
    (0, "CAT_MODEL",               None),                       # T0 cheap (flash)
    (2, "CAT_ESCALATION_MODEL_T1", "openrouter/.../mimo-v2.5"), # T1
    (4, "CAT_ESCALATION_MODEL_T2", "openrouter/.../opus-4.5"),  # T2 strongest
]
```

- **Escalation trigger** (`tier_stuck`): the Performer returned `give_up`/`exhausted`,
  OR there have been **2 oracle failures at the current tier** ("completed but the
  oracle still disagrees" is just as stuck as an explicit give-up).
- On stuck: `current_tier += 1` (next hypothesis uses the stronger model). At the
  top tier, the task fails. A `stipulator_escalation` trace event records each step.
- **All-flash baseline:** define the tiers with `None` fallbacks (and no env vars)
  so every tier resolves to the base model — the loop still runs all `MAX_HYPOTHESES`
  (give_up escalates the *counter*, not the model), exercising branching on one model.
- **planning_llm** is set via `CAT_PLANNING_MODEL` (falls back to the base model).
  Decoupling it is deliberate: a weak planner generates wrong hypotheses that no
  execution model can save, so planning quality is the higher-leverage knob.

**Proof of soundness (django-11433, 2026-06-08):** a task that failed under every
cheaper config passed with Opus planner + flash→mimo→opus execution: H1 flash give_up
→ H2/H3 mimo (H3 built on h2) → H4/H5 opus → PASS. Demonstrates the framework is
sound and **limited only by model capability** — given a strong enough model in the
ladder, the same loop solves what weaker models cannot.

---

## Roles (v3)

| Role | When called | Model | Question it answers |
|---|---|---|---|
| **Stipulator** | Once per hypothesis cycle | `planning_llm` (fixed) | What change satisfies the obligation, and which checkpoint do I branch from? |
| **Performer** | Inner loop, up to K times | `tier_llm[current_tier]` (escalates) | How do I implement this hypothesis? |
| **Oracle** | After every mutation | — (deterministic) | Did the world state match the criterion? |
| **Escalation** | On stuck (give_up / 2 oracle-fails) | — | Move to the next model tier, or fail? |

Resolver and PlanPatcher are **eliminated**. Their function is absorbed:
- Resolver's `patch_plan` → new hypothesis from Stipulator
- PlanPatcher's local edits → Stipulator's hypothesis revision with knowledge context
- Resolver's `forward/backward` → not needed (no plan tree)

---

## What's reused from v2

| Component | Status |
|---|---|
| `cat/llm.py` — LLM client | ✅ reuse, add `call_stipulator_v3` |
| `cat/capabilities.py` — all tools | ✅ reuse, `done`/`give_up` already there |
| `cat/trace.py` — trace events | ✅ reuse, add new event kinds |
| `cat/contracts.py` — Contract | ✅ reuse unchanged |
| `cat/representer.py` — probing | ✅ reuse unchanged |
| `cat/schemas.py` — base schemas | ⚙️ extend with v3 schemas (+ `start_from`) |
| `cat/activator.py` — loop engine | ❌ replace with `activator_v3.py` |
| `cat/plan_tree.py` — plan tree | ❌ delete |
| `cat/resolver.py` — Resolver | ❌ delete |
| `cat/plan_schema.py` — shim | ❌ delete |
| `cat/checkpoint.py` — Checkpointer | 🆕 new: snapshot/restore abstraction |
| `cat/llm.py::call_structured` | 🆕 new: Pydantic structured output (json_schema → json_object → extract → validate) |

---

## Token cost model (expected)

v2 django-15572: 440K tokens, 45 outer iters, 13 outer nodes
v3 expected:     ~100K tokens, 3 hypothesis cycles × ~30K each

Each hypothesis cycle:
- Stipulator: ~3K tokens (enriched context)
- Performer inner loop: ~10-15K tokens (5 attempts × 2-3K each)
- Oracle: 0 tokens
- Attestor: ~2K tokens (only on failure)

Target: **3-5× token reduction** on hard tasks by eliminating intermediate
step orchestration overhead.
