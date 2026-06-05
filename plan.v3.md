# CAT v3 — Refactor Plan

> Companion: `design.v3.md`  
> Strategy: strangler fig — new `activator_v3.py` alongside old `activator.py`,  
> validate on swe10 × 3, then delete old code.

Status legend: `pending` · `in_progress` · `done` · `skip`

---

## Phase 1 — Schemas (cat/schemas.py)

| # | Task | Status | Notes |
|---|---|---|---|
| 1.1 | Add `HypothesisSchema` {location, change, predicts, basis} | `done` | New — replaces PhasedPlanSchema |
| 1.2 | Add `PerformerTaskSchema` {obligation, hypothesis, oracle, context, max_attempts} | `done` | New — what Performer receives |
| 1.3 | Add `KnowledgeSchema` {findings, failed_hypotheses, oracle_failures} | `done` | New — knowledge transfer between hypotheses |
| 1.4 | Keep existing `ExecutionSchema`, `PerformerResultSchema` | `done` | Already added in R37+R38 |
| 1.5 | Add `AttestorV3Schema` {verdict: "new_hypothesis"\|"give_up", reason, diagnosis} | `done` | Simpler than v2 AttestorSchema |

---

## Phase 2 — Stipulator (cat/llm.py)

| # | Task | Status | Notes |
|---|---|---|---|
| 2.1 | Add `STIPULATOR_V3_SYSTEM_PROMPT` — outputs HypothesisSchema not PhasedPlan | `done` | One hypothesis, not multi-step plan |
| 2.2 | Add `call_stipulator_v3(llm, obligation, representations, knowledge, prior_attempts)` | `done` | Returns HypothesisSchema |
| 2.3 | Existing `call_stipulator_phased` stays for regression baseline | `done` | Keep until v3 validated |
| 2.4 | Add `ATTESTOR_V3_SYSTEM_PROMPT` — verdict: new_hypothesis or give_up | `pending` | Simpler than v2 (only 2 verdicts) |
| 2.5 | Add `call_attestor_v3(llm, result, knowledge, prior_attempts)` | `done` | Returns AttestorV3Schema |

---

## Phase 3 — Performer inner loop (cat/activator_v3.py)

| # | Task | Status | Notes |
|---|---|---|---|
| 3.1 | Create `cat/activator_v3.py` skeleton — class `ActivatorV3` | `done` | New file, old activator untouched |
| 3.2 | Implement `_perform_loop(oracle, hypothesis, max_attempts=5)` | `done` | Same inner loop as R38 but with PerformerTaskSchema input |
| 3.3 | Oracle check after every mutation inside inner loop | `done` | Not just at verify step |
| 3.4 | `done()`/`give_up()` signals already registered in capabilities | `done` | R37+R38 |
| 3.5 | Extract `findings` from inner loop messages | `done` | Key facts for KnowledgeSchema |
| 3.6 | Exhaustion summarisation LLM call | `done` | Already in R38 |

---

## Phase 4 — Outer loop (cat/activator_v3.py)

| # | Task | Status | Notes |
|---|---|---|---|
| 4.1 | Implement `run(contract, state)` outer loop | `done` | MAX_HYPOTHESES=3 |
| 4.2 | KnowledgeSchema accumulation across hypotheses | `done` | findings + failed_hypotheses |
| 4.3 | Oracle check after each `_perform_loop` | `done` | Contract fulfilled → return immediately |
| 4.4 | Call `call_attestor_v3` only on oracle failure | `done` | Not every iter |
| 4.5 | Pass enriched KnowledgeSchema to Stipulator on next hypothesis | `done` | Key for token savings |
| 4.6 | `_represent` + `_admit` unchanged — reuse from base Activator | `done` | Inherit from existing |

---

## Phase 5 — Wire up

| # | Task | Status | Notes |
|---|---|---|---|
| 5.1 | `benchmarks/swe_bench.py` — add flag `USE_V3=True` env var | `done` | Switch between v2 and v3 |
| 5.2 | `scripts/run_bench.sh` — add `--v3` flag | `pending` | Pass USE_V3 |
| 5.3 | Trace events: add `hypothesis_attempt`, `knowledge_update` to EventKind | `done` | cat/trace.py |
| 5.4 | Update `scripts/launch_swe500.sh` to support v3 flag | `pending` | |

---

## Phase 6 — Tests

| # | Task | Status | Notes |
|---|---|---|---|
| 6.1 | Unit test: `HypothesisSchema` validates correctly | `done` | |
| 6.2 | Unit test: `KnowledgeSchema` accumulation | `done` | |
| 6.3 | Unit test: `_perform_loop` exits on `done()`/`give_up()` | `pending` | |
| 6.4 | Unit test: oracle check after mutation | `pending` | |
| 6.5 | Smoke test: 1 swe task with `USE_V3=True` | `in_progress` | django-11239 (known easier) |

---

## Phase 7 — Validation

| # | Task | Status | Notes |
|---|---|---|---|
| 7.1 | Run swe10 × 3 with v3 | `pending` | Compare vs v2 baseline (16/30 = 53%) |
| 7.2 | Compare token cost per trial | `pending` | Target: 3-5× reduction on hard tasks |
| 7.3 | Trace analysis: hypothesis count, oracle check frequency | `pending` | Confirm oracle fires after mutations |
| 7.4 | v3 pass rate ≥ v2 pass rate → proceed to cleanup | `pending` | Gate condition |

---

## Phase 8 — Cleanup (after Phase 7 passes gate)

| # | Task | Status | Notes |
|---|---|---|---|
| 8.1 | Delete `cat/plan_tree.py` | `pending` | No plan tree in v3 |
| 8.2 | Delete `cat/resolver.py` | `pending` | Resolver eliminated |
| 8.3 | Delete `cat/plan_schema.py` | `pending` | Shim no longer needed |
| 8.4 | Delete `cat/_legacy/activator.py` PERFORMER_SYSTEM_PROMPT (v2 version) | `pending` | New system prompt in activator_v3 |
| 8.5 | Rename `activator_v3.py` → `activator.py` | `pending` | |
| 8.6 | Delete old `activator.py` | `pending` | |
| 8.7 | Update `DESIGN.md` to point to `design.v3.md` | `pending` | |
| 8.8 | Update `PLAN.md` status table | `pending` | |

---

## Milestone summary

| Milestone | Gate condition | Status |
|---|---|---|
| M1 — Schemas done | All Phase 1+2 tasks done, tests pass | `done` |
| M2 — v3 loop runs | Smoke test passes (django-11239) | `in_progress` |
| M3 — v3 validated | swe10×3 v3 ≥ v2 AND token cost down | `pending` |
| M4 — v2 deleted | All cleanup done, single activator | `pending` |
