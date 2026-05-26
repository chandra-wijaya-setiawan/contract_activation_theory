# Project Plan: CAT v1.0 – v2.0

Status as of 2026-05-26 (updated). See DESIGN.md for theory and architecture.

## Completed

### v1.0 — Synthetic proof of mechanism (DONE, 2026-05-14)

- Core runtime: Contract, Criterion, Activator, Capability, State, Recorder.
- CatAgent (contract-disciplined) + ReactAgent (vanilla ReAct) sharing same
  LLM + tool stack.
- 2 synthetic tasks (hello-file 1-step, multi-step-refactor 5-step), 12 trials.
- Result: CAT 6x fewer tool calls, 10x fewer tokens on 5-step task.
- Criterion: deterministic Python predicate (file existence + content match).

### v1.1 — terminal-bench, real Docker tasks (DONE, 2026-05-20)

- Tasks: `hello-world`, `fix-pandas-version`, `csv-to-parquet` (Docker
  containers via `ssh://cpubox`).
- Result: **ReAct 88.9% vs CAT 55.6%.** CAT costs 4x more ($0.012 vs
  $0.003/trial).
- Root causes:
  1. LLM judge called 3x per tool call (pre-activation + post-activation +
     checklist) → 4 LLM calls per tool call vs ReAct's 1.
  2. LLM judge fires on partial progress → premature contract closure.
- Criterion: LLM judge on terminal pane output — wrong choice.
- Lesson: deterministic criterion is mandatory for fair cost comparison.

### v1.2 — Expand synthetic coverage + LIFE-Harness layers (DONE, 2026-05-26)

- 3 new synthetic tasks: bug-fix (~8 steps), multi-file-rename (~10 steps),
  test-generation (~12 steps).
- Composable oracle predicates: file_exists, file_contains, file_not_contains,
  bash_succeeds, ast_has_function, all_of, any_of, oracle_from_predicates.
- LIFE-Harness layers absorbed (arxiv 2605.22166):
  - Action Realizer (activator.py): normalize args before invoke.
  - Environment Spec (capabilities.py): preconditions on tools.
  - Circuit Breaker (activator.py): trajectory regulation with replan.
  - Failure Attribution (trace.py): layer-attributed failure events.
- Result: CAT 3.5x cheaper on hardest task (test-generation), lower variance.
  All tasks 100% solve rate for both agents.

### v1.3 — terminal-bench deterministic criterion rerun (DONE, 2026-05-26)

Goal: Rerun v1.1's real Docker tasks with the deterministic test-runner
criterion.

**Changes:**
- `activator.py`: removed pre-activation `_verify_criteria` call → judge
  called once per tool call, not twice.
- `tb_agents.py`: `_make_test_runner_criterion(session, test_cmd)` — runs
  `<test_cmd>; echo EXIT:$?`, parses exit code. Zero false-positive rate.

**Results:**
- cat-p4 (3 tasks, 3 attempts): **CAT 100%** — regression fully recovered.
- cat-p6 vs react-p6 (5 tasks, 2 attempts): **CAT 90% = ReAct 90%** — parity.
- Root cause confirmed: deterministic criterion fixes premature closure.

**Lesson:** LLM judge is only safe called once at task end, not per tool call.

---

### v1.4 — SWE-bench verified (IN PROGRESS, 2026-05-26)

Goal: Validate CAT on real GitHub issues with deterministic pytest criterion.

**Oracle:** `benchmarks/swe_bench.py:TbSweAgent` — applies SWE-bench's own
`test_patch` + runs `FAIL_TO_PASS` tests via exit code. Proxy oracles for
tasks whose tests require live network (httpbin etc.).

**Results so far:**
- swe10 head-to-head (10 tasks, 1 attempt):
  - **CAT (TbSweAgent): 3/10 = 30%** — 199K input tokens total
  - **ReAct: 3/10 = 30%** — 1,006K input tokens total
  - **CAT uses 5× fewer input tokens at equal solve rate**
