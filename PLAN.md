# Project Plan: CAT

Contract Activation Theory — a contract-disciplined agent that runs on SWE-bench
(and eventually other benchmarks). See **DESIGN.md** for theory and architecture.

This document is the source of truth for *what's been done* and *what comes next*.
Detailed per-iteration decisions live in git commit messages and in
`docs/<version>_PRD_*.md`; the version sections below summarize without
duplicating that history.

---

## Status board

| Version | Domain | State | Headline |
|---|---|---|---|
| v1.0 | synthetic | ✅ DONE 2026-05-14 | mechanism proven: 6× fewer tool calls, 10× fewer tokens on 5-step |
| v1.1 | terminal-bench (LLM judge) | ✅ DONE 2026-05-20 | judge-per-tool-call was wrong; led to v1.3 fix |
| v1.2 | synthetic coverage + LIFE absorption | ✅ DONE 2026-05-26 | 100% solve on all tasks; LIFE layers absorbed |
| v1.3 | terminal-bench (deterministic) | ✅ DONE 2026-05-26 | parity with ReAct after judge-once-per-task fix |
| v1.4 | SWE-bench verified | ✅ DONE 2026-05-27 | swe30 v14 = 13/30 = 43.3% @ 3.8× cheaper than ReAct |
| v1.5 | LIFE strip + admission gate | ✅ DONE 2026-05-27 | theory ↔ code aligned; 4 LIFE absorptions removed |
| v1.6 | DAG-first observability + terminology | ✅ DONE 2026-05-28 | parent_id discipline, RunDAG, HTML reports, terminology rename |
| v1.6.5 | diagnostic taxonomy + HTML report | ✅ DONE 2026-05-28 | LLM-attribution with limitation_kind / improvement_vector |
| v1.6.6 | tool reliability | ✅ DONE 2026-05-28 | swe10 60% → 90% (docker exec + oracle alignment) |
| v1.6.6.7–14 | tool surface expansion + tightening | ✅ DONE 2026-05-29 | 14 capabilities, −55% tool calls, read truncation nudge |
| v1.7.0 | offline simulation (CEGAR + Dijkstra) | ✅ DONE 2026-05-30 | math validated: 22/30 splits domain-agnostic, d* meaningful |
| v1.7 Phase A.1 | corpus + LIBRARIAN (advisory) | ✅ DONE 2026-05-30 | SQLite corpus, runtime hint injection, smoke 2/3 PASS @ −18 to −25% tokens |
| **v1.7 Phase A.2** | **ADVISOR + ELECTION (arbitration)** | **🎯 NEXT** | **math arbitrates between N=2 LLM candidates** |
| v1.7 Phase B | SUSPENDED resume (DAG-grounded) | queued | after A.2 |
| v1.7 Phase C | A* heuristic h(v) from corpus | queued | after A.2 |
| v1.4 tau-bench | cross-domain validation | future | post-v1.7 |
| v1.8 hardening | head-to-head vs Hermes/Claude Code | future | gate before v2.0 |
| v2.x | learning system, cross-domain, cost-eff layer | future | open design questions section below |

---

## Next: v1.7 Phase A.2 — ADVISOR + ELECTION

**Goal:** make the math actually control behavior. Today (A.1) the LIBRARIAN
returns a Dijkstra recommendation as a text hint; the LLM may or may not heed
it. A.2 introduces multi-candidate generation + math-arbitrated selection.

**Concrete change:**

1. **Performer prompt update** — ask LLM to emit **N=2** candidate tool calls
   per turn (or split into two turns: emit one, get hint, emit alternative).
2. **`Librarian.score_candidates(candidates, raw_features) → ScoredList`** —
   for each candidate action, compute `score = expected_cost + d*(next_state | s, a)`.
   Unknown (s, a) pairs fall back to a global prior with a `defer=True` flag.
3. **ELECTION step in `Activator._perform_once`** — execute the lowest-scored
   candidate. If all candidates DEFER (no corpus signal), execute the first
   one (LLM's preferred choice — preserves backward-compat behavior on cold
   states).
4. **`election` trace event** — records winner + losers + scores so we can
   audit "would have picked X, elected Y" cases.
