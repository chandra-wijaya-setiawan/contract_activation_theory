# Project Plan: CAT

Contract Activation Theory — contract-disciplined agent on SWE-bench (and
eventually other benchmarks). Theory in **DESIGN.md**. Per-version detail
in **docs/PRD/**.

## Status

| Version | State | Headline |
|---|---|---|
| v1.0 | ✅ 2026-05-14 | Synthetic mechanism: 6× fewer tool calls on 5-step task |
| v1.1 | ✅ 2026-05-20 | terminal-bench: judge-per-tool-call problem identified |
| v1.2 | ✅ 2026-05-26 | LIFE-Harness layers absorbed (later removed in v1.5) |
| v1.3 | ✅ 2026-05-26 | Deterministic test-runner criterion → ReAct parity |
| v1.4 | ✅ 2026-05-27 | swe30 v14 = 13/30 (43.3%) at 3.8× cheaper than ReAct → [PRD](docs/PRD/v1.4_swe_bench_verified.md) |
| v1.5 | ✅ 2026-05-27 | Theory ↔ code aligned, 4 LIFE absorptions removed → [PRD](docs/PRD/v1.5_life_strip.md) |
| v1.6 | ✅ 2026-05-28 | DAG-first observability + terminology rename → [PRD](docs/PRD/v1.6_dag_observability.md) |
| v1.6.5 | ✅ 2026-05-28 | LLM diagnostic auditor (limitation_kind + improvement_vector) |
| v1.6.6 | ✅ 2026-05-28 | swe10 60% → 90% via docker exec + oracle alignment → [PRD](docs/PRD/v1.6.6_tool_reliability.md) |
| v1.6.6.7–14 | ✅ 2026-05-29 | 14-tool surface, −55% tool calls, read truncation nudge → [PRD](docs/PRD/v1.6.6.7-14_tool_surface_expansion.md) |
| v1.7.0 | ✅ 2026-05-30 | Offline CEGAR + Dijkstra simulator validated → [PRD](docs/PRD/v1.7.0_offline_simulation.md) |
| v1.7 A.1 | ✅ 2026-05-30 | Live corpus + LIBRARIAN advisory; smoke 2/3 PASS @ −18 to −25% tokens → [PRD](docs/PRD/v1.7_A1_corpus_librarian.md) |
| v1.7 A.2 | ⏸ deferred | PlanComposer scaffold built (R18) then deferred in favour of contract-discipline pass |
| v1.8 M1–M7 | ✅ 2026-05-31 | Plan-tree + tree-edit ops + Stipulator phased schema + surprise vector + librarian writes; swe10 baseline 8/10 |
| v1.8 R1–R12 | ✅ 2026-05-31 | Attestor-driven step_done, Pydantic plan schema, Revisor → PlanPatcher rename, premature-DONE fixes |
| v1.8 R13–R22 | ✅ 2026-06-01 | OutcomeSchema (R19), tool_choice=required (R19.5), trace surface (R19.10), request_revision escape valve (R22) |
| v1.8 R26 | ✅ 2026-06-01 | Schema library `cat/schemas.py` + Schema-suffix naming; **swe5x5 = 13/25 (52%)** |
| **v1.8 R27** | **✅ 2026-06-02** | **request_revision → `reason` tool arg; swe5x5 = 17/25 (68%), +16pp vs R26** |
| **v1.8 R29** | **✅ 2026-06-02** | **deterministic tool error → skip attestor → Resolver; swe500-partial +12pp (36%→48%)** |

## Next — R37 + R38 (two architectural abstractions)

All previous surgical R30-R36 fixes (OracleFirstPlan, LocateBeforeMutate, MutateBudget,
RollbackOnCorrupted, MultiCandidatePlan, VerifyStrict, TrueRestipulate) collapse into two
root-cause abstractions. Implementing these two generalizes the fix for all those symptoms.

---

### R37 — Full Restipulate (plan-level recovery)

**Problem.** `patch_plan` (the current Resolver decision) calls PlanPatcher to edit nodes
locally. But many failures have a wrong *premise* — wrong file, wrong class list, whole
approach off. Local node edits can't fix a wrong premise. The agent keeps patching until it
hits `max_restip_per_task` and gives up.

Examples that would have been fixed:
- django-15161: Stipulator guessed 6 expression classes, missed `Case`/`When`. 39 iters,
  363K tokens. PlanPatcher spawned C7-C11 as more guesses — still wrong.
- django-13315 (fail trials): Stipulator anchored wrong file. All patches operated on the
  wrong file.
- astropy-13579 (R27 baseline): Wrong file path on line 1. 4 PlanPatcher firings, all
  trying to salvage an unfixable plan.

**What full restipulate is NOT:** calling PlanPatcher. It is calling `call_stipulator_phased`
again — a fresh plan from scratch — but with the failure context injected.

**Design.**

1. **New Resolver decision: `restipulate`** (already reserved in `ResolverDecision` Literal
   since R15a but never chosen). Decision logic:
   - Choose `restipulate` when `patch_plan_count >= 3 AND stall_count > threshold`
   - Or when `deterministic_error` implies wrong-file (e.g. "No such file or directory")
   - Or when verify failure implies scope-miss (the test cites a class not in any mutate step)
   - Cap: `max_restipulates_per_task = 2` (separate from `max_restip_per_task` which governs PlanPatcher)

2. **Activator handles `restipulate` decision:**
   ```
   decision == "restipulate":
     → build RestipulateContext (failure_summary + anti_template + run.messages[:K])
     → call call_stipulator_phased(obligation, restipulate_context)
     → replace plan_tree with new plan
     → reset attention to A1, reset run_counts, keep run.messages (continuity)
     → bump run.restipulate_count
   ```

3. **RestipulateContext injected into Stipulator prompt:**
   - `failure_summary`: last test output + which files were touched + which errors occurred
   - `anti_template`: prior plan's phases/steps that failed (so the new plan avoids same structure)
   - `what_worked`: any steps that already PASSED attestor (don't redo these)

4. **`run.messages` persist.** The new Stipulator call sees the obligation but NOT the
   message history. The Performer still sees the full message history — so it knows what
   was already read/tried and doesn't repeat.

**Key difference from PlanPatcher:**

| | PlanPatcher (`patch_plan`) | Full Restipulate (`restipulate`) |
|---|---|---|
| Scope | One or a few nodes | Entire plan tree |
| Input | Current plan tree + error | Original obligation + failure summary |
| Output | Tree edit ops (RefineOp, BranchOp, etc.) | New `PhasedPlanSchema` |
| When | Local step failure, wrong args | Wrong premise, wrong file, scope miss |
| Cost | 2 LLM calls (Resolver + PlanPatcher) | 2 LLM calls (Resolver + Stipulator) |

Same LLM cost, much wider recovery.

**Files to change:**
- `cat/resolver.py`: add `restipulate` to `ResolverDecision` Literal; update Resolver
  system prompt to guide when to choose it; update `enforce_guards` to handle the new path.
- `cat/activator.py`: handle `restipulate` branch in the Resolver output dispatch;
  add `run.restipulate_count`; add `max_restipulates_per_task` guard; build
  `RestipulateContext` from recent trace events.
- `cat/llm.py`: `call_stipulator_phased` already exists — just needs a `restipulate_context`
  optional arg that enriches the prompt.

**Estimated size:** ~150 LOC across 3 files. 0 new schemas needed.

---

### R38 — Performer Inner React Loop (action-level self-correction)

**Problem.** The Performer today executes exactly ONE LLM call → ONE tool call per logical
iteration, then immediately hands off to the attestor → Resolver pipeline. This means every
micro-failure (wrong old_str, missing file, transient error) pays the full orchestration
tax: attestor LLM call + potential Resolver LLM call + potential PlanPatcher LLM call.

But most micro-failures are self-correctable in 1-2 additional tool calls: read the file →
see the actual content → str_replace with correct content. The model has the capacity; the
framework doesn't give it the chance.

Examples that would have been fixed:
- django-13315 (all fail trials): 3-6 iters of rg/read before str_replace landed, because
  each failed str_replace went through attestor+Resolver before the next try.
- astropy-13579 (R27): `# placeholder` line written, bash errored, Performer read the file
  again — but couldn't just immediately fix the error; had to wait for the next outer iter.
- Every `old_str not found` error: Performer knows it needs to read-then-retry, but the
  framework forces it to wait for full orchestration before the next attempt.

**Design.**

The outer loop in `run()` currently:
```
while iterations < max:
    _perform_once()      ← one LLM call → one tool call
    _extract_outcome()
    _observe_phase()     ← attestor → maybe Resolver
```

R38 changes `_perform_once` into `_perform_loop`:
```
while iterations < max:
    _perform_loop(max_inner=3)   ← up to 3 tool calls, LLM sees each result
    _extract_outcome()           ← summarize all inner calls
    _observe_phase()             ← attestor only sees final state
```

`_perform_loop` internals:
```
for inner in range(max_inner):
    call Performer LLM       # LLM sees all tool results so far in messages
    dispatch tool calls
    if any_durable_change:   # str_replace / write succeeded → stop, commit the change
        break
    if not any_tool_errored: # clean success (read/rg/bash all ok) → stop, hand to attestor
        break
    # error: give Performer one more shot to self-correct
    # (don't break — continue inner loop)
```

Exit conditions:
- **Durable change made** → stop. The mutation landed; attestor should evaluate.
- **No errors** → stop. Clean reads/searches; attestor evaluates.
- **Max inner reached** → stop, escalate normally to attestor → Resolver.
- **Performer emits no tool call** → stop (model says done).

The outer `run.iterations` counter increments ONCE per `_perform_loop` call — the inner
calls are invisible to the outer loop's stall/max_iterations accounting.

**What the attestor sees:** exactly the same as today — the last tool outputs. The inner
loop just means those outputs reflect a sequence of self-correcting actions, not always
just one.

**Cost model:**
- Worst case (all 3 inner calls): +2 Performer LLM calls per iter, but saves
  attestor + Resolver + PlanPatcher calls that would otherwise fire.
- Typical case: inner loop exits after 1-2 calls. Net cost close to neutral.
- Best case: Performer self-corrects without Resolver firing at all. Saves
  2-3 LLM calls and multiple outer iters.

**Key constraint:** `max_inner=3` is conservative. If all 3 inner calls fail (3x
`old_str not found`), the outer loop still escalates normally to the Resolver. R38 adds
a self-correction window; it doesn't replace the Resolver.

**Interaction with R29 (deterministic error):**
R29 fires on the *outer* iter's outcome: if `any_tool_errored` in the final `OutcomeSchema`,
skip attestor and go to Resolver. With R38, `any_tool_errored` reflects the last inner
call. If the Performer self-corrected in inner calls (3rd call succeeded), R29 does NOT
fire — the outer outcome is clean. If all inner calls errored, R29 fires as normal.

**Files to change:**
- `cat/activator.py`: rename `_perform_once` call site to `_perform_loop`; add inner loop
  logic; `_extract_performer_outcome` already handles multi-call message slices.
- `cat/_legacy/activator.py` (PERFORMER_SYSTEM_PROMPT): add rule: "If your tool call
  fails, diagnose the error and call the corrected tool in your NEXT response — you have
  up to 3 attempts before the planner steps in."

**Estimated size:** ~40 LOC in activator.py + 2 prompt lines. Smallest architectural
change; highest expected token efficiency gain.

---

### Implementation order

R38 first (smaller, self-contained, no schema changes, immediately testable via trace).
R37 second (needs RestipulateContext design + resolver prompt tuning).

Together they replace all of R30-R36 with two general mechanisms.

## Roadmap

- **v1.7.0.x — Two-tier feature refactor formalization.** `action_intent` declared at Capability registration, not hardcoded. Code-Python features moved to `domain_features` dict. Sets up cross-domain arrival.
- **v1.7.1 — SkillsBench cross-domain validation.** 94 tasks across 11 professional domains, deterministic per-task verifier, Docker-per-task (same shape as SWE-bench). Folds in head-to-head vs Claude Code / OpenHands / Codex (already on leaderboard). → [PRD](docs/PRD/v1.7.1_skillsbench.md)
- **v1.7.2 — Framework-comparison hardening (gate before v2.0).** Pi + Hermes head-to-head (the leaderboard doesn't cover these); pass^k consistency analysis. → [PRD](docs/PRD/v1.7.2_framework_comparison.md)
- **v1.9 — GAIA (near-verifiable open-ended).** Bridge between deterministic and subjective criteria. Multi-step research questions with exact-match answers.
- **v2.0 — Domain-agnostic expansion.** WebArena + OSWorld + Spider 2.0. → [PRD](docs/PRD/v2.0_cross_domain.md)

## v2.x — Open design questions (deferred, but real)

Known taxes the framework is carrying. Not "we might do this" — architectural answers needed when scale or domain pressure forces it.

- **Procedural memory / automaticity.** Multi-step path retrieval, task-class clustering, consolidation. → [PRD](docs/PRD/v2x_procedural_memory.md)
- **Cross-domain working memory via two-tier features.** Domain-agnostic core (verify_attempt_count, iter_since_durable_change, …) + domain_features extension. → [PRD](docs/PRD/v2x_cross_domain_features.md)
- **Cost-efficiency optimization layer.** Capability granting at admission + history compaction + cost-aware ADVISOR scoring. → [PRD](docs/PRD/v2x_cost_efficiency_layer.md)
- **Speculative activation.** Pre-activate capabilities while ELIGIBLE to reduce latency. v3+ probably.
- **Contract markets.** Auction protocol when multiple agents bid on a contract. Only relevant once multi-agent is real.
- **Persistence and distribution.** Contracts that survive process restarts; cross-agent handoff. Foundations exist (corpus is persistent storage); needs handoff protocol.
- **Multi-agent.** Multiple CAT agents collaborating on decomposed contracts. v3+ work; needs contract-as-message-format first.

## Metrics

All runs use `cat.metrics.RunMetrics` built from JSONL traces. Key signals (aligned with OTel GenAI conventions):

| Signal | Why it matters |
|---|---|
| `tokens_in/out`, `usd_total` | Primary cost driver |
| `llm_calls_by_role` | Per-role overhead split (stipulator / performer / attestor) |
| `tool_calls_by_name` | Which tools dominate |
| `wall_s_llm / wall_s_tools` | Where time is spent |
| `tool_calls_per_task_avg` (1.8.0) | Efficiency canary — current baseline 19.4 (was 42.7 pre-tools) |
| `tool_waste_count` (1.8.0) | Tasks with redundant tool calls (back-to-back, 4+ same-file reads) |
| `limitation_kind` histogram (1.6.5) | Where failures cluster |
| `improvement_vector` histogram (1.6.5) | Where to invest next |

## References

- LIFE-Harness: arxiv 2605.22166 (Xu et al. 2026)
- tau-bench: arxiv 2406.12045, github.com/sierra-research/tau-bench
- SWE-bench: github.com/princeton-nlp/SWE-bench
- AgentBench: github.com/THUDM/AgentBench (ICLR 2024)
- WebArena: github.com/web-arena-x/webarena
- OSWorld: github.com/xlang-ai/OSWorld
- Spider 2.0: arxiv 2411.07763, github.com/xlang-ai/Spider2
- Pi: github.com/earendil-works/pi
- Hermes Agent: github.com/NousResearch/hermes-agent
- Claude Code tools: code.claude.com/docs/en/tools-reference
- TriCEGAR (predicate-tree CEGAR for agent traces): cited in v1.7.0
- Ferns/Precup — Bisimulation Metrics are Optimal Value Functions, UAI 2014
- Step-Level Q-Value Models (Zhai et al., AAAI 2025): arxiv 2409.09345
- Graph Memory + RL: arxiv 2511.07800
- MACLA (hierarchical procedural memory): arxiv 2512.18950
