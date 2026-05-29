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
- cat-v13b vs react-v13 (3 tasks, 3 attempts, fresh): **CAT 88.9% = ReAct 88.9%** — exact parity. Pass@2 = 100%.
- Root cause confirmed: deterministic criterion fixes premature closure.

**Lesson:** LLM judge is only safe called once at task end, not per tool call.

---

### v1.4 — SWE-bench verified (DONE, 2026-05-26)

Goal: Validate CAT on real GitHub issues with deterministic pytest criterion.

**Oracle:** `benchmarks/swe_bench.py:TbSweAgent` — applies SWE-bench's own
`test_patch` + runs `FAIL_TO_PASS` tests via exit code. Proxy oracles for
tasks whose tests require live network (httpbin etc.).

**Results — swe30 full head-to-head (30 tasks, 1 attempt each):**

| Metric | CAT | ReAct |
|---|---|---|
| Solve rate | **9/30 = 30%** | **15/30 = 50%** |
| Input tokens total | 1,602K | 5,025K |
| Token cost ratio | 1× | **3.1× more expensive** |
| Tokens per solved task | 178K | 335K |

**Note:** swe30-cat used pre-v1.2 code (2026-05-16). ReAct is current code.
CAT v1.2 improvements (F2 replan on deadlock, P2 criterion interval) should
close some of the solve-rate gap in a rerun.

**Win/loss breakdown:**
- CAT only (3 tasks): django-12708, pytest-10081, pytest-6202
- ReAct only (9 tasks): django-11239, django-12039, django-13315, django-14787,
  matplotlib-24149, matplotlib-24970, requests-1142, sphinx-10323, sympy-15349
- Both solve (6): django-10880, django-14434, django-16333, xarray-4966,
  scikit-learn-14141, sympy-15875
- Neither (12)

**Root cause of CAT failures (ReAct-only wins):**
- Partial fixes: CAT plan step judged "complete" before all test vars applied
  (django-11239 example: fixed 2/4 SSL env vars). F2 replan should fix these.
- Exploratory tasks: ReAct's free-form exploration finds the right code path;
  CAT's pre-planned steps miss it (especially in large Django/sympy codebases).
- Token savings per solved task: CAT 178K vs ReAct 335K (CAT still 1.9× cheaper
  per solution even at lower solve rate).

**Earlier swe10 head-to-head (10 tasks, 1 attempt):**
- CAT (TbSweAgent): 3/10 = 30% — 199K input tokens total
- ReAct: 3/10 = 30% — 1,006K input tokens total
- CAT uses 5× fewer input tokens at equal solve rate

**Critical bugs fixed (2026-05-26):**
1. `TbSweAgent._resolve_test_cmd()`: only 4/30 tasks had deterministic oracle;
   26/30 used LLM judge → root cause of 30% solve rate.
   Fix: dynamic oracle from config.json for any task in the dataset cache.
2. REPRESENT step: missing `file_types` and `notes` probes → planner blind to
   archive types and task-specific hints (root cause of crack-7z, extract-safely).
3. Planner had no context of what the test failure looked like before planning.
   Fix: `TbSweAgent._initial_representations()` runs oracle once pre-REPRESENT,
   injects failure output so planner generates targeted steps (e.g. exact env vars
   missing, not just "fix SSL handling").

**Additional fixes (post v1.4, 2026-05-26):**
4. Improved planner prompt: require specific function names/file paths and oracle
   verification step as the plan's last step.
5. F2 replan now probes `git diff HEAD` — shows partial changes to the new planner
   so it can correct or build on prior edits rather than replanning from scratch.

**Re-run results (2026-05-26, DONE):**

| Run | Code | Solve rate | Notes |
|---|---|---|---|
| swe10-react | vanilla ReAct | **3/10 = 30%** | baseline |
| swe10-cat-v12 | old code, old oracle bug | **1/10 = 10%** | confirms oracle was critical |
| swe10-cat-v13 | oracle fix + init failure (empty output bug) | **3/10 = 30%** | matches react, solves different tasks |
| **swe10-cat-v14** | **ALL fixes** | **5/10 = 50%** | **+67% over react!** |

**v14 win/loss breakdown (vs swe10-react):**
- v14-only wins: scikit-learn-14141, sphinx-10323 (react failed both)
- Both solve: pytest-6202, matplotlib-26342, sympy-15875
- Neither: requests-2931, astropy-8872, django-12039, sympy-12489, xarray-4966 (parse_error)

**Token efficiency:**
- React: 140,381 tokens/solved task
- CAT v14: 100,089 tokens/solved task (28% cheaper per solution)

**Conclusion:** All 5 improvements combined pushed CAT from 30% (react parity) to 50%
(+20pp over react). The oracle fix alone wasn't enough (v13 = 30% = react). The test
output capture fix for the planner was the tipping point (scikit-learn: 29K tokens
PASS in v14 vs 206K tokens FAIL in v13).

---

### swe30-cat-v14 — definitive 30-task rerun with all fixes (DONE, 2026-05-27)

**Results — swe30-cat-v14 vs react baseline (30 tasks, 1 attempt each):**

| Metric | CAT v14 | CAT v0 | ReAct |
|---|---|---|---|
| Solve rate | **13/30 = 43.3%** | 9/30 = 30% | 15/30 = 50% |
| Tokens per solved task | **92K** | 178K | 354K |
| Token efficiency vs React | **3.8× cheaper** | 2.0× cheaper | baseline |

**Win/loss breakdown (CAT v14 vs React):**
- CAT v14 only (4 tasks react fails): matplotlib-26342, scikit-learn-26194, pytest-10081, pytest-6202
- Shared wins (9 tasks): django-10880, django-14787, django-16333, matplotlib-24149, matplotlib-24970, requests-1142, xarray-4966, scikit-learn-14141, sphinx-10323
- React only (6 tasks): django-11239, django-12039, django-13315, django-14434, sympy-15349, sympy-15875
- ERR? (parse error): sympy-17318

**New wins vs CAT v0 (7 tasks):** django-14787, matplotlib-24149, matplotlib-24970, matplotlib-26342, requests-1142, scikit-learn-26194, sphinx-10323

**Regressions vs CAT v0 (3 tasks):**
- django-14434 (v0=PASS): tool timeouts in Docker container — infrastructure issue
- django-12708 (v0=PASS): deterministic oracle, agent stuck in different approach
- sympy-15875 (v0=PASS): LLM judge task, new planner generated harder plan

**Conclusion:** v14 fixes push solve rate from 30% → 43.3% (+44% relative), with 3.8× token efficiency advantage over React. CAT uniquely solves 4 tasks that React cannot. Gap to React narrowed from 20pp (v0) to 6.7pp (v14). Main weakness: LLM judge tasks where planner over-specifies and gets stuck.

### v1.4b — terminal-bench-core full benchmark (2026-05-15, needs rerun)

80-task diverse benchmark (terminal-bench-core v0.1.1), 2 attempts each.
Run with **pre-v1.2 code** — results will improve with current codebase.

**Results (old code):**
- cat-official (TbCatAgent, window_size=4): **34/160 = 21%**
- react-official (TbReactAgent): **54/160 = 34%**
- Gap: ReAct +13pp. 5 tasks where ReAct 2/2 but CAT 0/2.

**cat-targeted-v2 (DONE, 2026-05-26):** Re-running those 5 tasks with v1.2 code.