5. **Test** — synthetic case where the corpus has a clearly cheaper path;
   verify ELECTION picks it over the LLM's default.

**Validation:** rerun the 3-task smoke with A.2 enabled. Compare:
- Same 2/3 PASS rate (no regression)
- Tool counts and per-task tokens vs A.1 advisory
- Trace `election` events: how often does math override the LLM, and on
  what kinds of actions?

**Estimated work:** ~150 lines across `activator.py` + `librarian.py` + a
small `PERFORMER_SYSTEM_PROMPT` change. ~1 hour focused.

**Out of scope (next next):** Beta(α,β) confidence floor, UCB exploration
bonus, belief_split trigger for self-loops, LIBRARIAN with sentence-
transformers for cross-task obligation similarity.

---

## Done — version-by-version history

The detail below is reference: what was decided, what shipped, what numbers
came out. Use git commits + `docs/<version>_PRD_*.md` files for finer detail.

### v1.0 — Synthetic proof of mechanism (DONE 2026-05-14)

- Core runtime: Contract, Criterion, Activator, Capability, State, Recorder
- CatAgent (contract-disciplined) + ReactAgent (vanilla) sharing same LLM + tool stack
- 2 synthetic tasks (hello-file 1-step, multi-step-refactor 5-step), 12 trials
- **Result:** CAT 6× fewer tool calls, 10× fewer tokens on 5-step task
- Criterion: deterministic Python predicate (file existence + content match)

### v1.1 — terminal-bench, real Docker tasks (DONE 2026-05-20)

