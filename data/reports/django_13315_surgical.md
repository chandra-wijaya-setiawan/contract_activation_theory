# django-13315 — surgical PASS vs FAIL trace comparison

**Task:** "limit_choices_to on a ForeignKey can render duplicate options in formfield."
**Correct fix (per upstream):** in `django/forms/models.py`, `apply_limit_choices_to_to_formfield()`, change
```python
formfield.queryset = formfield.queryset.complex_filter(limit_choices_to)
```
to
```python
formfield.queryset = formfield.queryset.complex_filter(limit_choices_to).distinct()
```
`.distinct()` must come **after** `complex_filter(limit_choices_to)` (the filter introduces the join that causes the duplicate).

R27 result: **3 PASS / 2 FAIL** (up from 0/5 in R26).

## Single-axis discriminator: which file the Stipulator targeted

| Trial | Result | File the agent ended up editing | Tokens | Iters | str_replace calls |
|---|---|---|---|---|---|
| 1 | ✅ PASS | `django/forms/models.py::apply_limit_choices_to_to_formfield` | 168 K | 11 | 1 |
| 3 | ✅ PASS | `django/forms/models.py::apply_limit_choices_to_to_formfield` | 114 K | 8  | 1 |
| 5 | ✅ PASS | `django/forms/models.py::apply_limit_choices_to_to_formfield` | 91 K  | 13 | 1 |
| 2 | ❌ FAIL | `django/db/models/fields/related.py::ForeignKey.formfield` (wrong file) | 155 K | 15 | 1 |
| 4 | ❌ FAIL | both files, **corrupted forms/models.py** | 205 K | 24 | 6 |

**The single signal that decides the outcome is the file location chosen in the plan.** Every PASS trial targeted `forms/models.py`; every FAIL trial started with `related.py` (the ForeignKey class).

## Trial-by-trial signature

### ✅ Trial 3 — the cleanest PASS (114 K, 8 iters)
- Stipulator's plan even started with the wrong file in A1 (`django/db/models/fields/related.py`) but step A2 broadened to "search for forms.py of related fields (model_forms.py)" — the search redirected the agent to the right file before C1.
- C1 mutate landed `complex_filter(limit_choices_to).distinct()` in one call with the exactly-correct old_str (44 chars).
- D1 verify ran the django test runner cleanly.

### ✅ Trial 5 — moderate friction PASS (91 K, 13 iters)
- C1 mutate took 5 iters to land (Performer churned rg/rg/read/read before str_replace).
- But the file was right, so once str_replace landed the fix was correct.
- This is the pattern R28 (UpdateStepOp + locate-before-mutate) targets — would have shaved ~3 iters.

### ❌ Trial 2 — the surgically-instructive FAIL (155 K, 15 iters)
- Plan A1: "Read the ForeignKey.formfield method in `django/db/models/fields/related.py`" — Stipulator anchored on the wrong file at plan time.
- All 5 investigate steps stayed in `related.py`. The plan never asked "is this the right entry point?"
- C1 mutate landed cleanly on first try, edited `ForeignKey.formfield()`:
  ```diff
  -    qs = qs.filter(limit_choices_to)
  +    qs = qs.filter(limit_choices_to).distinct()
  ```
- Looks reasonable, BUT: `ModelForm` builds form fields via `apply_limit_choices_to_to_formfield()` (in `forms/models.py`), **not** through `ForeignKey.formfield()`. So the .distinct() is added to a queryset that's never returned to the form.
- D1 verify ran the django test → harness reports `FAIL: test_limit_choices_to_no_duplicates` (the test that proves the duplication still happens via forms).
- Every signal in our system said the step is DONE. There's no way for the agent to know the test FAILED — verify-step output parsing is too shallow.

### ❌ Trial 4 — the catastrophic FAIL (205 K, 24 iters, **6 str_replace calls**)
- C1 mutate iterated for **15 iters**, making **6 str_replace attempts** across BOTH `related.py` AND `forms/models.py`, with no PlanPatcher revision firing.
- Final state: `forms/models.py` is **broken**. Test runner can't even import the test module:
  ```
  File "/testbed/django/forms/models.py", line 260, in __new__
      apply_limit_choices_to=False,
  ```
- The Performer kept retrying str_replace with different args. Each successful str_replace partly-edited the file. Net effect: contradictory edits left the function signature broken.
- D1 verify ran but the test never reached `test_limit_choices_to_no_duplicates` — the module crashed at import.
- This trial WAS the smoking gun for R28 (UpdateStepOp) AND for what I'll call R32 (rollback-on-corrupted-mutate): when a verify step shows ImportError/SyntaxError after multiple mutate attempts on one node, the PlanPatcher should revert via `git checkout` and re-plan.

## Why the failing trials looked confident

Every single failing trial reached `D1 verify step_done=True`. The attestor approved the verify step because:
1. The bash command exited 0 (it ran the test runner, even if the runner reported FAILED in its output).
2. No `[ERROR]` marker in the trace.
3. The verifier-outcome heuristic in OutcomeSchema parses "PASS"/"OK"/"FAIL" from pytest-style output, but the django test runner's `FAILED (failures=1)` format isn't always matched cleanly.

The same false-positive pattern from the R26 report (Pattern B) is still present — R27 didn't fix it.

## What R27 actually did for django-13315

R27 didn't change which file the Stipulator chose. It changed Performer behavior on deviation, which helped the *successful* trials land cleaner str_replace calls. But it didn't help the Stipulator pick the right file in the first place.

The +3 wins came from **2 of the 5 trials happening to broaden their A-phase search** (trials 3 and 5 included a "search model_forms" step), and **1 trial starting in the right file** (trial 1). The other 2 trials anchored on `related.py` and there was no mechanism to recover.

## Concrete improvements ranked by what they would have fixed

| R# | Fix | Trial 2 | Trial 4 | Why |
|---|---|---|---|---|
| **R29-FixVerify** | Parse `FAILED (failures=N)` from django test runner output as `verifier_outcome=FAIL`, force step !done, send to Resolver | ✓ would catch | ✓ would catch | turns the false-positive verify into a forced replan |
| **R31-MutateBudget** | If a single mutate node accumulates ≥3 successful str_replace calls AND verify still doesn't pass, auto-fire PlanPatcher with `BranchOp` to try a different file/approach | partly | ✓ critical | trial 4's 6 edits would have stopped after 3 |
| **R32-RollbackOnCorrupted** | If verify produces ImportError/SyntaxError after a mutate step, auto `git checkout` the file and re-plan | n/a | ✓ critical | trial 4's forms/models.py never recovers |
| **R28-LocateBeforeMutate** | For long old_str (>100c), prepend a locate substep that reads the file first; PlanPatcher auto-fills C1's old_str from the read | n/a | partly | trial 4's churn was partly Stipulator-hallucinated old_str |
| **R33-MultiCandidatePlan** | Stipulator emits N=2 candidate plans (different file targets); LLM-attestor picks one. (deferred PlanComposer) | ✓ critical | ✓ critical | the root cause: file selection at plan time |

## Headline

The +3 win for django-13315 is real but fragile — it came from **probabilistic file selection in the Stipulator**, not from the planner getting smarter. R29-FixVerify is the single highest-leverage fix: it would turn trials 2 and 4 from silent FAIL into "the agent realizes verify failed and replans," potentially converting them to PASS. R31/R32 prevent trial-4 corruption. R33 attacks the root cause but is a much bigger investment (the PlanComposer scaffold from R18, still in roadmap).