- swe30-cat (30 tasks, 1 attempt): **CAT 9/30 = 30%**
- swe30-react (11/30 tasks complete): 7/11 = 64% (run in progress → swe30-react-2)

**Next:** Complete swe30-react-2 (remaining 19 tasks) for full 30-task comparison.
Add pass^k metric (3 reps per task) after solve rate is understood.

## v1.4 — tau-bench (planned)

Goal: Validate CAT on customer-service conversations (non-coding domain).

### tau-bench integration

### Track B: tau-bench (customer service, non-coding)

**What:** Simulated customer service conversations. LLM plays the customer,
agent uses domain API tools (cancel_order, change_flight, etc.). Oracle
compares final database state against ground truth.

**Engineering work:**

1. **tau-bench integration** (`cat/bench/taubench.py`)
   - Clone tau-bench repo (or tau3-bench for latest).
   - Load task definitions: each has a user goal, initial DB state, expected
     final DB state, available API tools, and policy rules.

2. **Capability adapter**
   - Wrap each tau-bench API tool as a CAT Capability.
   - Example: `cancel_order(order_id)` becomes a Capability with JSON schema,
     handler calls the tau-bench environment's API.

3. **Criterion adapter**
   - Map tau-bench goal-state annotations to CAT Criterion predicates.
   - `(State) -> bool` where State includes the DB snapshot.
   - Compare actual DB state against expected: row-by-row equality check.

4. **User simulator integration**
   - tau-bench provides an LLM-based user that plays the customer role.
   - Need to wire this into the conversation loop: executor talks to user
     simulator AND uses tools. This is a new interaction pattern for CAT —
     currently the executor only talks to tools.
   - Option A: user simulator messages injected as tool results.
   - Option B: extend the Activator to support multi-party conversations.

5. **Policy constraints**
   - tau-bench tasks have policy rules (e.g., "refund only if within 30 days").
   - Map these to CAT Constraints or Conditions on the Contract.

6. **Run configuration**
   - Retail + airline domains, >=30 instances each.
   - 4 reps per instance for pass^k.
   - Total: 60 instances x 2 agents x 4 reps = 480 trials.

**Risks:**
- User simulator requires a second LLM call per conversation turn -> doubles
  token cost and adds latency.
- tau-bench policy rules are natural language, not predicates — need to decide
  whether to encode them as CAT Conditions or leave them in the system prompt.
- DB state comparison may need fuzzy matching for some fields.

### Cross-cutting work

1. **pass^k metric** (`cat/compare.py`)
   - Group trials by instance_id, check if all k reps passed.
   - Report pass^1, pass^2, pass^4 in the summary table.
   - This is the key consistency metric — high pass^1 but low pass^4 means
     the agent is unreliable.

2. **Pi baseline** (`cat/bench/pi_adapter.py`)
   - Run same instances through Pi (stateless ReAct baseline).
   - Either vendor Pi's runner or call it as subprocess.
   - Report head-to-head: CAT vs ReAct vs Pi.

3. **Benchmark runner** (`cat/bench/runner.py`)
   - Extend runner.py to handle bench="swebench" and bench="taubench"
     in addition to bench="synthetic".
   - Unified CLI: `python -m cat.bench --suite swebench-lite --reps 4`

**Deliverables:**
- SWE-bench Lite results (>=30 instances, pass^1 and pass^4)
- tau-bench results (>=60 instances across 2 domains, pass^1 and pass^4)
- Pi baseline comparison on both tracks
- Summary report with CAT vs ReAct vs Pi head-to-head

---

## v1.6 — Failure handling and re-planning

Goal: Make CAT robust to mid-run failures instead of failing the whole
contract on first unrecoverable error.

**Engineering work:**

1. **Failure propagation** (`cat/activator.py`)
   - When a child contract FAILs, parent gets one chance to re-plan.
   - Parent's Activator detects child FAILED -> decomposes again with failure
     context ("step X failed because Y, find alternative").
   - If re-plan also fails -> parent FAILs with full trace.
   - Limit: exactly one re-plan per parent. No infinite retry loops.