- Tasks: hello-world, fix-pandas-version, csv-to-parquet (Docker via ssh://cpubox)
- **Result:** ReAct 88.9% vs CAT 55.6%. CAT costs 4× more ($0.012 vs $0.003)
- Root causes:
  1. LLM judge called 3× per tool call → 4 LLM calls per tool call (vs ReAct's 1)
  2. LLM judge fires on partial progress → premature closure
- **Lesson:** deterministic criterion is mandatory; LLM judge is per-task-end only

### v1.2 — Synthetic coverage + LIFE-Harness layers (DONE 2026-05-26)

- 3 new synthetic tasks (bug-fix ~8-step, multi-file-rename ~10-step, test-generation ~12-step)
- Composable oracle predicates: file_exists, file_contains, bash_succeeds, ast_has_function, all_of, any_of
- LIFE-Harness layers absorbed (arxiv 2605.22166): Action Realizer, Environment Spec, Circuit Breaker, Failure Attribution
- **Result:** CAT 3.5× cheaper on hardest task; 100% solve rate for both agents
- **Later observation (driving v1.5):** the LIFE absorptions were 4 out of 5 load-bearing CAT mechanisms — theory ↔ code mismatch

### v1.3 — terminal-bench deterministic criterion rerun (DONE 2026-05-26)

- Removed pre-activation `_verify_criteria` (judge called once per tool call, not twice)
- Added `_make_test_runner_criterion(session, test_cmd)` — runs `<test_cmd>; echo EXIT:$?`, parses exit code
- **Result:** CAT 88.9% = ReAct 88.9% — exact parity. Pass@2 = 100%
- **Lesson:** LLM judge is safe ONLY at task end, never per tool call

### v1.4 — SWE-bench verified (DONE 2026-05-27)

Goal: validate CAT on real GitHub issues with deterministic pytest criterion.

**Critical fixes shipped (2026-05-26):**
1. `TbSweAgent._resolve_test_cmd()`: was using LLM judge for 26/30 tasks (oracle missing) → dynamic oracle from config.json
2. REPRESENT step: missing file_types and notes probes (planner blind to archive types)
3. Planner now sees pre-plan oracle failure output via `_initial_representations()` — generates targeted steps

**Results — swe30 v14 vs baselines (30 tasks, 1 attempt):**

| Metric | CAT v14 | CAT v0 | ReAct |
|---|---|---|---|
| Solve rate | **13/30 = 43.3%** | 9/30 = 30% | 15/30 = 50% |
| Tokens per solved task | **92K** | 178K | 354K |
| Token efficiency vs ReAct | **3.8× cheaper** | 2.0× cheaper | baseline |

- CAT v14 only wins (4): matplotlib-26342, sklearn-26194, pytest-10081, pytest-6202
- ReAct only wins (6): django-11239, django-12039, django-13315, django-14434, sympy-15349, sympy-15875
- ReAct still better on exploratory tasks (large Django/sympy codebases)

### v1.5 — Delete LIFE, realign to admission-gate theory (DONE 2026-05-27)

**Why:** Of 5 load-bearing CAT mechanisms in DESIGN.md, 4 were LIFE imports
(Action Realizer, Environment Spec preconditions, Circuit Breaker F2/F3/F4,
Failure Attribution taxonomy). The layer the theory is *named* after
(activation) was rhetorical — Step 6 was structurally identical to ReAct's act.

**Shipped:**
- Stripped all 4 LIFE imports from `cat/`
- Renamed Step 6 ACTIVATE → INVOKE
- Step 4 ACTIVATE is now the one-shot admission gate (boundary/environment/capacity → capability grant)
- Contract lifecycle as state machine (LATENT / ELIGIBLE / ACTIVE / SUSPENDED / FULFILLED / FAILED / EXPIRED)
- 60+ unit tests passing on the stripped code

**Gate criteria all passed:** no LIFE refs in `cat/`, tests green, end-to-end SWE run works.

**Decision log** (full detail in commits):
- LIFE Circuit Breaker (F2 replan) replaced by SUSPENDED-with-watcher event-driven re-admission
- Failure Attribution taxonomy replaced by contract-state reasons (which Criterion / Representation / admission check)
- Action Realizer removed; tool errors surface naturally
- Tool preconditions removed; tools just run and report errors

### v1.6 — DAG-first observability + terminology alignment (DONE 2026-05-28)

> See `docs/1.6.2.9_PRD_renaming_and_dijkstra.md` for the canonical terminology
> rename decision record.

**Goal:** promote the trace from flat event stream to typed reasoning DAG.

**Shipped:**
- `parent_id` discipline audit: every non-root event has a direct causal parent
- `cat/dag.py` — RunDAG class with longest_chain / branch_points / path_to queries
- `cat/dag_svg.py` — inline SVG visualizer (no external graphviz)
- HTML KPI report (`cat/run_report.py`): DAG SVG + reasoning trace + cost-by-layer + cross-run INDEX
- Structured run output layout: `data/runs/<bench>/<date>/<time>-<runid>/`
- `scripts/run_bench.sh` launcher with manifest.json + auto-report
- Terminology rename per PRD 1.6.2.9:
  - role tags: `planner` → `stipulator`, `executor` → `performer`, `judge` → `attestor`
  - methods: `_decompose` → `_stipulate`, `_invoke_once` → `_perform_once`
  - event kinds: `contract_decompose` → `contract_stipulate`, `capability_invoke` → `capability_perform`
  - step names: DECOMPOSE → STIPULATE, INVOKE → PERFORM, CLOSE → DISCHARGE
  - backward-compat aliases preserved in `cat/dag.py` event-kind map

**Gate:** 62/62 tests passing, sklearn-14141 PASS with new event kinds.

### v1.6.5 — Diagnostic taxonomy + LLM attribution (DONE 2026-05-28)

- `cat/attribution.py`: Attribution dataclass with limitation_kind, limitation_secondary, improvement_vector, improvement_specifics, narrative, confidence
- v2 auditor prompt with 4-axis taxonomy (limitation_kind ∈ {framework_structural, framework_parameter, model_intelligence, model_prompt, tool_environment, task_complexity, none}; improvement_vector ∈ {framework_code, prompt, model, tool, infra, accept, none})
- Parallelized attribution via ThreadPoolExecutor in run_report
- HTML report includes per-task Diagnosis block + run-level histograms

### v1.6.6 — Tool reliability (DONE 2026-05-28)

**Headline:** swe10 PASS rate lifted from 60% (v1.5 baseline) to 90%.

| Fix | Effect |
|---|---|
| 1.6.6.1 `docker exec` backend (replaces tmux) | tool error rate 80% → 21%; 3 of 4 v1.5 FAILs flip to PASS |
| 1.6.6.2 first-class `rg` + `fd` | sharper search primitives |
| 1.6.6.4 oracle restores `tests/` before applying test_patch | eliminates agent-oracle ↔ harness-verifier divergence |
| 1.6.6.5 PERFORMER prompt: do not edit test files | preventive |
| 1.6.6.6 oracle includes PASS_TO_PASS regression sample (≤30) | catches regressions agent would otherwise stop on |
| `bash -lc` login shell in docker exec | fixes PATH/conda env issues (sphinx-10323 recovery) |

**Solve-rate progression on swe10:**

| Run | Pass rate | Notable |
|---|---|---|
| v1.5-final (LIFE-stripped, tmux backend) | 6/10 = 60% | 4 FAILs all diagnosed `tool_environment` |
| swe10-v1.6.6 (docker exec + rg/fd only) | 8/10 = 80% | 3 django FAILs → PASS; sphinx regressed (PATH) |
| swe10-v1.6.6-all (+ bash -lc + oracle restore + prompt) | 8/10 = 80% | sphinx back; matplotlib regressed |
| **swe10-v1.6.6-final (+ PASS_TO_PASS in oracle)** | **9/10 = 90%** | only django-13315 remains |

**+30 percentage points from one sprint of infrastructure work.** The diagnostic auditor was correct: 4/4 v1.5 FAILs were `tool_environment`; once that bucket emptied, the headline jumped.

### v1.6.6.7–14 — Tool-surface expansion + tightening (DONE 2026-05-29)

**Capabilities added:**

| # | Capability | Pattern source | Purpose |
|---|---|---|---|
| 1.6.6.7 | `apply_patch(patch)` | Hermes | Multi-hunk diff via `git apply --reject --whitespace=fix`; partial-apply with .rej listing |
| 1.6.6.8 | `multi_edit(path, edits[])` | Pi `edit.ts` | Atomic N-hunk edit against ORIGINAL — order-independent, drift-free |
| 1.6.6.9 | `git_status` + `git_diff` first-class | own | First-class git tools so trace records accumulated changes |
| 1.6.6.10 | `rg(context=N)` | Pi `grep.context` | -C N on existing ripgrep wrapper |
| 1.7.0 | `goto_def`, `find_refs`, `outline` | Claude Code `LSP` | Jedi-backed Python navigation; lazy `pip install --user jedi` |
| 1.8.0 | Tool-economy KPI | CAT-original | Per-task `tool_usage` + redundancy flags; HTML "Tool economy" panel |
| 1.6.6.11 | stipulator SWE-bench guidance | own | "Don't plan to add tests" — closes django-13315 deadlock class |
| 1.6.6.12 | pytest signal extractor (`cat/test_signals.py`) | own | Extracts top repo-internal frame from pytest traceback → stipulator evidence |
| 1.6.6.13 | cost-per-PASS analysis (no code) | own | Diagnosed token bloat → drove 1.6.6.14 |
| 1.6.6.14 | read truncation nudge (Pi/Claude-Code) | Pi | "showing X of Y lines; use offset=N to continue" + description nudge |

11 total tools in docker registry (was 7). `PERFORMER_SYSTEM_PROMPT` expanded with tool-selection guidance.

**Results — swe10 v1.7-tools vs v1.6.6-final baseline:**
- 9/10 effective PASS (parity)
- **−55% tool calls per task** (42.7 → 19.4)
- **−46% wall time** (92 min → 49.5 min)
- +47% USD (driver: bigger per-call context from richer tool results; analysis in 1.6.6.13)

**Bottleneck migration:**

| Generation | Dominant failure layer | Fix vector |
|---|---|---|
| v1.5 | `tool_environment` (tmux) | v1.6.6 docker exec |
| v1.6.6 | `tool_environment` (oracle ≠ harness) | v1.6.6.4–6 oracle alignment |
| v1.7-tools | `framework_structural` + `framework_code` (missing tools + bad plans) | v1.6.6.7–11 tools + stipulator guidance |
| **post-1.6.6.11** | **`framework_parameter` (planner picks wrong target)** | **v1.7 learning loop** |

Each generation peeled the next layer up.

### v1.7.0 — Offline simulation: CEGAR + Dijkstra (DONE 2026-05-30)

External research (TriCEGAR mechanism + Ferns/Precup criterion + Dijkstra
objective) reframed the v1.7 design as:
- raw_features stay UNHASHED — predicate tree applies a function over them
- starts maximally coarse (one leaf per obligation), splits earn into the tree via counterexamples
- Trigger B (value_split): leaf with highest CV of realized_cost_to_goal
- Decision-tree-stump split: minimum weighted variance on raw_features (hand-rolled, no sklearn)
- Backward Dijkstra → d*(s) per leaf
- Two-tier features: domain-agnostic core vs domain-specific extensions

**Validated on merged swe10 corpus (19 traces, 392 samples):**
- 38 leaves after CEGAR refinement (was 10 coarse leaves at start)
- **22 of 30 splits domain-agnostic** (73%) — `verify_attempt_count`, `iter_since_durable_change`, `durable_change_count`
- d* values now meaningful (range 0–285K tokens, not all zero like v1)
- Predicates picked match human-predicted failure modes (pytest-loop, drift signals, post-edit state)

**Artifacts:**
- `scripts/dijsktra_simulation.py` — POC simulator
- `docs/dijsktralayer_manual_by_hand_draft.md` — 12-section walkthrough with real data

### v1.7 Phase A.1 — Corpus + LIBRARIAN (advisory) (DONE 2026-05-30)

Wires the simulator into runtime as a lightweight advisor.

**Shipped:**
- `cat/state_class.py` — shared canonicalization (Sample, PredicateTree, action_intent, abstract counters)
- `cat/corpus.py` — SQLite (trajectory + sample tables); dedup on trace_id
- `cat/librarian.py` — runtime query: `recommend(raw_features) → Optional[Recommendation]`
- `cat/activator.py` — optional `librarian=` kwarg; `_librarian_hint()` builds transient system message
- `cat/tb_agents.py` — constructs Librarian when `CAT_CORPUS_DB` env var is set
- `scripts/seed_corpus.py` — bulk ingest from existing run dirs
- `scripts/run_bench.sh` — post-run writeback after each run
- Seeded corpus: 25 trajectories, 540 samples (after smoke writeback)
- 94/94 tests passing (6 new for corpus + librarian)

**Smoke test (3 tasks):**

| Task | v1.7-tools baseline | A.1 (with Librarian) | Δ tokens |
|---|---|---|---|
| pytest-6202 | PASS, 28K | PASS, 32K | +14% (hint overhead on small task) |
| django-12039 | PASS, 445K | PASS, 365K | **−18%** (−80K) |
| django-13315 | FAIL, 489K | FAIL, 368K | **−25%** (−121K) |

Same solve rate, significant token savings on bigger tasks. Hint IS visible to the LLM but math doesn't FORCE the choice — that's Phase A.2.

**Bug found mid-smoke:** docker compose project names must be lowercase. Added guard in `run_bench.sh` (commit `df104ba`).

---

## Roadmap — what comes after v1.7 A.2

### v1.7 Phase B — SUSPENDED resume (DAG-grounded)

When contract enters SUSPENDED, walk DAG back to last `neutral` ancestor,
prune dead-end subtree, resume from there. Separable from A.1/A.2 but
reuses the same outcome-tagged DAG.

### v1.7 Phase C — A* heuristic h(v) learned from corpus

Upgrade Dijkstra to A*: learned admissible-by-construction heuristic
h(v) = 25th-percentile cost-to-goal of past PASS trajectories through
similar states. Ships after Phase A has a stable interface.

### v1.7.0.x — Two-tier feature refactor formalization

`action_intent` classification declared at Capability-registration time
(not hardcoded). Move code-Python-specific features (`workdir_sig`,
`env_facts`, `last_bash_class`) into `domain_features` dict. Sets up
cross-domain transfer for tau-bench arrival.

Already drafted in v1.7.0 work — just needs the cleanup pass.

### v1.4 — tau-bench (cross-domain validation)

Goal: validate CAT on customer-service conversations (non-coding).

- Simulated CS conversations; LLM plays customer, agent uses domain API
  tools (cancel_order, change_flight, etc.); oracle compares final DB
  state against ground truth
- Capability adapter: wrap each tau-bench API tool as a CAT Capability
- Criterion adapter: row-by-row DB state equality
- User simulator: tau-bench provides LLM-based user; need to wire into
  conversation loop (new interaction pattern for CAT)
- Retail + airline domains, ≥30 instances each, 4 reps for pass^k

**Risk:** user simulator adds a second LLM call per turn — doubles cost
on this domain. Acceptable.

### v1.8 — Coding domain hardening (gate before v2.0)

- **Head-to-head vs established agent** — pick one of Pi, Hermes,
  Claude Code; run on SWE-bench Lite; target ≥ parity on solve rate +
  measurable token-cost advantage
- **AgentBench subset** (THUDM, ICLR 2024) — OS environment (Linux shell,
  144 tasks), DBBench (SQL, 261 tasks); deterministic, comparable to
  published numbers
- **Pass^k across environments** — pass^1, pass^2, pass^4 for SWE-bench,
  tau-bench, AgentBench; consistency is CAT's differentiator

**Gate criteria:**
- ≥ parity on solve rate vs one established agent
- Measurably cheaper (tokens or USD) on same benchmark
- Pass^4 > 50% on solved instances
- Internal report or external paper

### v1.9 — GAIA (near-verifiable open-ended)

Bridge between deterministic and subjective criteria. Multi-step research
questions with exact-match or near-match answers (Wikipedia-grounded).
Three difficulty levels. Tests whether plan-first discipline reduces
unnecessary web lookups on hard questions.

### v2.0 — Domain-agnostic expansion (gated on v1.8)

| Benchmark | Domain | Tasks | Oracle |
|---|---|---|---|
| WebArena | Web browsing | 812 | Page state validators |
| OSWorld | Desktop control | 369 | System state validators |
| Spider 2.0 | Enterprise SQL | 632 | Query result comparison |

Adapter framework: generic base class for connecting external benchmarks
(task loader, capability wrapper, criterion mapper, grader adapter).
One module per benchmark under `cat/bench/`.

---

## v2.x — Open design questions (deferred, but real)

These are not "we might do this" — they are **known taxes the framework is
carrying** that need architectural answers when scale or domain demands it.

### Procedural memory / automaticity

Task classification + cued retrieval of past DAGs as reusable skills.
Brain analogy: declarative (in-context per run) vs procedural (DAG corpus
across runs). See DESIGN.md "Declarative vs procedural memory" section.

Predicted observable: token cost per task drops on recurring patterns as
the agent develops automaticity. Requires v1.7 DAG infrastructure first
(now in place).

### Cross-domain working memory via two-tier features

*(forward note from 1.7.0 v1 simulation, 2026-05-30)* — v1.7 builds the
offline-RL + online-search loop on swe10 (code domain). The framework
GENERALIZES to other domains (tau-bench, WebArena, OSWorld) only if the
canonicalization separates **domain-agnostic core features** from
**domain-specific extensions**.

**Domain-agnostic core** (every domain populates):
- `obligation_hash`, `plan_idx`, `last_outcome`
- `durable_change_count`, `iter_since_durable_change`
- `verify_attempt_count`, `investigate_attempt_count`, `mutate_attempt_count`
- `n_unique_actions`, `action_intent ∈ {investigate, mutate, verify, other}`

**Domain-specific extensions** (sparse dict):
- code-Python: `{workdir_sig, env_facts, last_bash_class}`
- tau-bench: `{api_endpoint_class, db_table_touched, ...}`
- WebArena: `{dom_section_class, last_form_id, ...}`

Each Capability registration declares an `intent` classification. The
CEGAR predicate tree learns predicates over domain-agnostic features
primarily — `verify_attempt_count > 3` fires identically in swe10 and
tau-bench, catching domain-independent failure modes. LIBRARIAN does
cross-domain *action-level* transfer via embedding; predicate tree does
cross-domain *reasoning-pattern* transfer via shared features.

The minimum refactor is queued as **v1.7.0.x** above. Bigger question
(whether REPRESENT/STIPULATE/PERFORM/VERIFY themselves need cross-domain
abstraction) — deferred to v2.x when a second domain creates pressure.

### Cost-efficiency optimization layer

*(forward note from 1.6.6.13)* — v1.7-tools cost analysis revealed three
structural sources of per-call cost inflation:

1. **Static tool-schema overhead** — every performer LLM call carries all
   14 tool schemas (~1KB JSON), even when the contract only needs 3 of them.
   Fix: **capability granting at the admission gate (Step 4)** — only
   register tools the contract genuinely requires. Co-developed with v1.7
   procedural memory (learns the contract → required-tool mapping).

2. **History accumulation through context** — by turn 10, the LLM
   re-reads 9 prior tool results every call. No in-context compaction
   beyond gross turn-summary squashing. Fix: **tool-result summarization
   at history-write time** (replace verbose results with their salient
   fact once the agent has moved on).

3. **LLM tool-selection bias** — LLM defaults to `read` (familiar) over
   `outline` (cheaper). 1.6.6.14 patches with description nudge; the
   principled fix is **cost-aware multi-candidate generation at Step 5b**
   (ADVISOR scores partly on expected-bytes-out, not just plan fit).

These three form a coherent "cost-efficiency layer" — schema granting,
history compaction, cost-aware candidate scoring. None ships before v1.7+.

### Speculative activation

Pre-activate capabilities while ELIGIBLE to reduce latency. v3+ probably.

### Contract markets

Auction protocol when multiple agents bid on a contract. Only relevant
if multi-agent (below) is real.

### Persistence and distribution

Contracts that survive process restarts; cross-agent handoff. Foundations
already exist (the corpus is persistent storage); just needs the
hand-off protocol.

### Multi-agent

Multiple CAT agents collaborating on decomposed contracts. Plausible
v3 work; needs contract-as-message-format first.

---

## Metrics (all versions)

All runs use `cat.metrics.RunMetrics` built from JSONL traces. Key tracked
signals (aligned with OTel GenAI semantic conventions):

| Signal | OTel field | Why it matters |
|---|---|---|
| `tokens_in/out` | `gen_ai.usage.input_tokens` | Primary cost driver |
| `tokens_cache_read` | `gen_ai.usage.cache_read.*` | Prompt caching savings |
| `llm_calls_by_role` | `gen_ai.operation` (extended) | Per-role overhead split |
| `tool_calls_by_name` | `gen_ai.tool.name` | Which tools dominate |
| `tool_errors` | — | Reliability signal |
| `wall_s_llm / wall_s_tools` | `gen_ai.client.operation.duration` | Where time is spent |
| `max_prompt_tokens` | — | Context window pressure |
| `judge_overhead_fraction` | CAT-specific | Judge as % of total tokens |
| `criterion_checks` | CAT-specific | How often criterion evaluated |
| `plan_completion_rate` | CAT-specific | Did executor follow the plan? |
| `budget_*_used_pct` | CAT-specific | Budget utilization per constraint |
| `tool_calls_per_task_avg` | CAT-specific (1.8.0) | Efficiency canary |
| `tool_waste_count` | CAT-specific (1.8.0) | Tasks with redundant tool calls |

---

## References

- LIFE-Harness: arxiv 2605.22166 (Xu et al. 2026) — absorbed v1.2, removed v1.5
- tau-bench: arxiv 2406.12045 (Yao et al. 2024), github.com/sierra-research/tau-bench
- SWE-bench: github.com/princeton-nlp/SWE-bench
- AgentBench: github.com/THUDM/AgentBench (ICLR 2024)
- WebArena: github.com/web-arena-x/webarena
- OSWorld: github.com/xlang-ai/OSWorld
- Spider 2.0: arxiv 2411.07763, github.com/xlang-ai/Spider2
- Pi agent: github.com/earendil-works/pi
- Hermes Agent (Nous Research): github.com/NousResearch/hermes-agent
- Claude Code tools reference: code.claude.com/docs/en/tools-reference
- TriCEGAR (predicate-tree CEGAR for agent traces): arxiv (cited in v1.7.0)
- Ferns/Precup — Bisimulation Metrics are Optimal Value Functions, UAI 2014
- Step-Level Q-Value Models (Zhai et al., AAAI 2025): arxiv 2409.09345
- Graph Memory + RL: arxiv 2511.07800
- MACLA (hierarchical procedural memory): arxiv 2512.18950