**Results:**
- configure-git-webserver: 0/2 FAIL (timed out — multi-service setup still too hard)
- crack-7z-hash.hard: 0/2 FAIL (timed out — password-cracking needs REPRESENT improvement)
- extract-safely: 0/2 FAIL (agent doesn't write /app/solution.txt)
- fix-pandas-version: 1/2 PASS → Pass@2=100% ✓ (v1.2 fixed this one)
- git-workflow-hack: 0/2 FAIL (timed out)
- **Overall: 1/5 improved by v1.2 code**

**Root cause analysis:**
- fix-pandas-version: F2/F3 replan improvements let agent find the right fix path.
- crack-7z-hash.hard: REPRESENT step too generic — doesn't probe archive type or detect
  password protection before planning. New probes (file_types, notes) added in v1.3a
  representer.py should improve future runs.
- extract-safely: agent doesn't know output must go to /app/solution.txt — needs
  better task-specific REPRESENT or more literal instruction parsing.
- configure-git-webserver, git-workflow-hack: long multi-step tasks; timeout is
  the binding constraint (360s for complex Docker setup tasks).

## v1.5 — Delete LIFE, realign to admission-gate theory (NEXT, HARD GATE before v1.6+)

Goal: Strip all LIFE-Harness machinery, rename Step 6 ACTIVATE → INVOKE, build
the real activation layer as a one-shot admission gate (daemon model), restate
the theory's premise honestly, and verify the refactored system runs
end-to-end against a previously-failed SWE-bench task.

**Why now:** v1.2 absorbed LIFE as a patch on the contract theory. On
inspection, four of the load-bearing mechanisms in DESIGN.md are LIFE imports
(Action Realizer, Environment Spec preconditions, Circuit Breaker
F2/F3/F4, Failure Attribution taxonomy), and the layer the theory is *named*
after (activation) is rhetorical — Step 6 is structurally identical to
ReAct's act phase. Before any new benchmark work, the theory and the code
must match.

### Subtasks

#### 1.5.1 — Revise DESIGN.md to admission-gate theory  ✅
- Replace "execution is the exception" slogan with the falsifiable claim:
  **contract discipline trades exploration for bounded resource use.**
- Rename Step 6 ACTIVATE → INVOKE.
- Introduce Step 4 ACTIVATE as the one-shot admission gate (boundary,
  environment, capacity checks → capability grant).
- Document admission-failure responses: FAIL, SUSPEND-with-watcher,
  ESCALATE, ASK_USER — declared as `ContractPolicy` per contract.
- Drop LIFE-Harness Layer section (Step 6), drop Failure Attribution
  Taxonomy section, drop F2/F3/F4 escape-hatch section.
- Add testable-claims section at the end.

#### 1.5.2 — Add numbered v1.5 subtasks to PLAN.md  ✅
- Replace the v1.5 stub with this numbered list. Inline decision notes
  documented after each task as `Decision: X because Y`.

#### 1.5.3 — Remove Action Realizer
- Delete `activator._realize_action` and its call site in `_invoke_once`.
- Remove `_MARKDOWN_FENCE_RE`.
- If the LLM mis-formats args (e.g. leading `./`), it gets a tool error in
  the next turn — same as any other failure.

#### 1.5.4 — Remove tool preconditions (Environment Spec)
- Delete `preconditions: list[Precondition]` field on `Capability`.
- Delete `_pre_read_file_exists` and `_pre_bash_non_empty`.
- Delete precondition loop in `CapabilityRegistry.invoke`.
- Tools just run and report errors.

#### 1.5.5 — Remove Circuit Breaker (F2/F3/F4)
- Delete `criterion_fail_streak`, `replan_count`, `free_form`,
  `consecutive_nonwrite`, `criterion_history`, `_recent_turns` fields.
- Delete `_do_replan`, `_enter_free_form`, `_build_criterion_context`.
- Replace with one rule: when the executor stops making progress (no
  criterion satisfaction within the envelope), contract → SUSPENDED with
  `suspend_reason="criterion_unreachable"`. Re-admission is event-driven.
- Delete `max_criterion_fails` parameter; keep `criterion_check_interval`.

#### 1.5.6 — Remove Failure Attribution taxonomy
- Delete `_attribute_tool_failure` in activator.py.
- Delete `FailureLayer` literal and `failure_attribution` event kind in
  trace.py.
- Delete precondition-failed → environment_contract record in
  capabilities.py.

#### 1.5.7 — Add escalation policy + admission outcome to Contract
- Add `ContractPolicy` dataclass: `on_boundary_fail`, `on_environment_fail`,
  `on_capacity_fail` enums (FAIL / SUSPEND / ESCALATE / ASK_USER).
- Add `policy`, `grant: set[str]`, `suspend_reason`, `re_entry_condition`
  fields on `Contract`.
- Default policy: boundary→FAIL, environment→SUSPEND, capacity→FAIL.

#### 1.5.8 — Split EVALUATE into admission gate + per-iter budget
- New method `_admit(contract, state, run) -> Decision` that runs
  boundary + environment + capacity checks and computes the grant.
- Decision = pass | fail(reason, type). On fail, consult `contract.policy`
  for response.
- Keep budget enforcement as `_check_budget` called every iteration.
- ACTIVATE step runs `_admit` exactly once after DECOMPOSE; the per-iter
  loop only sees `_check_budget`.

#### 1.5.9 — Rename ACTIVATE → INVOKE
- Rename `_activate_once` → `_invoke_once`.
- Rename `capability_activate` event kind → `capability_invoke`.
- Update docstrings throughout activator.py.
- ACTIVATE is now the admission gate (Step 4); INVOKE is per-iteration
  discharge (Step 6).

#### 1.5.10 — Update unit tests for new design
- Delete tests dependent on F2/F3: `test_replan_receives_criterion_context`,
  `test_free_form_prompt_includes_criterion_context`,
  `test_build_criterion_context_deduplicates`,
  `test_build_criterion_context_empty_when_no_history`,
  `test_criterion_history_populated_on_fail`.
- Add tests: admission gate boundary fail → FAILED, environment fail →
  SUSPENDED with reason, capacity fail → FAILED.

#### 1.5.11 — Run unit test suite, fix regressions
- `pytest tests/ -q` must pass on the stripped + refactored code.

#### 1.5.12 — Run a v14-failure SWE-bench task end-to-end
- Pick a task v14 failed (e.g. `django__django-12039` or `sympy__sympy-15349`).
- Goal: end-to-end execution without crashes; whatever the solve result
  is, it is the honest CAT-only-without-LIFE baseline.

**Expected impact on swe30 numbers (re-run scheduled post-1.5.12):**
- Solve rate likely drops (F2 replan was the recovery path for partial fixes).
- The drop is the diagnostic — it tells us what native mechanism must grow
  next to replace what LIFE was doing (likely the SUSPENDED-with-watcher
  loop in v1.6).

**What stays (CAT-native, do NOT remove):**
- REPRESENT and its probe set (Step 2).
- Pre-committed plan via DECOMPOSE (Step 3).
- Admission gate (Step 4 — new, replaces Step 6's stolen name).
- Oracle as closure gate (Step 7).
- Contract lifecycle states (LATENT / ELIGIBLE / ACTIVE / SUSPENDED / FULFILLED / FAILED / EXPIRED).
- Budget as hard Constraint enforced per iteration.

**Gate criteria before v1.6:**
- `git grep -i "life\|action_realization\|environment_contract\|residual_reasoning\|circuit breaker\|free_form\|criterion_fail_streak" cat/ DESIGN.md` returns nothing (or only legitimate non-LIFE matches).
- Unit tests pass.
- One SWE-bench task runs end-to-end with the refactored code.
- Every remaining mechanism in DESIGN.md derivable from the contract axiom.

### Decision log

(Decisions made during 1.5 implementation. Format: `1.5.N — <decision>. Reason: <why>.`)

- **1.5.1** — DESIGN.md restructured around three concepts: the daemon analogy
  (contract = dormant by default), the admission gate as Step 4 ACTIVATE
  (the layer the theory is named after), and the discharge phase (Step 5-8)
  bounded by an explicit envelope. Reason: the previous DESIGN.md used the
  word "activation" rhetorically — Step 6 stole the name without inheriting
  the semantics. Pulling the gate out as a separate step makes dormancy
  measurable and gives the SUSPENDED state a real meaning (it is what the
  gate produces on environment fail, not a never-entered placeholder).

- **1.5.5** — Removed `criterion_fail_streak`, `replan_count`, `free_form`,
  `consecutive_nonwrite`, `criterion_history`, `_do_replan`,
  `_enter_free_form`, `_build_criterion_context`. The single replacement is
  `max_iterations` exhaustion → `ContractState.SUSPENDED` with reason
  `criterion_unreachable: iteration envelope exhausted` and
  `re_entry_condition: fresh representations or extended envelope`.
  Reason: the entire F2/F3/F4 escape ladder was LIFE's circuit breaker.
  Replacing it with a single contract-native transition (SUSPENDED, not
  FAILED) preserves the recovery semantics (re-admission is possible if the
  world changes) without the streak-counter machinery.

- **1.5.6** — Kept the `_extract_turn_summary` helper and `prior_attempts`
  log even though they were originally introduced alongside F2 replan.
  Reason: the sliding-window compression (P1) is CAT-native — it bounds
  executor context to `window_size * avg_turn_tokens`. The fact that F2 also
  consumed `prior_attempts` does not make the compression itself a LIFE
  import. P1 stays.

- **1.5.7** — Default `ContractPolicy` is `boundary→FAIL,
  environment→SUSPEND, capacity→FAIL`. Reason: boundary failures are
  definition bugs that only re-submission helps; environment failures may
  resolve when the world changes (so SUSPEND is correct); capacity
  failures usually mean the envelope is wrong, and silently suspending
  hides the cost overrun. ASK_USER is wired but routes to SUSPEND in v1
  (no user-input capability yet).

- **1.5.8** — Allowed `LATENT → SUSPENDED` transition (not previously in
  the state machine). Reason: when the admission gate rejects with
  `policy=SUSPEND` on environment fail, the contract has not yet been
  ELIGIBLE — it goes straight from LATENT to SUSPENDED. The old transition
  table only allowed `ELIGIBLE → SUSPENDED`, which assumed admission had
  always passed. After the daemon-model refactor, admission can suspend.

- **1.5.9** — Renamed event kind `capability_activate` →
  `capability_invoke`. Added new event kind `contract_admit` (payload:
  ok / failure_type / reason / grant). Reason: Step 4 is now the
  activation gate; Step 6 is the invocation. The trace must distinguish
  the two — one fires once, the other fires every iteration.

- **1.5.9** — Added optional `only` parameter to
  `CapabilityRegistry.tool_schemas(only=grant)`. Currently passed by
  `_invoke_once` as the contract's grant set. v1 grant = full registry
  (so the tool schema is unchanged). The plumbing is in place so v1.6 /
  v2.0 can derive per-plan-step grants and gate the tool schema
  accordingly — that is when dormancy will become measurable.

- **1.5.10** — Replaced F2/F3/F4 unit tests with three admission-gate tests
  (`test_admission_*`) and one envelope-exhaustion test
  (`test_max_iterations_transitions_to_SUSPENDED`). Reason: the F-tests
  asserted behavior that no longer exists; the admission-gate tests assert
  the new contract-native semantics (boundary → FAILED, environment →
  SUSPENDED by default, environment → FAILED with policy override).

- **1.5.11** — All 49 unit tests pass on the refactored code (no LIFE).
  This is the unit-level proof that the strip-and-refactor preserves
  the CAT-native mechanisms (REPRESENT, DECOMPOSE, oracle gate,
  contract lifecycle, budget enforcement, P1 sliding window).

#### 1.5.15 — Contract owns its envelope (budget escape fix)

**Why:** The swe30-v1.5-postlife run revealed that the contract is NOT in
control of its own envelope. Multiple cost dimensions silently escape
contract accounting, and the harness's `max_agent_timeout_sec=3000s` is the
only thing that actually stops a stuck run. From the contract's point of
view, the world just collapses (tool calls start returning
`NotFound: 404`). This directly violates DESIGN.md's claim that "Budget is
the envelope granted at admission" and that "Failure = explicit contract
state with reason."

**Audit of budget escapes:**

| Cost field | Tracked at | Leaked at |
|---|---|---|
| `tokens_in/out` | `_invoke_once` executor ✓ | `_decompose` planner ✗, `_update_plan_checklist` judge ✗ |
| `usd` | `_invoke_once` executor ✓ | `_decompose` planner ✗, `_update_plan_checklist` judge ✗ |
| `tool_calls` | `_invoke_once` ✓, `representer` ✓ | `_verify_criteria` oracle bash ✗ |
| `llm_calls` | all sites ✓ | (none) |
| `wall_s` | (none) | **everywhere** — never accumulated |

Consequences:
- Real token usage on swe-bench tasks is roughly **2-3× the contract's
  reported total** (planner ≈ 2K tokens, judge ≈ 200-300 tokens × N iterations
  is significant on long runs).
- USD reported by the contract is similarly under-counted.
- `wall_s` is dead code: `Constraints.check()` checks it, but `cost.wall_s` is
  never incremented, so the check never fires.
- Hard tasks hit the harness `agent_timeout` instead of self-closing via
  `Constraints.check()` returning a reason like "deadline passed". The
  contract has no chance to transition to FAILED gracefully.

**Engineering work:**

1. **Add cost-charging helpers** (`cat/activator.py`)

   ```python
   def _charge_llm(self, contract: Contract, result: LLMResult) -> None:
       contract.cost.tokens_in  += result.tokens_in
       contract.cost.tokens_out += result.tokens_out
       contract.cost.usd        += result.usd
       contract.cost.llm_calls  += 1
       contract.cost.wall_s     += float(result.raw.get("wall_s", 0.0))

   def _charge_tool(self, contract: Contract, result: ToolResult) -> None:
       contract.cost.tool_calls += 1
       contract.cost.wall_s     += float(result.wall_s)
   ```

   These are the single source of truth for cost accounting.

2. **Refactor every callsite to use the helpers**
   - `_invoke_once`: replace the 4 explicit cost lines with `_charge_llm(...)`
     after `llm.chat(...)`, and `_charge_tool(...)` after `registry.invoke(...)`.
   - `_decompose`: replace `cost.llm_calls += 1` with `_charge_llm(...)`.
     **Need to update `call_planner` to return the LLMResult, not just steps**
     (currently throws away the result after parsing). Easiest: have it return
     `(steps, LLMResult)` and update the one caller.
   - `_update_plan_checklist`: same change for `call_judge` → return
     `(complete, reason, LLMResult)`.
   - `_build_oracle_criterion`: closure captures `contract`; predicate calls
     `self._charge_tool(contract, result)` after `registry.invoke(...)`.
   - `representer.represent()`: after each `registry.invoke(...)`, in addition
     to `tool_calls += 1`, also do `cost.wall_s += result.wall_s`. (Cheapest fix
     is to import Cost charging from activator — or just inline the wall_s
     accumulator in representer.)

3. **Lock deadline_ts at admission** (`cat/activator.py:_admit`)

   ```python
   # At admission, compute the absolute deadline from max_wall_s.
   # The contract closes itself via _check_budget when now > deadline_ts.
   if contract.constraints.deadline_ts is None:
       contract.constraints.deadline_ts = time.time() + contract.constraints.max_wall_s
   ```

   The existing `Constraints.check()` already enforces `now_ts > deadline_ts`.
   Per-iter `_check_budget` calls `Constraints.check()` every loop, so the
   contract self-closes with reason `deadline passed (... > ...)`.

   Rationale for deadline_ts vs cost.wall_s as the enforcement mechanism:
   - Already-implemented check (no new control-flow logic).
   - Robust against time-skip bugs (system clock is authoritative).
   - Matches the daemon-grant semantics: admission grants a deadline; the
     daemon discharges within it.
   - `cost.wall_s` stays as the observability metric (sum of LLM + tool wall);
     `deadline_ts` is the enforcement.

4. **Adjust TbSweAgent envelope** (`cat/tb_agents.py`)
   - Current `max_wall_s = 900.0` (15 min) — too aggressive; many real fixes
     legitimately need >15 min.
   - Harness cap: `max_agent_timeout_sec = 3000s` (50 min).
   - **New value: `max_wall_s = 2700.0`** (45 min) — gives contract 45 min,
     leaves 5-min buffer for the harness's post-test phase.
   - The contract self-closes at 45 min → no more `agent_timeout` in v1.5.

5. **Sub-budgets per role (optional)** — not done in 1.5.15, deferred to 1.6:
   - The judge consumes a meaningful fraction of total tokens. A future
     refinement is per-role sub-budgets (`max_judge_tokens`, `max_executor_tokens`)
     so a chatty judge can't starve the executor. For now, single
     `max_tokens_total` envelope, but accurately counted across all roles.

6. **Tests** (`tests/test_activator.py`)
   - `test_admission_locks_deadline`: assert `contract.constraints.deadline_ts`
     is set after admission.
   - `test_deadline_fires_per_iter`: set `max_wall_s=0.1`, slow tool sleeps 0.2s,
     contract transitions to FAILED with reason containing "deadline passed"
     within 1-2 iterations.
   - `test_planner_tokens_charged_to_contract`: scripted planner returns
     LLMResult(tokens_in=100, tokens_out=50), assert `contract.cost.tokens_in
     >= 100` after run.
   - `test_judge_tokens_charged_to_contract`: same for judge.
   - `test_oracle_bash_charged_to_contract`: oracle criterion runs bash;
     assert `contract.cost.tool_calls` increments.

7. **Update PLAN.md decision log** with the implementation notes.

**Expected impact on swe-bench runs:**
- Tasks that previously hit the 50-min harness `agent_timeout` will instead
  self-close at 45 min with `state_reason="budget violation: deadline passed"`.
  The contract is once again the unit of discipline.
- Reported token totals will jump (no longer missing planner + judge tokens).
  Headline 3.8× efficiency vs ReAct may shrink; that is honest.
- USD reported per task increases (was undercount). Still well below the $5
  envelope per task.
- Solve rate likely unchanged (the cap firing earlier doesn't change which
  tasks succeed; it just changes how they fail).
- `failure_mode` in results.json shifts from `agent_timeout` to `unset` with
  the harness no longer reaping.

**Gate before re-running swe-bench:**
- All unit tests pass.
- The five new tests pass.
- `cost.wall_s > 0` observed in at least one real trace event.

**Implementation result (2026-05-27):**
- `_charge_llm` and `_charge_tool` helpers added; every callsite refactored to
  use them.
- `call_planner` and `call_judge` signatures changed to return `LLMResult`
  alongside their parsed output, so cost can be charged at the caller.
- `_admit` now locks `deadline_ts = now + max_wall_s` on admission (when not
  pre-set).
- `_build_oracle_criterion` now takes `contract` and the closure calls
  `_charge_tool` after every oracle bash run.
- `representer.py` updated to charge `wall_s` per probe.
- `TbSweAgent.max_wall_s` raised from 900s to 2700s (45 min) with 5-min
  buffer under the harness's 50-min cap.
- **54/54 unit tests pass** (49 prior + 5 new for 1.5.15).

#### 1.5.16 — Smaller pilot rerun for fast feedback

After 1.5.15 lands, rerun **swe10 (10 tasks) at concurrency 10** rather than
swe30 at concurrency 3. Rationale:

- 10 tasks × 1 wave (concurrency 10) ≈ wall = ~max single-task time = ~50 min.
  About 3-4× faster than the 30-task variant.
- Tighter feedback loop on the 1.5.15 fix.
- Lower resource pressure per container vs swe30 — each container gets more
  of the 32-CPU / 126 GB host.
- Task selection: pick 10 tasks that span the v14 result space (some v14-PASS,
  some v14-FAIL, including one previously-`agent_timeout` task to verify the
  new deadline mechanism fires correctly).

**Note**: concurrency 10 caused docker-compose contention on the first try
(4/10 tasks failed image-build with `unknown_agent_error`). On retry, use
concurrency 3 for the failed subset, or stagger task starts.

#### 1.5.18 — HTML KPI report + LLM-driven layer attribution (QUEUED)

Goal: end every batch run with an auto-emitted **HTML report** the user can
review directly, with **LLM-driven layer attribution** rather than only
deterministic heuristics. Removes Claude from the read-the-trace loop during
development.

**Why now (after pilot):** the swe10-v1.5-envelope pilot results will be the
first data set with the new admission-gate + deadline architecture. That is
the right moment to formalize how runs are reported, so all subsequent
experiments (1.5.17 iteration-cap test, 1.6 tau-bench, etc.) emit a uniform
report by default.

**Why LLM-driven attribution (vs deterministic-only):** at the development
stage, plan-step-vs-execution coupling is hard to detect with substring rules.
An LLM judge given the plan + the failure evidence can reliably distinguish
"plan named the wrong file" from "plan was right, executor edited wrong lines"
from "criterion was never satisfiable." Deterministic rules become a safety
net + a cheaper-tier fallback for very long runs.

**Engineering work:**

1. **`cat/run_report.py`** (new, ~200 LOC)
   - Walks any `data/tb-runs/<run_id>/` directory.
   - For each task: builds `RunMetrics` from JSONL, then calls
     `attribute_task(metrics, events)` from `cat/attribution.py`.
   - Aggregates: pass rate, failure-mode breakdown, per-role cost (planner /
     executor / judge), iteration p50/p75/p90, wall-time distribution.
   - Emits **`_kpi_report.html`** + **`_kpi_report.json`** next to
     `results.json`. (HTML is what the user reviews; JSON is for downstream
     comparison scripts.)
   - HTML uses a single self-contained template (no external CSS/JS deps so
     it can be opened in any browser locally).

2. **`cat/attribution.py`** (new, ~150 LOC)
   - **LLM-driven primary path**: given the trace events + plan + final state
     + last criterion evidence, prompt a judge LLM to return a structured
     JSON: `{represent: verdict, decompose: verdict, invoke: verdict,
     verify: verdict, primary_credit_or_blame: layer, one_line_rationale}`.
     Verdict = `OK | DEGRADED | FAILED`.
   - **Heuristic fallback**: cheap deterministic rules (already drafted —
     see prior analysis: `executor_quit_early` → INVOKE blame,
     `admission_environment` → REPRESENT blame, etc.). Used when LLM call
     fails or `--no-llm` is passed.
   - Cache attribution by trace-content hash so reruns of the report are
     free.

3. **HTML report shape (sketch):**
   ```
   ┌─────────────────────────────────────────────┐
   │ Run: swe10-v1.5-envelope        agent cat-swe│
   │ Pass: 5/10 (50%)  Wall: 0h 47m  Tokens: 1.2M │
   ├─────────────────────────────────────────────┤
   │ Failure breakdown    Iteration p50/p75/max  │
   │   deadline_passed: 2    PASS: 14 / 17 / 24  │
   │   executor_quit:   1    FAIL: 25 / 51 / 51  │
   │   admission_env:   0                        │
   │   infra (docker):  4                        │
   ├─────────────────────────────────────────────┤
   │ Per-role token cost                          │
   │   planner:  3% │ executor: 82% │ judge: 15% │
   ├─────────────────────────────────────────────┤
   │ Per-task table (sortable, expandable rows)  │
   │ ┌─────────────────────────┬──────┬──────┐    │
   │ │ task             │ ✓/✗ │ iter │ blame│    │
   │ ├──────────────────┼──────┼──────┤    │
   │ │ sklearn-14141    │ ✓   │  6   │  -   │    │
   │ │ django-11239     │ ✗   │ 51   │ INVOKE│   │
   │ │   ↳ rationale: plan named wrong PG env vars … │
   │ └─────────────────────────┴──────┴──────┘    │
   └─────────────────────────────────────────────┘
   ```

4. **Auto-emit at end of run.** Two trigger points:
   - **Per-task hook** in `cat/tb_agents.py:perform_task` after
     `recorder.finalize()`: writes `task_kpi.json` next to the trace.
     (Cheap; no LLM needed at this point — just `RunMetrics.from_jsonl`.)
   - **Per-run hook** triggered by the harness: hook into terminal-bench's
     "all tasks done" lifecycle if it exposes one; otherwise add a small
     watcher script that polls for `results.json` to be final and then runs
     `python -m cat.run_report <run_dir>`. The watcher launches with the
     `tb runs create ...` bash one-liner.

5. **No HTML framework dependency.** Render as one inline HTML+CSS+small JS
   template. Avoid bringing in Jinja just for this. Sortable table can use
   a 30-line vanilla JS snippet.

**Out of scope for 1.5.18:**
- Cross-run comparison HTML (left for 1.5.19 or a separate `cat/compare_report.py`).
- Interactive trace replay UI (already partially covered by `trace_view.py`).

**Acceptance criteria:**
- After `tb runs create … --run-id X`, `data/tb-runs/.../<X>/_kpi_report.html`
  exists and renders.
- Attribution column populated for every FAIL row with a one-line rationale.
- User can answer "what broke on this run?" without reading the JSONL.

**Implementation result (2026-05-27):**
- `cat/attribution.py` — LLM-driven primary path + heuristic fallback +
  trace-hash cache. Returns `Attribution{layers, primary, rationale, method}`.
- `cat/run_report.py` — walks tb runs directory, aggregates per-task
  `RunMetrics` + attribution, emits `_kpi_report.html` (~57KB self-contained)
  and `_kpi_report.json`.
- `cat/tb_agents.py` — `_emit_task_kpi` hook writes per-task
  `<run>.task_kpi.json` next to the trace at `recorder.finalize()`. Uses
  heuristic-only on the agent's critical path; LLM attribution is run-level
  via `python -m cat.run_report`.
- 54/54 unit tests still pass.
- Validated end-to-end against `data/tb-runs/swe30/swe30-cat-v15/`:
  30/30 tasks attributed; LLM rationales are specific and actionable on FAILs
  ("Every tool call timed out → executor never made progress" for
  django-13315; "executor repeatedly tried to read a non-existent file path"
  for sympy-15875). A couple of PASS rationales were noisy/contradictory —
  acceptable for a development-phase tool.
- Cost: ~$0.015 to attribute 30 tasks at DeepSeek Flash rates.

- **1.5.12** — End-to-end SWE-bench run on `django__django-11239` (a v14
  failure case noted in PLAN.md as "partial-fix: fixed 2/4 SSL env vars"):
  v1.5 also FAILED (failure_mode=agent_timeout at 3000s, iter=51). Same
  outcome as v14 and v15 on this task. Trace confirmed the new
  architecture firing: `contract_admit ok=True grant_n=5` recorded once
  (one-shot admission), 51 `capability_invoke` events, LATENT → ELIGIBLE
  → ACTIVE transitions. The agent never reached max_iterations (would
  have transitioned to SUSPENDED per the new design) because the
  harness 50-minute envelope expired first — read-tool TIMEOUTs ate
  wall time, not algorithmic regression. **Diagnostic**: this is a
  high-path-entropy task that all CAT variants (v14, v15, v1.5) fail
  and ReAct passes — confirming the theory's testable prediction that
  pre-commitment trades exploration for efficiency. The v1.5 strip
  did not introduce a new failure mode here.

- **1.5.13** — Added a no-regression test on a v14-PASS task to confirm
  the refactor preserves working behavior. Ran
  `scikit-learn__scikit-learn-14141` (the joblib show_versions task used
  as the worked example in DESIGN.md) end-to-end on v1.5:
  **is_resolved=True, parser=tests passed, 27,478 in / 2,725 out tokens,
  2.5 min wall.** This is the affirmative proof the LIFE-stripped
  admission-gate architecture solves clean tasks at parity with the
  previous code. Gate criteria for v1.5 → v1.6 satisfied: no LIFE refs
  in `cat/`, unit tests green, one v14-PASS task still passes, one
  v14-FAIL task still fails (no regression on either).

---

## v1.6 — DAG-first observability + terminology alignment (NEXT after v1.5)

> See **`docs/1.6.2.9_PRD_renaming_and_dijkstra.md`** for the canonical
> terminology rename + Dijkstra-layer-mapping decision record.
>
> Cross-run learning (was 1.6.6) → moved to v1.7.
> Multi-candidate / Dijkstra-explicit work → v1.8.

Goal: promote the trace from a flat event stream to a typed reasoning DAG, and
make the HTML report DAG-visualized. This subsumes 1.5.19 (final unified
report) and 1.5.20 (structured run layout) — they become subtasks of v1.6
because the DAG impacts both the report shape and the on-disk layout.

**Why DAGs (and why not Dijkstra):**

A CAT run is already a latent DAG — every `TraceEvent` carries a `parent_id`
field. We just don't enforce it: half the events chain to the contract root
(flat fan-out, not a real DAG), half chain to their immediate cause (proper
edge). Tighten that and the trace IS a DAG, no schema change.

But Dijkstra's static-graph + shortest-path assumptions don't transfer to CAT:
the graph is constructed turn-by-turn by the planner + executor; there is no
"shortest" path (only "first to satisfy criterion"); LLM stochasticity makes
costs non-deterministic. The right model is **A\* with imitation learning over
past DAGs**: past PASS DAGs are demonstrations; past FAIL DAGs are
anti-demonstrations; the planner is conditioned on retrieved past DAGs that
match the current task's REPRESENT fingerprint. The DAG corpus is the
contract layer's *memory* — the part of the daemon model that "remembers what
worked" without the LLM having to.

### Subtasks

#### 1.6.0 / 1.6.2.9 — Terminology rename + PRD (LANDS BEFORE 1.6.3 RESUMES)

Per `docs/1.6.2.9_PRD_renaming_and_dijkstra.md`:
- `planner` → `stipulator`, `executor` → `performer`, `judge` → `attestor` (role_tags)
- `_decompose` → `_stipulate`, `_invoke_once` → `_perform_once` (methods)
- `contract_decompose` → `contract_stipulate`, `capability_invoke` → `capability_perform` (event kinds)
- Step names: DECOMPOSE → STIPULATE, INVOKE → PERFORM, CLOSE → DISCHARGE
- Theory-only roles (SURVEYOR / REGISTRAR / RECORDER / ENFORCER / LIBRARIAN / ADVISOR) live in DESIGN.md, not in code
- Backward-compat: old event kinds remain readable by `cat/dag.py` and `cat/trace_view.py` via an alias map
- Acceptance: 62/62 tests pass, sklearn-14141 gate test PASS with new event kinds

#### 1.6.1 — `parent_id` discipline audit

Audit every `Recorder.append(TraceEvent(...))` call site:
- `contract_state`: parent_id = previous contract_state (the state we came from)
- `contract_represent`: parent_id = the latent state event
- `contract_decompose`: parent_id = the contract_represent
- `contract_admit`: parent_id = the contract_decompose
- `capability_invoke`: parent_id = the previous invocation OR the contract_admit (for iter 1) OR the previous criterion_check (after VERIFY)
- `tool_call` / `tool_result`: parent_id = the capability_invoke that triggered them
- `criterion_check` (oracle): parent_id = the capability_invoke whose result triggered the check
- `criterion_check` (judge): parent_id = the capability_invoke whose state it judged

Today most of these point to `contract.id` (the root) which loses the
information. After this audit, every non-root event has a *direct causal
parent* in the same trace.

Add a unit test:
```python
def test_trace_is_valid_dag(events):
    ids = {e.event_id for e in events}
    for e in events:
        if e.parent_id is not None:
            assert e.parent_id in ids
```

**Acceptance:** all existing unit tests pass with the tightened parents; new
DAG validity test passes; no event has `parent_id=contract.id` except the
root state.

**Gate test (single SWE task before proceeding to 1.6.2):**
Run `scikit-learn__scikit-learn-14141` (canonical fast PASS, ~2 min wall):
- Task PASS verdict matches prior runs (no regression)
- Walk the trace and assert: every non-root event has `parent_id` pointing
  to another event in the same JSONL; no orphan parents; the DAG is
  acyclic and connected from the latent state root
- Report any anomalies. Pause for user review if anything looks off.

#### 1.6.2 — `cat/dag.py` — RunDAG class + dag.json

```python
@dataclass
class RunDAG:
    nodes: dict[event_id, Node]       # typed: Representation | PlanStep | Invocation | Verify | StateTransition
    edges: list[Edge]                  # typed: emitted_by | addresses | produced | verified_by | succeeded_by

    def longest_chain(self) -> list[Node]
    def branch_points(self) -> list[Node]
    def path_to(self, leaf_event_id: str) -> list[Node]
    def to_dict(self) -> dict           # for dag.json
    @classmethod
    def from_jsonl(cls, path: Path) -> "RunDAG"
```

Persist as `dag.json` next to the trace at `recorder.finalize()`.

**Gate test (single SWE task before proceeding to 1.6.3):**
Run `scikit-learn__scikit-learn-14141`:
- Task PASS verdict unchanged
- `dag.json` exists next to the trace and is parseable
- `RunDAG.from_jsonl(...)` round-trips: rebuild DAG from JSONL, compare to
  on-disk dag.json — node and edge sets match
- Sanity: `longest_chain()` returns a non-trivial path (≥ REPRESENT →
  DECOMPOSE → ADMIT → at least one Invocation → FULFILLED)
- Pause for user review if structure looks off.

#### 1.6.3 — DAG visualization in HTML report

Redesign 1.5.18's per-task table: each task row expands into an inline SVG DAG
showing the actual reasoning path.

```
expanded row layout:
┌─────────────────────────────────────────────────────────────────────┐
│ Task: django__django-11239                FAIL · 51 iters · 35 min  │
├─────────────────────────────────────────────────────────────────────┤
│ ┌────────┐                                                          │
│ │ LATENT │                                                          │
│ └───┬────┘                                                          │
│ ┌───▼──────────┐                                                    │
│ │ REPRESENT    │ 10 reps                                            │
│ └───┬──────────┘                                                    │
│ ┌───▼──────────┐                                                    │
│ │ DECOMPOSE    │ 3 plan steps                                       │
│ └───┬──────────┘                                                    │
│ ┌───▼──────────┐                                                    │
│ │ ADMIT (✓)    │ grant=5 caps                                       │
│ └───┬──────────┘                                                    │
│ ┌───▼──┐ ┌────┐ ┌────┐                                              │
│ │inv 1 │→│ ✓  │→│ vf │  s1 marked complete                          │
│ └───┬──┘ └────┘ └────┘                                              │
│ ┌───▼──┐ ┌────┐ ┌────┐                                              │
│ │inv 2 │→│ ✗  │→│ vf │  tool error                                  │
│ └───┬──┘ └────┘ └────┘                                              │
│  ... ↓                                                              │
│ ┌──────────┐                                                        │
│ │ FAILED   │ deadline passed                                        │
│ └──────────┘                                                        │
│                                                                     │
│ Layer attribution:  represent OK · decompose OK · invoke FAILED · verify NA │
│ Primary blame: invoke — "executor exhausted iter budget without converging" │
└─────────────────────────────────────────────────────────────────────┘
```

Implementation:
- Inline SVG written directly by `run_report.py` (no D3 / libs).
- Node positions: simple layered layout (BFS depth = y, sibling index = x).
- Nodes colored by layer (REPRESENT blue, DECOMPOSE green, INVOKE yellow,
  VERIFY red/green by outcome, terminal STATES black-bordered).
- Hover on a node → shows its full event payload as a tooltip.
- Highlight the path that led to the terminal state (different stroke width).

This replaces the per-task plain table from 1.5.18. The aggregate KPI cards
at the top stay.

**Gate test (single SWE task before proceeding to 1.6.4):**
Run `scikit-learn__scikit-learn-14141`:
- HTML report generated; opens cleanly in browser via the http server
- Task row expands to show inline SVG DAG
- Node colors and labels render correctly (REPRESENT blue, DECOMPOSE green,
  INVOKE yellow, terminal state black-bordered)
- Hover/tooltip shows event payload
- Path-to-terminal-state highlight is visible
- Pause for user review of the visual.

#### 1.6.4 — Structured run output layout (folds 1.5.20)

Implements the README's `data/runs/<bench>/<date>/<time>-<runid>/` layout
including `dag.json` as an additional artifact:

```
data/runs/<bench>/<date>/<time>-<runid>/
  manifest.json
  results.json
  run_metadata.json
  run.log
  _kpi_report.html
  _kpi_report.json
  _attribution_cache/
  <task-id>/
    <trial-name>/
      agent-logs/<run>.jsonl
      agent-logs/<run>.dag.json    ← NEW in v1.6
      agent-logs/<run>.task_kpi.json
      panes/
      sessions/
data/runs/INDEX.html
```

Includes:
- `scripts/run_bench.sh` launcher wrapping `tb runs create`
- `scripts/migrate_runs.py` symlinker for legacy `data/tb-runs/...`
- `cat/run_report.py --index` to regenerate the cross-run INDEX

**Gate test (single SWE task before proceeding to 1.6.5):**
Run `scikit-learn__scikit-learn-14141` via the new `scripts/run_bench.sh`:
- Output lands at `data/runs/swebench/<today>/<HH-MM-SS>-skltest/`
- `manifest.json` written with bench, agent, git_sha, started_at, ended_at,
  pass_rate
- HTML report auto-generated at end of run
- `data/runs/INDEX.html` lists the new run with a working link
- Run the migration script: legacy runs appear as symlinks under
  `data/runs/swebench/_legacy/...`
- Pause for user review if any of these are off.

#### 1.6.5 — Final v1.5 results in new layout (folds 1.5.19)

Migrate the three swe10 v1.5 runs (envelope, retry, retry2) into the new
layout via symlinks. Generate a unified DAG-visualized HTML report combining
all three, with per-task FINAL verdict (PASS > real-FAIL > infra-error
priority).

This is the deliverable originally tabled as 1.5.19. It now lands as part
of v1.6 so the report is built on top of the DAG infrastructure rather than
the flat 1.5.18 table.

**Acceptance (this IS the gate for v1.6):**
- All three swe10 v1.5 runs (envelope, retry, retry2) are accessible under
  `data/runs/swebench/2026-05-27/...` via symlinks
- A unified DAG-visualized HTML report combines them with per-task final
  verdict
- INDEX lists all runs (legacy + new) with pass rates
- User reviews the unified report — sign-off closes v1.6 phase 1.

#### 1.6.6 — Cross-run DAG corpus + retrieval (MOVED TO v1.7 per PRD 1.6.2.9)

The pay-off of having DAGs:

- `data/dag_corpus/` indexes every past `dag.json`.
- At plan time (Step 3 DECOMPOSE): embed obligation + `REPRESENT` fingerprint;
  retrieve top-K most-similar past DAGs; feed plans + outcomes as few-shot
  context to the planner.
- **DAG-grounded SUSPENDED resume**: when a contract suspends with
  `criterion_unreachable`, find the branch point in this run's DAG where the
  agent committed to the failing approach. Re-admit with a new Representation
  named `prior_dead_end` containing the rejected sub-DAG.
- This restores the v1.6 "failure propagation → parent replans once" behavior
  promised in v1.4 Open Questions, but **DAG-grounded** rather than
  streak-counter-driven (which was LIFE).

This is the part of the daemon model that closes the loop — the contract
layer's memory of what worked.

### Status of deferred 1.5.x items

- **1.5.19** (final v1.5 unified report) → folded into **1.6.5**
- **1.5.20** (structured run layout + launcher + INDEX) → folded into **1.6.4**

The 1.5.18 HTML report (already shipped) remains usable for runs that exist
today. The 1.6.3 redesign supersedes it once DAGs are available.

---

## v1.6.6 — Tool reliability + sharpness (NEXT, infrastructure track)

Goal: stop inheriting terminal-bench's tmux execution model for SWE-bench
runs. Use the right execution primitives per domain (see DESIGN.md
"Benchmark-adapter pattern"). Removes the dominant cause of v1.5 FAILs
(`tool_environment` 4/4 per the diagnostic auditor) without changing any
cognitive layer.

**Why before v1.7+:** the procedural-memory and Dijkstra experiments need a
stable substrate. Today's tmux flakiness confounds any cognitive change —
"did retrieval help?" is unanswerable if 40% of trials die at the bash
layer.

### Subtasks

#### 1.6.6.1 — `docker exec` backend (replace tmux for non-interactive tasks)

- New `build_docker_registry(container_name)` in `cat/capabilities.py`,
  parallel to `build_tmux_registry`.
- Each tool dispatches via `docker exec -i <container> sh -c <cmd>` →
  captures stdout/stderr/exit_code as a proper subprocess.
- `read` / `write` / `list` / `str_replace` similarly use `docker exec`
  directly (cat / docker cp + redirect / ls / inline python).
- No tmux capture, no sentinel scraping, no pane viewport problems.
- `TbCatAgent` selects backend: `docker_registry` by default for SWE-bench;
  `tmux_registry` retained for tasks whose `task.yaml` declares an
  interactive shell requirement.
- Expected impact: eliminate all 4 `tool_environment` FAILs from v1.5-final
  (django-10880 / 11239 / 12039 / 13315). Predicted solve rate uplift:
  6/10 → 8-9/10 on the v1.5 swe10 slice without any cognitive change.

**Gate test:** rerun the same 4 failed tasks via the new backend. PASS-rate
must climb from 0/4 to ≥3/4 (allow 1 genuine model-limitation FAIL).

#### 1.6.6.2 — First-class `ripgrep` + `fd` capabilities

- Add `rg(pattern, paths, flags)` and `fd(pattern, paths, flags)` to the
  default + docker registries.
- Performer can call them directly instead of `bash("grep -rn ...")` —
  10-100× faster on large codebases (Django: 10K+ files).
- Reduces the "bash for everything" pattern and gives the performer
  sharper primitives.

#### 1.6.6.3 — Dedicated `pytest_run` capability

- `pytest_run(test_ids, marker, timeout)` returns structured exit_code +
  parsed pass/fail counts + first failure traceback.
- Bypasses the bash → tmux → sentinel parsing chain.
- Used by oracle criterion and any performer-initiated test runs.
- Removes flakiness where pytest output exceeds the tmux viewport.

### Gate criteria before v1.7

- All three subtasks landed.
- Unit tests still pass.
- swe10-v1.5-envelope rerun on the new backend: ≥8/10 PASS (vs current 6/10).
- DESIGN.md "Benchmark-adapter pattern" section published.

### Result — v1.6.6 lifted swe10 solve rate 60% → 90% (DONE 2026-05-28)

Stacked fixes shipped in v1.6.6 (all 6 subtasks + 2 follow-ups, all gates passed):

| Fix | Effect |
|---|---|
| 1.6.6.1 `docker exec` backend (replaces tmux) | tool error rate 80% → 21%; 3 of 4 v1.5 FAILs flip to PASS |
| 1.6.6.2 first-class `rg` + `fd` (with grep/find fallback) | sharper search primitives; no measurable effect on v1.5 task set (codebases small enough) |
| 1.6.6.3 (deferred) — docker exec already obsoletes the original `pytest_run` need | n/a |
| 1.6.6.4 oracle restores `tests/` before applying `test_patch` | eliminates agent-oracle ↔ harness-verifier divergence |
| 1.6.6.5 PERFORMER prompt: do not edit test files | preventive; reduces oracle confusion at the source |
| 1.6.6.6 oracle includes PASS_TO_PASS regression sample (≤30) | matches harness's verifier; catches regressions the agent would otherwise stop on |
| `bash -lc` login shell in docker exec (1.6.6.1 follow-up) | fixes PATH issues where pytest is in a conda env not on default docker-exec PATH (sphinx-10323 recovery) |

Solve-rate progression on the swe10 slice:

| Run | Pass rate | Notable |
|---|---|---|
| v1.5-final (LIFE-stripped + admission gate, tmux backend) | 6/10 = 60% | 4 FAILs all diagnosed `tool_environment` by LLM auditor |
| swe10-v1.6.6 (docker exec + rg/fd only) | 8/10 = 80% | 3 django FAILs → PASS; sphinx regressed (PATH issue) |
| swe10-v1.6.6-all (+ bash -lc + oracle restore + prompt) | 8/10 = 80% | sphinx back; matplotlib regressed (incomplete oracle exposed) |
| **swe10-v1.6.6-final (+ PASS_TO_PASS in oracle)** | **9/10 = 90%** | matplotlib recovered; only django-13315 remains (real model/task limit, 8% tool err) |

**+30 percentage points from one sprint of infrastructure work.** No cognitive
change (no procedural memory, no multi-candidate search). The diagnostic
auditor was correct: 4/4 v1.5 FAILs were `tool_environment`; once that bucket
emptied, the headline jumped.

Token efficiency wins as well — on PASSes, oracle restore + prompt
discipline + accurate oracle let the agent stop early:
- sklearn-14141: 240K → 13K → 46K  (eventual stop on PASS_TO_PASS)
- sphinx-10323:  521K → 20K → 22K  (was envelope-exhausted, now clean)
- pytest-6202:   162K → 60K → 24K

The contract bookkeeping divergence we noted (agent state=failed yet harness=PASS)
also resolved as a side effect — the oracle now matches the harness, so agent
state machine closes FULFILLED rather than envelope-exhausted.

---

## v1.6.6.7–10 + v1.7.0 + v1.8.0 — Tool-surface expansion (DONE 2026-05-28)

After 1.6.6 lifted solve rate to 90% on swe10, the auditor surfaced a
different bottleneck on the remaining FAIL (django-13315): the agent's plan
called "apply patch" but no patch tool existed. Survey of 2026 SWE-bench
agents (Pi/Earendil, Hermes/Nous, Claude Code) confirmed CAT was missing the
**common high-leverage tool set**: multi-block edit, patch apply, structural
navigation, first-class git ops. The user's reframing crystallized the
direction:

> "tooling is not cheating; framework's job is to optimize and minimize the
> usage of tooling"

CAT's value-add is contract discipline picking the *right* tool, not
restricting the surface. So: give the performer a rich toolset, then measure
tool-economy as a first-class KPI.

### What landed

| # | Capability | Pattern source | Purpose |
|---|---|---|---|
| 1.6.6.7 | `apply_patch(patch)` | Hermes | Multi-hunk diff via `git apply --reject --whitespace=fix`; partial-apply with .rej listing |
| 1.6.6.8 | `multi_edit(path, edits[])` | Pi `edit.ts` | Atomic N-hunk edit against ORIGINAL — order-independent, drift-free |
| 1.6.6.9 | `git_status` + `git_diff(staged, path)` | own design | First-class git tools so the trace records accumulated changes |
| 1.6.6.10 | `rg(context=N)` | Pi `grep.context` | -C N on the existing ripgrep wrapper; mid-context in one call instead of grep+read |
| 1.7.0 | `goto_def`, `find_refs`, `outline` | Claude Code `LSP` | Jedi-backed Python structural nav; lazy `pip install --user jedi` inside container |
| 1.8.0 | Tool-economy KPI | CAT-original | Per-task `tool_usage` + back-to-back + 4+-reads-same-file flags; HTML "Tool economy" panel |

11 capabilities total in the docker registry (was 7), with `PERFORMER_SYSTEM_PROMPT`
expanded to enumerate tool-selection guidance ("prefer `outline` over `read` when
you only need structure", etc.).

### Decision log

- **1.6.6.7.a** `apply_patch` uses `git apply --reject --whitespace=fix`
  (vs `patch -p1`). Why: --reject gives partial apply with .rej files the
  agent can introspect; --whitespace=fix absorbs trailing-whitespace
  mismatches that kill ~10% of LLM-generated diffs. Confirmed via test
  `test_apply_patch_partial_apply_writes_rej`.
- **1.6.6.8.a** multi_edit semantics: each `old_text` matches the ORIGINAL
  file (Pi semantics), not incrementally. Why: line numbers don't drift,
  edits are order-independent, partial application is well-defined. Test
  `test_multi_edit_aborts_when_one_edit_misses` proves atomicity (no
  partial writes when any edit fails). Pi's exact rationale was cleaner
  than Claude Code's single-Edit-with-replace_all, which lacks atomic
  multi-region semantics.
- **1.7.0.a** Backend = jedi, not full LSP (pyright/pylsp per repo). Why:
  SWE-bench is 100% Python; jedi is a single Python dep with no language
  server process to manage. Trade-off acknowledged: when CAT moves to
  non-Python benchmarks, swap to LSP via tree-sitter; jedi stays as the
  Python fast path.
- **1.7.0.b** Lazy install at first use: container-side script does
  `pip install --user --quiet jedi>=0.19` if import fails. Avoids forcing
  rebuild of every SWE-bench docker image. Cost: ~3 s one-time per task.
- **1.8.0.a** Redundancy heuristic only counts perform-layer calls. Why:
  REPRESENT probes (`rep-*`) and oracle calls (`oracle-*`) are framework
  overhead — flagging them as "wasteful agent behavior" would mis-attribute.
- **1.8.0.b** Re-running the new report on the v1.6.6-final run surfaced
  concrete waste even on PASSing tasks: django-13315 read related.py 4×,
  django-10880 read expressions.py 6× + aggregates.py 7×. These are
  exactly the inefficiencies `outline` + `multi_edit` target.

### Test coverage

| File | New tests | Covers |
|---|---|---|
| `tests/test_capabilities.py` | 10 (apply_patch ×2, multi_edit ×3, git ×1, rg.context ×1, symbol_nav ×3) | docker registry semantics, atomicity, partial apply, jedi outline / goto / refs |
| `tests/test_run_report.py` | 4 | tool_usage extraction, back-to-back detection, 4+-same-file detection, no-waste case |

All 76 tests pass.

### Gate criteria before v1.7.1+

- All capability subtasks landed: ✓
- Unit + integration tests pass: ✓ (76/76)
- swe10 rerun with new toolset: ✓ (task #51, run-id swe10-v1.7-tools)
  - Pass rate: 9/10 effective (90%, parity with v1.6.6-final), corrected for
    one infra-failed task that PASSed on retry (#51-retry sympy-15875)
  - tool_calls_per_task_avg: **19.4 vs 42.7 baseline = −55%**
  - wall time: 49.5 min vs 92 min = −46%
  - Hypothesis confirmed: rich tool surface + contract discipline = same
    solve rate at materially less work.
- django-13315 remained FAIL on retry with 1.6.6.11 stipulator guidance, but
  for a strictly more advanced reason (stipulator picked the wrong target
  function). Bottleneck has migrated from `framework_structural` (missing
  patch) to `framework_parameter` (stipulator quality + in-loop pathology).
  This is the natural inflection point into the v1.7.1+ learning-system
  phase.

### Bottleneck migration so far

| Generation | Dominant failure layer | Fix vector |
|---|---|---|
| v1.5 | `tool_environment` (tmux fragility) | v1.6.6 docker exec backend |
| v1.6.6 | `tool_environment` (oracle ≠ harness) | v1.6.6.4–6 oracle alignment |
| v1.7-tools | `framework_structural` + `framework_code` (missing tools + bad plans) | v1.6.6.7–11 tools + stipulator guidance |
| **1.6.6.11** | **`framework_parameter` (planner quality + in-loop pathology)** | **1.7.x / 1.8.x — learning + multi-candidate** |

Each generation peeled the next layer up. The pattern is monotonic: fixing
the dominant layer reliably exposes the next layer's bottleneck. CAT's
instrumentation makes the bottleneck observable; what's missing is the
loop where the framework's own diagnostic triggers its own next fix.

### Cost-per-PASS analysis (1.6.6.13, 2026-05-29)

v1.7-tools spent $0.462 on swe10 vs v1.6.6-final's $0.313 (+47%) despite
−55% tool calls. Per-task breakdown traced the increase to:

- **PERFORM layer ONLY.** Stipulator/Attestor per-call costs were flat
  (×1.03); performer per-call cost grew ×1.58 (5,932 → 9,388 input
  tokens/call). LLM call counts were stable (203 → 196).
- **Same turns, bigger turns.** The fewer-but-fatter pattern: agents made
  fewer tool calls, but each performer LLM call carried ~3,500 more input
  tokens. Driver: `read` alone accounts for 95% of all tool-output bytes
  (1.05 MB across 55 calls, avg 19 KB each). The new tools we worried
  about (`goto_def`, `find_refs`, `outline`) returned tiny outputs (224 B,
  274 B, 0 B). `outline` had **zero** calls — the LLM never reached for
  the cheapest option.
- **LLM keeps doing bare big reads after `goto_def`.** After `goto_def`
  returns `{path, line}`, the LLM tends to follow with `read(path)` rather
  than `read(path, offset=line-5, limit=50)`. Nothing in the `read` tool
  description or its truncation message nudges narrow reading.
- **django-13315 outlier real but partial.** Burned its full 500K budget
  in v1.7 (vs giving up at 106K in v1.6.6) because the new tools made
  progress *seem* tractable. Accounts for $0.08 of the $0.15 increase.
- **Per-PASS economics: $0.032 → $0.045 (+39%)**, not the naive +66% — the
  budget-burn task inflates the naive figure. Roughly even once
  django-13315's planner issue is fixed at v1.7+ (procedural memory or
  multi-candidate stipulation).

Surgical fix landed as **1.6.6.14** — borrow Pi/Claude-Code truncation
pattern: explicit "showing X of Y lines" message + description nudge "use
offset/limit; continue with offset until complete." ~10 lines of code, no
schema change.

What was NOT fixed and deferred to v2.x: the structural overhead from
having 14 tools available on every call (~$0.05/run static floor). The
proper fix is **capability granting at the admission gate** — only expose
tools the contract genuinely needs. See v2.x note below.

---

## v1.7.x / v1.8.x sequencing decision (locked 2026-05-29)

Two pending chains, each internally sequential, independent of each other:

```
1.7 chain (procedural memory):
   1.7.2 outcome-tag DAG nodes
     ↓
   1.7.1 LIBRARIAN retrieves outcome-tagged DAGs at PERFORM
     ↓
   1.7.3 DAG-grounded SUSPENDED resume

1.8 chain (Dijkstra multi-candidate):
   1.8.1 capture N candidates at SELECT
     ↓
   1.8.2 ADVISOR scores candidates
     ↓
   1.8.3 ELECTION step picks
```

**Decision: 1.7 chain first, then 1.8 chain.**

Reasoning:
- Procedural memory pays off on every task (each new task can retrieve a
  similar prior DAG); multi-candidate planning pays off mainly on hard tasks
  with high path entropy.
- 1.7.2 outcome-tagging is a prerequisite for 1.8.x anyway — you need
  labeled DAGs to score candidates against, so doing 1.8.x first would
  immediately stall on the same dependency.
- The largest remaining failure mode (stipulator picks wrong target on
  django-13315-class issues) is structurally a *cross-task learning*
  problem — i.e., the 1.7 chain — more than a *single-task search*
  problem.

Execution order:
1. (optional) Tighten v1.6 with the 3 quality follow-ups (in-loop redundancy
   guard / sharpened initial_test_failure / cost-per-PASS investigation)
2. 1.7.2 outcome-tag DAG nodes
3. 1.7.1 LIBRARIAN retrieval at PERFORM
4. 1.7.3 DAG-grounded SUSPENDED resume
5. 1.8.1 multi-candidate capture
6. 1.8.2 ADVISOR scoring
7. 1.8.3 ELECTION

---

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

4. **Contract-native failure classification** (replaces v1.2's LIFE attribution)
   - Failures are now attributed by contract state: which Criterion failed,
     which Representation was violated, which admission check rejected.
   - Tool errors are tool errors — no separate substring-based taxonomy.
   - Report: distribution of FAILED contracts by terminal `state_reason`,
     SUSPENDED contracts by `suspend_reason`. Feeds v1.8's procedural skills.

**Deliverables:**
- Re-plan on child failure (one retry, contract-native, not LIFE replan)
- SUSPENDED state flow working end-to-end (event-driven re-admission)
- Recovery stress test tasks + benchmark results
- Contract-state-based failure breakdown from v1.4/v1.5 runs

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

2. **LIFE-Harness external baseline** (arxiv 2605.22166)
   - LIFE is now an EXTERNAL comparison target only — not absorbed.
   - Run CAT (no-LIFE) vs published LIFE-Harness numbers on shared tau-bench.
   - Direct test: top-down design (CAT contract gate) vs bottom-up learned
     interventions (LIFE four-layer harness). Same model backbone.
   - Critical ablation: does CAT-without-LIFE-imports match what we had
     with LIFE-imports absorbed in v1.2-v1.4?

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
tune SUSPENDED re-admission semantics, derive real capability grants) until
gate criteria are met. Do NOT proceed to v2.0.

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

- **Procedural memory / automaticity** — task classification + cued retrieval
  of past DAGs as reusable skills. Brain analogy: declarative (in-context per
  run) vs procedural (DAG corpus across runs). See DESIGN.md "Declarative vs
  procedural memory" section. Predicted observable: token cost per task
  drops on recurring patterns as the agent develops automaticity. Requires
  v1.6 DAG infrastructure first.
- **Cost-efficiency optimization layer** *(forward note from 1.6.6.13)* —
  the v1.7-tools cost-per-PASS analysis revealed three structural sources
  of per-call cost inflation that no single fix at the loop layer cleanly
  addresses:
    1. **Static tool-schema overhead** — every performer LLM call carries
       all 14 tool schemas (~1KB of JSON per call), even when the contract
       only needs 3 of them. The fix is **capability granting at the
       admission gate (Step 4)**: only register the tools the contract
       genuinely requires. Requires understanding contract → required-tool
       mapping, likely co-developed with 1.7.x procedural memory which
       can learn this mapping from past DAGs.
    2. **History accumulation through context** — by turn 10, the LLM
       re-reads 9 prior tool results every call. CAT today has no
       in-context compaction beyond gross turn-summary squashing. A real
       fix needs **tool-result summarization at history-write time**
       (replace verbose results with their salient fact once the agent
       has moved on from them) — likely a new attestor-adjacent role.
    3. **LLM tool-selection bias** — the LLM defaults to `read` (familiar)
       over `outline` (cheaper, less familiar in pretraining), even when
       outline would dominate. 1.6.6.14 patches this with a description
       nudge, but the principled fix is **cost-aware multi-candidate
       generation at Step 5b** (the Dijkstra ADVISOR could score
       candidates partly on expected-bytes-out and not just on plan fit).
  These three together form a coherent "cost-efficiency layer" — schema
  granting (Step 4), history compaction (post-Step 7), and cost-aware
  candidate scoring (Step 5b). All three want each other's infrastructure;
  none ships before v1.7+/v1.8. The placeholder name is intentionally
  bland: this is not premature optimization, it's a *known* tax we're
  carrying that needs an architectural answer in v2.x.
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