2. **SUSPENDED -> re-ELIGIBLE flow** (`cat/contracts.py`)
   - Contracts blocked on external state (test run in progress, API rate
     limit) transition to SUSPENDED.
   - When the blocking condition clears, contract becomes ELIGIBLE again.
   - Activator polls or receives notification.

3. **Recovery stress tests** (new synthetic tasks)
   - Task where the first approach fails (wrong file, missing dependency)
     and the agent must recover.
   - Task with a flaky test that passes on retry.
   - Oracle checks that recovery happened AND final state is correct.

4. **Failure attribution analysis**
   - Use the failure_attribution events from v1.2 to classify failures
     from v1.4 benchmark runs.
   - Report: what % of failures are action_realization vs
     environment_contract vs trajectory_regulation vs residual_reasoning.
   - This data feeds into v1.8's procedural skills design.

**Deliverables:**
- Re-plan on child failure (one retry)
- SUSPENDED state flow working end-to-end
- Recovery stress test tasks + benchmark results
- Failure attribution breakdown from v1.4 runs

---

## v1.8 — Coding domain hardening (HARD GATE)

Goal: Prove CAT matches or beats an established coding agent on a recognized
benchmark. This is the gate to v2.0 — must pass before expanding to other
domains.

**Engineering work:**

1. **Head-to-head vs established agent**
   - Pick one: Pi, Hermes, or Claude Code.
   - Run on shared benchmark (SWE-bench Lite from v1.4).
   - Target: >= parity on solve rate, measurable advantage on token cost.

2. **LIFE-Harness comparison** (arxiv 2605.22166)
   - Run CAT and LIFE-Harness on shared tau-bench / tau2-bench tasks.
   - Direct test: top-down design (CAT) vs bottom-up learned interventions.
   - Use same model backbone for fair comparison.

3. **AgentBench subset** (THUDM, ICLR 2024)
   - Add OS environment (Linux shell tasks, 144 instances).
   - Add DBBench (SQL tasks, 261 instances).
   - Optional: ALFWorld (household tasks, 134 instances).
   - All deterministic, published LIFE-Harness numbers available.
   - Criterion adapters: file/exit-code for OS, query-result for DB.

4. **Event sourcing** (`cat/trace.py`)
   - Every state transition emits a replayable event (already partially done).
   - Build replay tool: given a trace, reconstruct the full execution.
   - Post-hoc analysis: where exactly does ReAct drift vs where CAT stays.

