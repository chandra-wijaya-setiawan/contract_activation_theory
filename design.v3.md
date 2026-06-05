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
    location:  str   # WHERE to act — file, function, API endpoint, record ID
    change:    str   # WHAT to do — specific enough for Performer to execute
    predicts:  str   # WHAT will be true after — verifiable statement
    basis:     str   # WHY this hypothesis — reasoning from obligation + knowledge
```

**Not** a step-by-step plan. One specific prediction about the world state
after the fix. The Performer figures out HOW to implement it.

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

    for hypothesis_n in range(MAX_HYPOTHESES=3):

        # Stipulator generates ONE specific hypothesis
        hypothesis = call_stipulator_v3(
            obligation    = contract.obligation,
            representations = contract.representations,
            knowledge     = knowledge,
            prior_attempts = prior_attempts,
        )

        # Performer implements it (inner loop)
        result = perform(contract.oracle, hypothesis, max_attempts=5)

        # Oracle is the ONLY judge — check after every mutation
        if oracle_passes(contract, state):
            return FULFILLED

        # Collect knowledge regardless of outcome
        knowledge.findings.update(result.findings)
        prior_attempts.append({
            "hypothesis": hypothesis,
            "verdict": result.verdict,
            "reason": result.reason,
        })

        # Attestor only when oracle failed — decide: try again or quit
        if result.verdict == "give_up":
            attest = call_attestor_v3(result, knowledge, prior_attempts)
            if attest.verdict == "give_up":
                break
            # else: continue to next hypothesis

    return FAILED
```

**No plan tree. No stall_count. No Resolver. No PlanPatcher.**

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

---

## Roles (v3)

| Role | When called | Question it answers |
|---|---|---|
| **Stipulator** | Once per hypothesis cycle | What specific change will satisfy the obligation? |
| **Performer** | Inner loop, up to K times | How do I implement this hypothesis? |
| **Oracle** | After every mutation | Did the world state match the criterion? |
| **Attestor** | Only on oracle failure | Should we try a new hypothesis or give up? |

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
| `cat/schemas.py` — base schemas | ⚙️ extend with v3 schemas |
| `cat/activator.py` — loop engine | ❌ replace with `activator_v3.py` |
| `cat/plan_tree.py` — plan tree | ❌ delete |
| `cat/resolver.py` — Resolver | ❌ delete |
| `cat/plan_schema.py` — shim | ❌ delete |

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
