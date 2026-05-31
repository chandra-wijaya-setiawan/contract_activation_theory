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

(updated as I execute)

### M1 — plan_tree
- Status: pending

### M2 — Stipulator schema
- Status: pending

### M3 — canonicalization
- Status: pending

### M4 — surprise + swe1
- Status: pending

### M5 — in-loop Stipulator + swe3
- Status: pending

### M6 — librarian + swe10
- Status: pending

### M7 — comparison
- Status: pending

## Risk log

(things I notice that could derail the run; track + mitigate)

(none yet)
