# M7 — swe10 v2 (iteration_loop_redesign) vs v1.7 A.2 fixed

**Date:** 2026-05-31
**v2 run:** `data/runs/swebench/2026-05-31/01-47-06-v2-swe10/`
**baseline:** `data/runs/swebench/2026-05-30/13-37-39-swe10-v1-7-a2-fixed/`
**design spec:** [docs/iteration.md](iteration.md)
**execution plan:** [docs/itteration_loop_redesign_plan.md](itteration_loop_redesign_plan.md)

## Headline

| | v1.7 A.2 fixed | v2 (iteration_loop_redesign) | Δ |
|---|---|---|---|
| **Pass rate** | 8/10 = 80% | 8/10 = 80% | **+0** |
| **Tokens (total)** | 1,595,702 | 1,813,361 | +13.6% |
| **Tokens (out)** | 124,511 | 90,433 | -27.4% |

**Net: v2 matches baseline pass rate.** Total tokens are slightly higher
(+14%) but output tokens (the expensive direction) are lower (-27%).

## Per-task

| task | baseline | v2 | base tokens | v2 tokens | Δ tokens |
|---|---|---|---|---|---|
| django__django-10880 | ✓ | ✓ | 383,506 | 522,737 | +36% |
| django__django-11239 | ✗ | **✓ WIN** | 97,162 | 162,447 | +67% |
| django__django-12039 | ✓ | ✓ | 395,339 | 68,554 | **-83%** |
| django__django-13315 | ✗ | ✗ | 333,917 | 198,015 | -41% |
| matplotlib__matplotlib-24149 | ✓ | ✓ | 18,045 | 88,026 | +388% |
| pytest-dev__pytest-6202 | ✓ | ✓ | 32,995 | 244,631 | +641% |
| scikit-learn__scikit-learn-14141 | ✓ | ✓ | 29,271 | 45,345 | +55% |
| sphinx-doc__sphinx-10323 | ✓ | ✓ | 65,296 | 174,297 | +167% |
| sympy__sympy-15349 | ✓ | ✓ | 208,136 | 125,349 | -40% |
| sympy__sympy-15875 | ✓ | **✗ LOSS** | 32,035 | 183,960 | +474% |

**Swap detail**:
- **Won** `django__django-11239`: baseline failed, v2 passed. The task we
  hand-walked at the start of the session as the canonical example.
- **Lost** `sympy__sympy-15875`: baseline passed at 32K tokens, v2 spent 184K
  tokens and still failed. The in-loop re-stipulate added cost without
  finding the fix.

## v2-specific telemetry (per task)

| task | surprises | restipulates | commits | drops | refines | commit_new |
|---|---|---|---|---|---|---|
| django-10880 | 20 | 5 | 11 | 5 | 0 | 5 |
| django-11239 | 14 | 3 | 8 | 2 | 1 | 1 |
| django-12039 | 6 | 2 | 5 | 1 | 0 | 1 |
| django-13315 | 10 | 3 | 7 | 3 | 0 | 2 |
| matplotlib-24149 | 9 | 1 | 7 | 0 | 1 | 1 |
| pytest-6202 | 17 | 5 | 11 | 5 | 0 | 3 |
| scikit-learn-14141 | 6 | 0 | 5 | 0 | 0 | 0 |
| sphinx-10323 | 12 | 5 | 9 | 2 | 1 | 2 |
| sympy-15349 | 12 | 5 | 6 | 5 | 1 | 3 |
| sympy-15875 | 13 | 2 | 9 | 1 | 1 | 1 |
| **total** | **119** | **31** | **78** | **24** | **4** | **19** |

**Observations**:
- All op kinds were exercised except BRANCH and MERGE — no genuine
  alternative-approach exploration fired in this set.
- Re-stipulate cap (max 5/task) was hit on 5 of 10 tasks → there were more
  surprises than budget allowed; some surprises went unaddressed.
- scikit-learn-14141 (fastest task) had 6 surprises but 0 re-stipulates —
  surprises were resolved by step-appears-done short-circuit.

## Librarian persistence (cross-task transfer)

After the run, `data/librarian_v2/v2.db` contains:

- **11 plan_trees** stored (5 activator-FULFILLED + 6 activator-FAILED)
  - Activator-FULFILLED ≠ TB-resolved. TB independently verifies the patch
    against the test suite.
- **119 edges** across **81 distinct state_classes**
- **6 distinct obligation_classes** (= 6 buckets for cross-task transfer)
  - `b45822967827` (django.db.backends): 4 visits, all activator-FAILED
  - `2127aea7717d` (sphinx): 2 visits, both PASS
  - `e8cfe9a27e64` (django.db.models): 2 visits, both FAILED

## Honest assessment

### What worked
- **End-to-end v2 path runs cleanly** across all 10 tasks. No crashes after
  the defensive `expects` coerce landed.
- **5 of 7 ops exercised**: COMMIT, CHECKOUT, DROP, COMMIT_NEW, REFINE.
- **Cross-task librarian persists** between tasks. Edges + plan trees
  accumulate.
- **Per-intent surprise heuristic** prevents premature auto-commit on
  investigate steps with empty output.
- **Pass rate matches baseline.**

### What didn't work
- **Token cost +14% overall.** The re-stipulate calls are real LLM cost.
- **One regression (sympy-15875)** offsets the one win (django-11239).
- **No BRANCH/MERGE usage** — the in-loop Stipulator never emitted
  branch-style alternatives in this 10-task run.
- **`obligation_class` L1 granularity** turned out narrow enough that
  baseline subsystem-level transfer didn't collide. With ~6 buckets and
  ~2 visits each, the corpus is too sparse for transfer benefit yet.

### What this means
v2 design is **architecturally sound** but not yet **economically superior**
to baseline. To beat baseline on a future run, tune:
- (a) smaller in-loop Stipulate prompts (reduce per-restipulate cost)
- (b) higher surprise threshold (fire less on tasks the LLM is already on-track)
- (c) more data: library transfer hasn't paid off because corpus is too small

## Reference

- v2 trace dir: `data/runs/swebench/2026-05-31/01-47-06-v2-swe10/`
- v2 librarian DB: `data/librarian_v2/v2.db`
- comparison script: `scripts/compare_v2_vs_baseline.py`
- baseline: `data/runs/swebench/2026-05-30/13-37-39-swe10-v1-7-a2-fixed/`
