# CAT Roadmap

Forward-looking ideas. Items here are *not* committed work — they're a
parking lot for design ideas we want to come back to.

Each entry should include: motivation, rough shape, open questions.

---

## PlanComposer — parametric plan composition

**Status**: scaffold landed in `cat/plan_composer.py` (R18). Not yet wired
into the activator's run-loop. Personas (conservative / minimal /
exploratory) + heuristic scorer in place. Single-shot stipulator is still
the default.

**Future extension parameters** (not implemented yet — design only):

### `effort` — single bundled trade-off knob

Instead of orthogonal `optimize_for` + `intelligence_level` + `K` (which creates
false dilemmas and bad combinations), expose ONE dial:

```python
PlanComposer(llm, effort="medium")  # default
```

| effort | K | model tier | scorer | ~cost | use case |
|---|---|---|---|---|---|
| `minimal` | 1 | flash | heuristic | 1× | CI smoke tests, dev iteration |
| `low` | 1 | flash | heuristic | 1× | inner-loop iteration |
| `medium` | 3 | flash | heuristic | ~1.05× | normal swe-bench runs (R18 default) |
| `high` | 3 | mid model on hard personas | heuristic + LLM tiebreak | 3-5× | headline runs |
| `max` | 5 | deep model | learned + LLM | 10-15× | leaderboard submission |

Each level pre-bundles the underlying knobs in a coherent direction. Within
a level you never get "cheap model + expensive scoring" or other incoherent
combinations.

Power users can still override individual params (`personas=`, `K=`, `scorer=`)
if they have a specific need.

Same pattern as Claude Code's effort levels, Cursor's model tiers, OpenAI's
reasoning_effort — proven user model.

Motivation: different deployment targets (dev iteration vs leaderboard) want
different cost/quality trade-offs, but they're correlated, not orthogonal.
Higher quality fundamentally costs more (more samples, deeper models). A
single dial reflects that physics.

### Learned scorer (`LearnedPlanScorer`)
`HeuristicPlanScorer` is hand-coded. As the LibrarianV2 accumulates plan-
tree → outcome pairs, we can train a scorer that predicts pass-probability
from plan shape. Replaces the heuristic with a learned model that gets
better with corpus size.

Motivation: heuristic scoring captures intuition but not learned patterns
specific to obligation_class. A django task and a sympy task have
different "good plan" shapes; a learned scorer would pick that up.

Open question: training signal. Per-obligation_class binomial outcomes
(passed/failed) × plan features → logistic regression or small MLP. Need
~1000+ samples to train reliably.

### LLM-judge scorer (`LLMJudgeScorer`)
Alternative to learned: a separate LLM call ranks K candidate plans.
Higher quality than heuristic but costs an extra LLM call per stipulate.

Use case: when heuristic scoring shows ties or near-ties, fall through
to LLM judge. Pure-LLM judge would dominate cost; hybrid (heuristic
first, LLM tiebreak) is the right shape.

---

## Resolver: true `restipulate` decision

**Status**: parked. R15a renamed the existing `restipulate` decision to
`patch_plan` because it actually invoked the PlanPatcher (local edits),
not the full Stipulator. The name `restipulate` is now FREE in the
decision enum and reserved for a future outer-loop full-reset path.

**Shape**: when the Resolver decides `restipulate`, the activator calls
`call_stipulator_phased` again with the original obligation + a
diagnosis of what went wrong + the prior plan tree as anti-template.
The new plan replaces the old; attention resets to A1 of the new tree;
run.messages persist (so the executor still sees prior tool outputs);
run_counts reset.

**Cap**: `max_restipulates_per_task` (separate from `max_patch_plans_per_task`).

Motivation: PlanPatcher only handles local fixes. When the WHOLE plan
strategy is wrong (wrong file, wrong approach), local edits accumulate
without reaching the fix. A true restipulate gives the planner a clean
slate informed by what failed.

Open questions:
- How much of run.messages to surface to the new stipulator? (all? last K?)
- Should the new plan inherit any "already done" status from prior tree?
  (Probably no — fresh plan, fresh expects, the messages history is the
  only continuity.)

---

## Dijkstra-style plan-space search

**Status**: idea phase.

**Shape**: real path-finding through plan space, not just K-sample of linear
plans. Requires:
- Stipulator emits a DAG with BRANCH points at uncertain decisions
- Each edge has a cost estimate (expected tokens × failure probability)
- Activator picks the cheapest path through the DAG
- Re-evaluates the path at each iteration given actual progress (A*-style)

Motivation: the K-sample PlanComposer addresses *plan-shape variance* but
not *path-choice variance*. Dijkstra would address both. Higher reward,
much higher complexity.

Open questions:
- Where do edge costs come from? Hand-tuned vs learned from librarian
  corpus.
- How does the LLM emit a DAG cleanly? (Multi-phase plan with explicit
  alternative children?)
- Computational cost of repeated shortest-path computation per iter.

---

## Per-task variance map (deterministic-fail vs flaky)

**Status**: scaffold idea. Currently we run swe10 × 5 trials manually to
observe per-task variance. Could be automated.

**Shape**: a benchmark mode that runs each task K times and produces a
report:
- Tasks that are 100% PASS (deterministic-pass)
- Tasks that are 100% FAIL (deterministic-fail — capability limit)
- Tasks that are 20-80% PASS (flaky — variance source)

Triage: deterministic-fail tasks need framework or model improvement.
Flaky tasks need variance-reduction (PlanComposer, better Resolver, etc.).

---

## v1 legacy cleanup

**Status**: archived to `cat/_legacy/` after R15a.

`cat/_legacy/activator.py` (the base v1 Activator class) is still imported
by the new `cat/activator.py` to reuse `_admit`, `_perform_once`, etc.
We should fold those methods into the active Activator and delete the
legacy file. Wait until the active code path is stable for another
~10 commits before doing this — too easy to regress while iterating.

Same for `cat/_legacy/state_class.py` + `cat/_legacy/corpus.py` +
`cat/_legacy/librarian.py` — they support the v1 election/recommendation
system which the active code doesn't use. Keep until we've confirmed
no path depends on them.
