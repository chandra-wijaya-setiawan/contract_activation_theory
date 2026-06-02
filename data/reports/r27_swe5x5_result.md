# R27 swe5x5 — `reason` field replaces `request_revision`

**Run:** `data/runs/swebench/2026-06-01/23-56-20-r27-swe5x5b/`
**Final score:** **17/25 = 68%** (vs R26 13/25 = 52%) — **+16 percentage points**

| Task | R26 | R27 | Δ |
|---|---|---|---|
| django-10880 | 1/5 | 2/5 | +1 |
| django-11239 | 3/5 | 4/5 | +1 |
| django-13315 | 0/5 | **3/5** | **+3** ← biggest win |
| matplotlib-24149 | 4/5 | 3/5 | −1 (variance) |
| pytest-6202 | 5/5 | 5/5 | 0 |

## What changed

R27 removed the `request_revision` SIGNAL tool (called 0/25 times in R26 — dead weight) and replaced it with an optional `reason: str` arg on every tool. The Performer is supposed to fill `reason` when its tool call deviates from `ExecutionSchema.suggested_tool`. The dispatch layer pops `reason` from args before the handler runs, surfaces it in the `tool_call` trace event, and stashes it on `InvocationSchema.reason` (the actual-execution record). This cleanly separates the plan side (`suggested_args`) from the actual side (`args + reason`).

## How the LLM actually used `reason`

| Pattern | Count | Pct |
|---|---|---|
| Total tool calls | 470 | 100% |
| Tool calls with `reason` set | 64 | 13% |
| Deviations WITH reason ✓ (target) | 11 | 24% of deviations |
| Deviations WITHOUT reason ✗ | 34 | 76% of deviations |
| Follows WITH reason (noise) | 43 | over-explanation |

The LLM uses `reason` **inverted from intent**: when following the plan it tends to over-explain ("Run the oracle test command to verify the changes work"), when deviating it tends to stay silent. The system-prompt rule "REQUIRED when you deviate" isn't being internalized strongly enough.

## Why R27 still won +16 points

The score moved without the LLM using `reason` correctly. Three plausible drivers, ordered by likelihood:

1. **System-prompt tightening as a side-effect.** Rewriting rule 8 to emphasize "the planner's suggested_tool is your strong default — call it when you can" appears to have nudged the Performer toward following suggestions more, regardless of whether it bothered with `reason`.
2. **Removing `request_revision` from the tool list.** Even though it was never used, it was visible in every prompt's tool catalog. Cutting it tightened the action space.
3. **Variance.** 5×5 is small. A real gain of +16pp could survive a variance test, but not at 95% confidence on this sample size.

## Next R-targets (unchanged from earlier report, but priorities reorder)

| R# | Change | Expected Δ |
|---|---|---|
| R27.1 | Strengthen `reason`-on-deviation prompt (or auto-detect deviation in the activator and force a retry with reason) | +1-2 trials if LLM compliance moves from 24% → 60%+ |
| R28 | UpdateStepOp + locate-before-mutate (kills the old_str hallucination loop in django-13315 trials 1/5 and 2/5) | +1-2 trials |
| R31 | VerifyStrict (capture bash exit code, reject compound cmds, require PASS string) | +1-2 trials, mostly on django-13315.5 and matplotlib-24149 |

Headline: CAT is now at **68% on swe5×5** with the deepseek-v4-flash backend and the new schema discipline. That's +25 points over the v14 baseline (43%) and is starting to look respectable for an open-weight, single-attempt agent.
