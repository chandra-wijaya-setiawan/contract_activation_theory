# iteration_loop_redesign — execution plan

Living doc. Update during execution.

Start: 2026-05-31 (this session)
Design spec: [docs/iteration.md](iteration.md)
Goal: swe10 v2 run end-to-end + comparison vs v1.7 A.2 fixed (8/10).

## Baseline to beat (or at least match)

- **v1.7 A.2 fixed**: 8/10 (`data/runs/swebench/2026-05-30/13-37-39-swe10-v1-7-a2-fixed/`)
- Task set: django-13315, scikit-learn-14141, django-12039, django-10880,
  sphinx-10323, pytest-6202, matplotlib-24149, sympy-15349, django-11239,
  sympy-15875.

## Honest scope

**Realistic outcome**: v2 either runs and scores 5-9/10, OR has a bug that
prevents end-to-end run. Outperforming v1.7 is NOT promised on first run.
The design is novel; new failure modes are expected. Goal is **end-to-end
execution + data** so we can iterate.

If v2 < 8/10 on first run: that's acceptable as long as we have a diagnosis
path. If v2 doesn't run at all: that's a failure of this session.

## Milestones with hard gates

| M | Deliverable | Gate | Est LOC |
|---|---|---|---|
| M1 | `cat/plan_tree.py` + tree-edit ops + unit tests | `pytest tests/test_plan_tree.py` green | ~250 |
| M2 | Stipulator cold-start phased schema + tests | LLM emits valid JSON for 1 obligation | ~100 |
| M3 | `obligation_class` + `state_class` v2 + tests | hashes are deterministic, computed in <10ms | ~150 |
| M4 | Surprise vector + activator integration + swe1 | swe1 runs end-to-end, produces trace | ~200 |
| M5 | In-loop Stipulator (op diffs) + swe3 | swe3 runs end-to-end, ≥1 PASS | ~150 |
| M6 | Librarian writes + chronological swe10 | swe10 completes, results.json valid | ~150 |
| M7 | Comparison report vs v1.7 A.2 fixed | Markdown table + token deltas | — |

Total est ~1000 LOC. Aggressive.

## Out of scope (defer)

- Full `predict_det` forward model (needs n≥2 data; cold start anyway)
- Anti-template integration in Stipulator cold-start prompt (works without)
- Embedding-based obligation_class fallback
- Test cleanup / removing old A.x code paths
- DAG visualizer updates for new event kinds

## Strategy notes

- **Backward compat**: activator gains a `use_plan_tree=True` flag. Old
  path still works (for v1.7 tests). New path is the v2 design.
- **Failure modes**: if Stipulator emits malformed JSON, fall back to v1
  flat plan. If in-loop ops are malformed, no-op (skip the tree-edit).
- **Token budget**: cap re-stipulate to 5 calls per task. Beyond that,
  give up replanning and stay on the original tree.
- **Commits**: one commit per milestone. Each commit must pass tests.

## Progress log

### M1 — plan_tree ✓
- 16/16 unit tests green
- `cat/plan_tree.py`: PlanTree, PlanNode, 7 ops (COMMIT, CHECKOUT, BRANCH, MERGE, DROP, COMMIT_NEW, REFINE), serialization
- Committed.

### M2 — Stipulator phased schema ✓
- 12/12 unit tests green
- `cat/llm.py`: `call_stipulator_phased`, `call_stipulator_inloop`, validators
- Cold-start schema (§6.1) and in-loop op schema (§6.2) both validated
- Committed.

### M3 — canonicalization ✓
- 12/12 unit tests green
- `cat/obligation_class.py`: L1 categorical hash (framework + subsystem-top-2)
- `cat/state_class_v2.py`: 9-component bucketed hash with RunCounts
- Committed.

### M4 — surprise + swe1 ✓
- 12/12 surprise unit tests green
- `cat/surprise.py`: 6-dim surprise vector + any_surprised
- `cat/activator_v2.py`: subclass of Activator with new run() flow
- `cat/tb_agents.py`: CAT_V2=1 toggle
- swe1 (django-11239) ran end-to-end → 0/1 (gate: end-to-end execution met)
- Fix landed: `_step_appears_done` per-intent heuristic + stall-counter
- Committed.

### M5 — in-loop Stipulator + swe3 ✓
- swe3 ran: **1/3 PASS** (sphinx-10323 ✓; django-10880, django-11239 ✗)
- Tokens used: 211K vs 545K baseline equivalent → ~60% reduction
- Telemetry across the run: 23 surprises, 6 restipulates, 16 commits, 2 drops, 1 refine, 2 commit_news
- Found + fixed `'str object has no attribute get'` bug: malformed `expects`
  field from in-loop LLM was passed through. Defensive isinstance() coerce
  in plan_tree.py for from_phased_plan, COMMIT_NEW, REFINE, BRANCH.

### M6 — librarian writes + chronological swe10 ✓
- `cat/librarian_v2.py` 5/5 unit tests green
- Persistent SQLite at `data/librarian_v2/v2.db`
- Plan-tree write + edge write per iter wired
- swe10 ran chronologically, concurrency=3, persistent librarian
- Final librarian state: 11 plan_trees, 119 edges, 6 obligation_classes
- **swe10 result: 8/10 (matches baseline)**

### M7 — comparison ✓
- `scripts/compare_v2_vs_baseline.py` produces `docs/M7_swe10_v2_comparison.md`
- **Pass rate: 8/10 v2 == 8/10 baseline (+0)**
- Token total: 1.81M v2 vs 1.60M baseline (+14%)
- Task swap: WON django-11239 (baseline failed); LOST sympy-15875
- Honest assessment + next-iteration tuning ideas in M7 doc

## Risk log

- **Plan length**: Stipulator may emit short plans (<5 steps) for complex tasks.
  Mitigation: prompt strengthened with explicit step-count guidance.
- **Step auto-commit**: investigate steps with empty tool output were auto-
  committing. Fixed via `_step_appears_done` + stall counter.
- **In-loop re-stipulate cost**: capped at 5 per task by max_restip_per_task.
- **First swe1 result**: 0/1 on django-11239. Same task often fails in
  baseline. Need swe3+ to evaluate properly.
