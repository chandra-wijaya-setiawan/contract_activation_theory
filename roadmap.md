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

### `optimize_for` — cost vs quality vs speed
```python
PlanComposer(llm, optimize_for="cost")     # prefer minimal persona, K=1
PlanComposer(llm, optimize_for="quality")  # K=3, conservative + exploratory
PlanComposer(llm, optimize_for="speed")    # K=1, minimal persona
PlanComposer(llm, optimize_for="balanced") # current default (K=3, persona-weighted)
```
Each axis dials persona selection, K, and scorer weights.

Motivation: different deployment targets (CI gating vs autonomous research vs
hobbyist) want different cost/quality trade-offs at the same code surface.

### `intelligence_level` — model tier per persona
```python
PlanComposer(llm, intelligence_level="fast")  # all personas → flash model
PlanComposer(llm, intelligence_level="mid")   # current default
PlanComposer(llm, intelligence_level="deep")  # exploratory persona → claude-sonnet
```

Motivation: the exploratory persona benefits most from a stronger model
(branch reasoning is hard for cheap models). The minimal persona barely
needs intelligence at all. Mixing model tiers per persona is more cost-
efficient than picking one model for everything.

Open question: how to plumb model selection cleanly. Probably a
`PersonaSpec.model: Optional[str]` field that overrides the default LLM.

### `K` — number of candidates
Today K is implicit (3 personas → 3 candidates). Future: explicit K and
auto-selected persona subset.

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
