# CAT Experiment Roadmap

## Phase 1 — Synthetic benchmark (complete)

**Tasks:** `hello-file`, `multi-step-refactor` (local subprocess, synthetic oracle)
**Result:** CAT uses 0.16× tool calls and 0.10× input tokens vs ReAct on a 5-step task, same solve rate. Discipline thesis supported.
**Criterion:** Deterministic Python predicate (file existence + content match).

---

## Phase 2 — terminal-bench, real Docker tasks (complete)

**Tasks:** `hello-world`, `fix-pandas-version`, `csv-to-parquet` (Docker containers via `ssh://cpubox`)
**Result:** ReAct 88.9% vs CAT 55.6%. CAT costs 4× more ($0.012 vs $0.003/trial). Two root causes identified:
1. LLM judge called 3× per tool call (pre-activation + post-activation + checklist) → 4 LLM calls per tool call vs ReAct's 1.
2. LLM judge fires on partial progress → premature contract closure.
**Criterion:** LLM judge on terminal pane output — wrong choice.

---

## Phase 3 — terminal-bench, deterministic test-runner criterion (next)

**Goal:** Rerun Phase 2 with the architectural fixes from Phase 2's root cause analysis.

**Changes:**
- `activator.py`: removed pre-activation `_verify_criteria` call → judge called once per tool call, not twice.
- `tb_agents.py`: `_make_test_runner_criterion(session, test_cmd)` — runs `<test_cmd>; echo EXIT:$?`, parses exit code. Zero false-positive rate.

**Run command:**
```bash
DOCKER_HOST=ssh://cpubox tb runs create \
  --dataset-path tasks \
  --task-id hello-world --task-id fix-pandas-version --task-id csv-to-parquet \
  --agent-import-path cat.tb_agents:TbCatAgent \
  --agent-kwarg model_name=openrouter/deepseek/deepseek-v4-flash \
  --agent-kwarg test_cmd="python -m pytest tests/ -q" \
  --n-attempts 3 --output-path data/tb-runs/cat-p3 --run-id cat-phase3
```

**Expected:** LLM calls/tool drops from 4× to ~1.3× (executor + 1 checklist judge/step). Solve rate recovers to match or exceed ReAct.

**Criterion:** `exit_code == 0` from test runner — deterministic.

---

## Phase 4 — SWE-bench (planned)

**Goal:** Stress-test CAT discipline on hard, realistic software engineering tasks.

**Why SWE-bench:** Long tasks (>10 steps), real GitHub issues, test suite oracle — exactly the scenario where ReAct loop drift is most costly. Deterministic criterion available (pytest exit code), so Phase 3's criterion design transfers directly.

**What to measure beyond Phase 3:** Patch quality (diff size, files touched), loop depth (how many iterations before criterion fires), cost vs solve rate curve across difficulty tiers.

**Criterion:** `pytest` exit code from the repo's existing test suite.

---

## Phase 5 — GAIA (planned)

**Goal:** Near-verifiable open-ended tasks — bridge between deterministic and subjective criteria.

**Why GAIA:** Multi-step research questions with exact-match or near-match answers (Wikipedia-grounded). Three difficulty levels. Tests whether CAT's plan-first discipline reduces unnecessary web lookups on hard questions.

**Criterion challenge:** Answers require fuzzy matching (dates, names, numbers). Use exact-string + normalized-string match as deterministic criterion; fall back to LLM judge only for Level 3.

---

## Phase 6 — WebArena / open-ended (planned)

**Goal:** First genuinely open-ended criterion — agent browses the web and completes natural-language instructions.

**Why deferred:** WebArena success criteria are partially verifiable (did the cart update? did the form submit?) but edge cases require judgement. Before attempting this, the criterion architecture needs to be proven reliable on fully deterministic tasks (Phases 3–5).

**Criterion design:** Run WebArena's built-in evaluator (programmatic where possible) + LLM judge on final page state only for ambiguous cases. Judge called **once at task end**, not per tool call.

**Human eval:** Entry point for this phase only — as a publication-level sanity check on a sample of 50 trials, not as the iterative signal.

---

## Metrics (all phases)

All phases use `cat.metrics.RunMetrics` built from JSONL traces. Key tracked signals (aligned with OTel GenAI semantic conventions):

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
