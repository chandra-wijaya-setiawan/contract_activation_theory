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

## Next — R-chain (ordered by expected ROI)

| R# | Name | Pattern it kills | Size | Status |
|---|---|---|---|---|
| **R30** | **OracleFirstPlan** | Stipulator scope-miss (django-15161: listed wrong classes) | S | 🎯 next |
| **R31** | **LocateBeforeMutate** | Stipulator old_str hallucination (A Pattern) | M | pending |
| **R32** | **MutateBudget** | N successful str_replace on 1 node + verify still fails → BranchOp | S | pending |
| **R33** | **RollbackOnCorrupted** | ImportError/SyntaxError after mutate → git checkout + re-plan | S | pending |
| **R34** | **MultiCandidatePlan** | Wrong-file anchor (C Pattern, django-13315) | L | deferred |
| **R35** | **VerifyStrict** | False-positive verify (B Pattern, test runner exit code) | S | pending |
| **R36** | **TrueRestipulate** | Whole-plan-wrong: Resolver calls full re-stipulate with failure context | M | deferred |

### R30 — OracleFirstPlan (next)
When `INITIAL_TEST_FAILURE` is present in representations (it always is for SWE-bench),
the Stipulator should run the oracle test **as the first MAP step** to discover *which*
classes/functions actually fail — not hallucinate a list from training data.

Current behavior: Stipulator reads the issue text and guesses the list of affected classes
(e.g. C1-C6 for 6 expression classes). Missing ones discovered mid-flight via PlanPatcher.

Fix: add to Stipulator system prompt: "If INITIAL_TEST_FAILURE is present, your first MAP
step MUST be `bash: run the oracle test to see which specific names/classes currently fail`.
All subsequent mutate steps must be grounded in that output."

Costs one extra bash iter per task. Expected payoff: eliminates scope-miss failures on
tasks like django-15161 (363K tokens, 39 iters, missed `Case`/`When`).

### R31 — LocateBeforeMutate
For mutate steps where `suggested_args.old_str` is long (>100c), prepend a locate substep
that reads the file to confirm exact content. PlanPatcher auto-fills `old_str` from the
read result before Performer runs str_replace. Kills the 3-6 iter rg/read churn loop.

### R32 — MutateBudget
If a single mutate node accumulates ≥3 *successful* str_replace calls AND verify still
reports FAIL, auto-fire PlanPatcher with `BranchOp` to try a different approach. Stops
the 6-edit corruption seen in django-13315.4.

### R33 — RollbackOnCorrupted
If verify produces `ImportError`/`SyntaxError`/`IndentationError` after a mutate phase,
run `git checkout <file>` on the touched files and re-plan from scratch. The corrupted
file state was the worst-case terminal failure mode (django-13315.4, astropy-13579 past
runs).

### R35 — VerifyStrict
Capture bash exit code deterministically (already have it in `ToolResult`), and add it to
the attestor's input as `[EXIT_CODE: N]`. For verify-intent steps, `exit_code != 0` →
`complete=False` regardless of attestor LLM judgement. Closes the `--tb=short` invalid-
flag silent-pass and all django runner `FAILED (failures=1)` false-positives.

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