5. **Procedural skills (first cut)** (`cat/skills.py`)
   - Mine successful trajectories from v1.4-v1.6 runs.
   - Extract common decomposition patterns (e.g., "read -> diagnose -> edit ->
     test -> verify" for bug-fix tasks).
   - Planner queries skill library before LLM call.
   - Cross-run memory: long-term store of what plans worked.

6. **pass^k across all environments**
   - Report pass^1, pass^2, pass^4 for SWE-bench, tau-bench, AgentBench.
   - Consistency is the key differentiator for CAT.

**Gate criteria:**
- CAT >= parity on solve rate vs at least one established agent.
- CAT measurably cheaper (tokens or USD) on same benchmark.
- Results reproducible (pass^4 > 50% on solved instances).
- Results published (internal report or external paper).

**If gate fails:** Iterate on v1.6-v1.8 (improve failure handling, add skills,
tune circuit breaker) until gate criteria are met. Do NOT proceed to v2.0.

---

## v1.9 — GAIA (near-verifiable open-ended tasks)

Goal: Bridge between deterministic and subjective criteria before tackling
fully open-ended benchmarks.

**Why GAIA:** Multi-step research questions with exact-match or near-match
answers (Wikipedia-grounded). Three difficulty levels. Tests whether CAT's
plan-first discipline reduces unnecessary web lookups on hard questions.

**Criterion design:**
- Exact-string + normalized-string match as deterministic criterion.
- Fall back to LLM judge only for Level 3 (hardest) questions.
- Judge called **once at task end**, not per tool call.

---

## v2.0 — Domain-agnostic expansion (gated on v1.8)

Goal: Prove CAT's discipline advantage transfers across domains, not just
coding.

**Prestige benchmarks:**

| Benchmark | Domain | Tasks | Oracle type |
|---|---|---|---|
| WebArena | Web browsing (Reddit, GitLab, shopping) | 812 | Page state validators |
| OSWorld | Desktop control (files, apps, browser) | 369 | System state validators |
| Spider 2.0 | Enterprise SQL (Snowflake, BigQuery) | 632 | Query result comparison |

**Criterion adapter taxonomy:**

| State type | Adapter | Used by |
|---|---|---|
| File system | `file_content(path) == expected` | OS, SWE-bench, OSWorld |
| Database rows | `db_state == gold_state` | tau-bench, DBBench, Spider 2.0 |
| Web/DOM | `page_element(selector) == expected` | WebArena, WebShop |
| Object location | `object_at(room, surface)` | ALFWorld |

**Engineering work:**

1. **Adapter framework** — generic base class for connecting external
   benchmarks to CAT (task loader, capability wrapper, criterion mapper,
   grader adapter). Should be pluggable so adding a new benchmark is mostly
   config + one adapter file.

2. **Per-benchmark adapters** — one module per benchmark under `cat/bench/`.

3. **WebArena criterion design** — run WebArena's built-in evaluator
   (programmatic where possible) + LLM judge on final page state only for
   ambiguous cases. Judge called **once at task end**, not per tool call.
   Human eval on a sample of 50 trials as a publication-level sanity check.

4. **Cross-domain analysis** — do failure attribution patterns differ by
   domain? Does the circuit breaker fire more in some domains? Does the
   skill library transfer across domains?

5. **Publication** — write up the full v1.0-v2.0 story: theory, synthetic
   proof, real benchmarks, cross-domain validation.

---

## v2.x — Open design questions (deferred)

- **Speculative activation** — pre-activate capabilities while ELIGIBLE to
  reduce latency.
- **Contract markets** — auction protocol when multiple agents bid on a
  contract.
- **Persistence and distribution** — contracts that survive process restarts,
  cross-agent handoff.
- **Multi-agent** — multiple CAT agents collaborating on decomposed contracts.

---

## Metrics (all versions)

All runs use `cat.metrics.RunMetrics` built from JSONL traces. Key tracked
signals (aligned with OTel GenAI semantic conventions):

| Signal | OTel field | Why it matters |
|---|---|---|
| `tokens_in/out` | `gen_ai.usage.input_tokens` | Primary cost driver |
| `tokens_cache_read` | `gen_ai.usage.cache_read.*` | Prompt caching savings |
| `llm_calls_by_role` | `gen_ai.operation` (extended) | Planner/executor/judge overhead split |
| `tool_calls_by_name` | `gen_ai.tool.name` | Which tools dominate |
| `tool_errors` | — | Reliability signal |
| `wall_s_llm / wall_s_tools` | `gen_ai.client.operation.duration` | Where time is spent |
| `max_prompt_tokens` | — | Context window pressure |
| `judge_overhead_fraction` | CAT-specific | Judge as % of total tokens |
| `criterion_checks` | CAT-specific | How often criterion evaluated |
| `plan_completion_rate` | CAT-specific | Did executor follow the plan? |
| `budget_*_used_pct` | CAT-specific | Budget utilization per constraint |

---

## References

- LIFE-Harness: arxiv 2605.22166 (Xu et al. 2026)
- tau-bench: arxiv 2406.12045 (Yao et al. 2024), github.com/sierra-research/tau-bench
- SWE-bench: github.com/princeton-nlp/SWE-bench
- AgentBench: github.com/THUDM/AgentBench (ICLR 2024)
- WebArena: github.com/web-arena-x/webarena
- OSWorld: github.com/xlang-ai/OSWorld
- Spider 2.0: arxiv 2411.07763, github.com/xlang-ai/Spider2
