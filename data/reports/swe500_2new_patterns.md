# swe500 — 2 new failure patterns from 2 unseen-family trials

Picked from r27-swe500 partial (161/500, 36% pass) — task families we never tuned for.

## Trial 1 — `psf__requests-1724` — **Pattern F: harness false-negative (fix is correct, infra unrelated)**

**Issue:** Unicode method names cause UnicodeDecodeError in Python 2.7. Fix: convert to bytes/str.

**Agent run:** 5 iters, 42K tokens, single str_replace.

**Plan + execution (clean):**
```
A1 investigate bash    Read sessions.py to find req.method line     ✓
A2 investigate read    Read surrounding context                      ✓
B1 mutate     str_replace  Change method.upper() → str(method).upper() ✓
C1 verify     bash    Run oracle test command                       ✓
```

**The fix the agent produced:**
```python
-            method = method.upper(),
+            method = str(method).upper(),
```
This is the **correct conceptual fix** — explicit str() coercion preventing the unicode propagation. The agent's reasoning was sound: ID the line, read context, make the minimal change.

**Why the harness reported FAIL:** the test suite hit network failures (`assert 503 == 200`) on dozens of UNRELATED tests (`test_BASICAUTH_TUPLE_HTTP_200_OK_GET`, `test_HTTP_200_OK_PUT`, etc.). These tests depend on httpbin.org being reachable from inside the docker container. Many of those tests failed because the network was unreachable, and SWE-bench's pass criterion requires the target test PLUS no test regressions — so 30+ unrelated infra failures count as regressions.

**This is a NEW pattern.** The agent did the right thing; the bench's pass criterion penalizes a correct fix because tests in the same file need network.

→ **Not a CAT problem.** Filter / re-run after network-isolated, or downgrade these tasks in the pass-rate calculation. No R-number; this is benchmark hygiene.

---

## Trial 2 — `astropy__astropy-13579` — **Pattern G: deep-semantic bug that genuinely exceeds the model's capacity**

**Issue:** `SlicedLowLevelWCS.world_to_pixel` returns inconsistent values for coupled world dimensions. Real fix requires reasoning about WCS coordinate transformations.

**Agent run:** 27 iters, **312K tokens**, **5 str_replace calls**, **4 PlanPatcher firings**, **3 plan drops** + 3 new commits + 1 checkout.

**This trial shows our system actually working as designed:**

| Mechanism | Fired |
|---|---|
| Plan patches | 4 |
| Plan drops (failed nodes abandoned) | 3 |
| New nodes created mid-flight | 3 |
| Plan checkouts (branch switches) | 1 |
| Str_replace calls (refinements) | 5 |
| Verify attempts | 4 |

So PlanPatcher fired actively, the Performer kept refining, every verify-step bash ran the real test (`pytest test_sliced_wcs.py::test_coupled_world_slicing`). The system did everything it was supposed to do.

**Why it failed anyway:** the bug is **hard** — it requires understanding:
1. WCS coordinate coupling (a math/physics concept)
2. How `crpix` (reference pixel) maps to world coordinates
3. That for sliced-out dimensions, the right value is the world-coord of the reference pixel, not `1.` or `nan`

The agent landed `world_arrays_new.append(np.nan)` as the first guess, then progressively refined to a longer block with comments about CRPIX, but never got the math right. The test:
```
FAILED astropy/wcs/wcsapi/wrappers/tests/test_sliced_wcs.py::test_coupled_world_slicing
FAILED astropy/wcs/wcsapi/wrappers/tests/test_sliced_wcs.py::test_spectral_slice
```
kept reporting back FAILED, and the agent kept iterating.

**This is a NEW pattern.** The CAT loop is doing its job — multiple replans, real test feedback, real iteration. But deepseek-v4-flash isn't *strong* enough to reason through the underlying physics. This is a **capacity ceiling**, not a framework bug.

→ Either accept that this class of bugs is out of reach for v4-flash, or test the same task with a stronger model (Opus / Sonnet 4.6) to see if the framework + a better LLM lands.

---

## Comparison to known patterns

| Pattern | Cause | Fix-ability |
|---|---|---|
| A — Stipulator hallucinates old_str | Planning-time guess at exact code | R28 LocateBeforeMutate |
| B — Verify false-positive (`FAILED` ignored) | Attestor doesn't parse test output | R29 FixVerify |
| C — Wrong-file fix (django-13315) | Stipulator anchors wrong file | R33 MultiCandidatePlan |
| D — Tool-swap noise | Performer hedging | benign; observable via `reason` field |
| E — Mid-run crash | TB timeout / uncaught | infra |
| **F — Harness false-negative (NEW)** | Test suite has unrelated infra-dependent tests | bench hygiene, not CAT |
| **G — Deep-semantic ceiling (NEW)** | Bug requires reasoning the model can't do | bigger LLM, not CAT |

**The implication for swe500:** of the 36% pass rate, some fraction of the FAILs are likely Pattern F (benchmark artifacts the agent can't beat) and Pattern G (genuine capacity ceiling). Both lower-bound what the framework can deliver on this model. To meaningfully push past 36%:

1. **Audit Pattern F prevalence** — sample 10 fails, check how many have correct-looking patches that are blocked by unrelated test infra. If >15%, real CAT pass rate is higher than headline.
2. **R29 FixVerify** still the best short-term move for the patterns we can fix.
3. **Model swap** (deepseek → claude-sonnet-4-6 or stronger) is the only path through Pattern G.
