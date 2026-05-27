# Contract Activation Theory — Agentic Loop Design

## Premise

An agent is not a process that "runs." It is a **contract-fulfillment system**: a
host for latent capabilities that are activated only when a contract's conditions
are satisfied, and only to the minimum extent needed to discharge the contract.

Execution is the exception, not the default. The default state is dormancy.

---

## Core Vocabulary

| Term | Definition |
|---|---|
| **Contract** | A first-class object representing an obligation. Carries activation conditions, fulfillment criteria, constraints, and lifecycle state. |
| **Criterion** | A measurable predicate that, when true, contributes to fulfillment. A contract is fulfilled when its criteria conjunction holds. |
| **Condition** | A predicate over world/state that gates *activation* (distinct from fulfillment, which gates *closure*). |
| **Capability** | A latent action the agent can invoke (tool call, sub-contract spawn, computation). Not invoked unless a contract requires it. |
| **Activation** | The transient grant of execution rights to a capability on behalf of a contract. |
| **Cost** | Scalar (or vector) over resources: tokens, time, money, side-effects. Selection prefers minimum cost. |
| **Representation** | A verified statement of fact about the current state of the world, established before contract formation, upon which the contract's obligations are grounded. A contract formed on absent or false representations is formally incomplete: its obligations are contingent on conditions that were never established. |

---

## Contract Lifecycle

```
            received
               |
               v
         [ LATENT ] -----------+
               |               | conditions never met,
   conditions met              | TTL expired → EXPIRED
               |
               v
         [ ELIGIBLE ]
               |
   scheduler grants slot
               |
               v
         [ ACTIVE ] ---+--- criteria met ---> [ FULFILLED ] --> closed
               |       |
               |       +--- constraint violated ---> [ FAILED ]
               |       |
               |       +--- preempted / awaiting ---> [ SUSPENDED ]
               v                                          |
         (decompose into sub-contracts)                   |
                                                          v
                                              re-eligible when unblocked
```

A contract is never "running" between transitions. It holds state, waits, and
re-enters the evaluation pool when something changes.

---

## The Agentic Loop — 8 Steps

The loop is **not a thread**. It is a bounded evaluation cycle driven by:
new contracts, external events, timer ticks, or tool-call completion.

```
1. INGEST      receive or detect a contract
2. REPRESENT   establish verified facts (bounded probes, no LLM)
3. DECOMPOSE   planner emits ordered checklist grounded in representations
4. EVALUATE    check eligibility: conditions, budget, constraints
5. SELECT      pick minimum-cost fulfillment path (v1: always executor LLM)
6. ACTIVATE    grant execution to one capability; observe result
7. VERIFY      run criteria; update step checklist via judge
8. CLOSE       FULFILLED / re-enter EVALUATE / FAILED / SUSPENDED
```

Steps 4–8 repeat until a terminal state is reached.

---

### Step 1 — INGEST

**What:** Accept a contract from an external caller (user, parent contract, event).

**Formula:**
```
C = Contract(
    id        = uuid(),
    obligation = raw_task_text,
    criteria  = [],           # filled in DECOMPOSE or injected by caller
    conditions = [],
    constraints = Constraints(max_tokens, max_tool_calls, max_llm_calls, deadline),
    state     = LATENT,
    cost      = Cost(0, 0, 0, 0),
    representations = [],     # filled in REPRESENT
)
```

**Real example (scikit-learn-14141):**
```
obligation = "Add joblib in show_versions
              joblib should be added to the dependencies listed in show_versions
              or added to the issue template when sklearn version is > 0.20."
constraints = max_tool_calls=120, max_llm_calls=200, max_tokens=800_000, max_usd=$2.00
state       = LATENT
```

---

### Step 2 — REPRESENT

**What:** Run a fixed probe set against the environment. Results are
`(key, value, evidence)` triples — verified facts, not assumptions.
No LLM is called. Every probe is a deterministic tool call.

**Formula:**
```
R = []
for probe in PROBE_SET:
    result = bash(probe.command, timeout=10s)
    R.append(Representation(
        key      = probe.key,
        value    = extract_value(result),
        evidence = result.output[:500],
    ))

# Optional: SWE-bench injects oracle pre-run failure as R[initial_test_failure]
if criterion.is_oracle:
    passed, ev = criterion.predicate(state)
    if not passed:
        R.append(Representation(
            key      = "initial_test_failure",
            value    = "tests fail before any changes",
            evidence = state.kv["last_criterion_output"][-2500:],
        ))

contract.representations = pre_injected_reps + R
```

**Fixed probe set (all tasks):**

| key | command | purpose |
|---|---|---|
| `workdir` | `pwd` | anchor for relative paths |
| `ls_detail` | `ls -la` | directory structure |
| `file_types` | `file *` | detect binary/archives |
| `notes` | `head -20 README*` | task-specific hints |
| `test_scripts` | `find . -name setup.py -o -name pytest.ini` | test entry points |
| `test_command` | `cat run_tests.sh` | runner script if present |
| `git_state` | `git log --oneline -5` | repo context |
| `eval_command` | `cat /tests/run-tests.sh` | eval harness if present |
| `judge_strategy` | (derived) | `oracle: <cmd>` or `llm_judge` |
| `tool_strategy` | (derived) | which tools are available |

**Real values (scikit-learn-14141, 2026-05-26T17:13:06Z):**
```
workdir          = "/testbed"
ls_detail        = "total 168\ndrwxrwxrwx 1 root root 4096 May 26 17:12 .\n..."
file_types       = "CAT_EXIT:0"
notes            = ".. |Travis| image:: https://api.travis-ci.org/scikit-learn/scikit-learn.svg"
test_scripts     = "./sklearn/svm/setup.py"
test_command     = "(no run_tests.sh)"
git_state        = "32cc3ffaf SWE-bench\n3d997697f [MRG] Error for cosine affinity..."
eval_command     = "(no /tests/run-tests.sh)"
judge_strategy   = "oracle: python -m pytest --tb=short -q"
tool_strategy    = "bash(search/run) + str_replace(edit existing) + write(new files only)"
```

**Token cost of REPRESENT:** 0 LLM tokens (pure tool calls, ~8 bash invocations).

---

### Step 3 — DECOMPOSE

**What:** The only step where the agent uses judgment (LLM). Receives the
obligation and all representations; emits an ordered, verifiable checklist.

**Formula:**
```
steps = call_planner(
    llm,
    obligation,
    representations = contract.representations,
    # System prompt instructs: name specific files/functions,
    #   last step must be "run oracle and confirm exit 0"
)
# → list[{id: str, description: str}]

run.plan = [PlanStep(id, description, complete=False) for s in steps]
contract.cost.llm_calls += 1
```

**Planner system prompt directive (key constraint):**
> Each step must be verifiable by reading tool output alone.
> Name specific files, functions, or line numbers where known.
> The LAST step must be: run the oracle test command and confirm it exits 0.

**Real plan (scikit-learn-14141):**
```
s1: "Add 'joblib' to the _dependencies list in sklearn/utils/_show_versions.py."
s2: "Run the oracle test command 'python -m pytest --tb=short -q
     sklearn/utils/tests/test_show_versions.py::test_get_deps_info'
     and confirm it exits 0."
```
*LLM call: 1 (planner), tokens_in=2,191, tokens_out=680*

**Real plan (sphinx-10323, first decompose):**
```
s1: "Locate the LiteralIncludeReader class definition in
     sphinx/directives/code.py and find the method that applies
     dedent, prepend, and append."
s2: "Modify the LiteralIncludeReader.run() or _resolve_files() method
     so that dedent is applied only to the literalinclude content,
     NOT to prepend/append text."
s3: "Add a new test function test_LiteralIncludeReader_dedent_and_append_and_prepend
     in tests/test_directive_code.py that verifies prepend and append text
     are NOT stripped of leading whitespace when dedent is specified."
s4: "Run the oracle test command and confirm it exits 0."
```

---

### Step 4 — EVALUATE

**What:** Check budget and conditions before each iteration. Hard stop if
any constraint is violated.

**Formula:**
```
violation = constraints.check(cost, depth, now)
# violation is non-None if:
#   cost.tokens_total > max_tokens_total   → "tokens_total exceeded"
#   cost.tool_calls   > max_tool_calls     → "tool_calls exceeded"
#   cost.llm_calls    > max_llm_calls      → "llm_calls exceeded"
#   cost.usd          > max_usd            → "usd exceeded"
#   now               > deadline           → "deadline exceeded"
#   depth             > max_depth          → "depth exceeded"

if violation:
    contract.state = FAILED
    run.failure_reason = violation
    return

if not contract.conditions_met(state):
    contract.state = SUSPENDED
    return
```

**Real budget (SWE-bench runs):**
```
max_iterations    = 60
max_criterion_fails = 20   (F4 threshold = 10)
max_tool_calls    = 120
max_llm_calls     = 200
max_tokens_total  = 800,000
max_usd           = $2.00
```

---

### Step 5 — SELECT

**What:** Choose the fulfillment path. v1 is fixed: always executor LLM with
the registered tool suite. No learned policy.

**Formula:**
```
path = ExecutorLLM(
    model       = contract.llm_model,
    tools       = registry.tool_schemas(),
    temperature = 0.2,         # low variance; one action per turn
    tool_choice = "auto",
)
```

v2 will generalize this to a minimum-cost selector over multiple candidate paths.

---

### Step 6 — ACTIVATE

**What:** One executor LLM call → one tool call → one tool result. Strictly
one tool per turn (discipline rule). The result is appended to the message
thread so the next turn sees it.

**Formula:**
```
# Build context window
messages = [
    {role: system,    content: EXECUTOR_SYSTEM_PROMPT},
    {role: user,      content: build_user_prompt(obligation, plan_checklist, budget)},
    ...run.messages[-window_size*2:]  # last window_size turns (action + result pairs)
]

# One LLM call
result = llm.chat(messages, tools, temperature=0.2, role="executor")
contract.cost += result.tokens

# Realize action (normalize paths, strip markdown fences)
args = _realize_action(result.tool_calls[0])

# Execute capability
tool_result = registry.invoke(name, args, state)
contract.cost.tool_calls += 1

# Record attribution if error
if tool_result.is_error:
    layer = _attribute_tool_failure(tool_result)
    # layer ∈ {action_realization, environment_contract, residual_reasoning}

# Append to message thread
run.messages.append({role: assistant, tool_calls: [...]})
run.messages.append({role: tool, content: tool_result.output})
```

**Action Realization (LIFE-Harness Layer):**
The `_realize_action` function normalizes args before dispatch:
- Strip leading `./` and `\` from paths (common model mistake)
- Strip markdown fences from `write` content
- Cast `timeout_s` string → float

**Failure Attribution taxonomy:**

| Layer | Trigger | Example |
|---|---|---|
| `action_realization` | "invalid", "unknown tool", "json", "type" | wrong argument type |
| `environment_contract` | "not found", "permission", "timeout", "escapes workdir" | file missing, path error |
| `residual_reasoning` | anything else | model chose wrong approach |

**Real activation sequence (scikit-learn-14141, iterations 1–3):**
```
iter 1:
  CALL  read  path="sklearn/utils/_show_versions.py"
  OUT   "import platform\nimport sys\nimport importlib\n\ndef _get_sys_info():..."
  JUDGE s1=False  "file content shown does not contain a '_dependencies' list"

iter 2:
  CALL  bash  "grep -n '_dependencies' sklearn/utils/_show_versions.py"
  OUT   "CAT_EXIT:1\n(testbed) root@2ddbc31cb55a:/testbed#"   ← exit code 1
  JUDGE s1=False  "grep for '_dependencies' failed (exit code 1)"

iter 3:
  CALL  bash  "grep -rn 'dependencies' sklearn/utils/_show_versions.py"
  OUT   "34:    \"\"\"Overview of the installed version of main dependencies"
  JUDGE s1=False  (no further judge check this turn)
```

**Executor user prompt structure (actual):**
```
OBLIGATION:
Add joblib in show_versions
joblib should be added to the dependencies listed in show_versions...

PLAN (checklist — mark via judge, not yourself):
  [ ] s1: Add 'joblib' to the _dependencies list in sklearn/utils/_show_versions.py.
  [ ] s2: Run the oracle test command ... and confirm it exits 0.

BUDGET:
  tokens: 8432/800000  tool_calls: 3/120  llm_calls: 5/200  usd: $0.0012/$2.00

Begin. Take ONE action toward the first un-checked step.
```

---

### Step 7 — VERIFY

**What:** Two independent checks run after every activation:

**(A) Oracle criterion** — deterministic, authoritative:
```
if criterion.is_oracle:
    passed, evidence = criterion.predicate(state)
    # predicate runs: bash(test_command); checks exit_code == 0
    state.kv["last_criterion_output"] = evidence.detail["output"]
    if passed:
        run.criterion_fail_streak = 0
        contract.state = FULFILLED
    else:
        run.criterion_fail_streak += 1
        run.criterion_history.append((run.iterations, evidence.summary[:200]))
```

**(B) Judge criterion** — advisory per step:
```
for step in run.plan:
    if step.complete: continue
    summary = last_N_messages_as_text(run.messages, N=8)
    complete, reason = call_judge(llm, step.description, summary)
    step.complete = complete
    step.last_reason = reason
    break   # only judge first un-checked step per turn (token budget discipline)
```

**Oracle is authoritative — judge is advisory.** When any oracle criterion is
present, LLM-judge criteria are skipped entirely at close time. The contract
closes on real test results, not model opinion.

**Real oracle check (scikit-learn-14141, iteration 11):**
```
criterion_id  = "test-runner"
command       = "cd /testbed && echo '...' | base64 -d | git apply --ignore-whitespace -;
                 PYTHONPATH=/testbed pytest sklearn/utils/tests/test_show_versions.py
                 ::test_get_deps_info -x -q --tb=short"
satisfied     = true
evidence      = "test exit_code=0 PASS"
iteration     = 11
```

**Real judge check (sphinx-10323, iteration 8):**
```
step  = s1: "Locate the LiteralIncludeReader class definition..."
output = "Found LiteralIncludeReader class (__init__ at line 189) and its
          methods: prepend_filter (line 343), append_filter (line 350),
          dedent_filter (line 357)"
complete = True
reason   = "Found to_rotation_matrix at line 172."
```

---

### Step 8 — CLOSE

**What:** Determine the terminal transition for this iteration. Three outcomes:

```
if criteria_satisfied:
    contract.state = FULFILLED
    return

elif criterion_fail_streak >= replan_threshold:      # F2 escape
    _do_replan(contract, run, state)
    run.criterion_fail_streak = 0
    # re-enter loop with fresh plan

elif replan_count >= 1 and not free_form:            # F3 escape
    _enter_free_form(contract, run)
    run.criterion_fail_streak = 0
    # re-enter loop, no plan structure

elif free_form and criterion_fail_streak still high:  # all escapes exhausted
    contract.state = FAILED
    run.failure_reason = "criterion_unreachable"
    return

else:
    # continue — re-enter Step 4
```

`replan_threshold = max(1, max_criterion_fails // 2)` = 10 for SWE-bench runs.

---

## Residual Information Flow Between Loops

This is the memory the loop carries from one iteration to the next and across
replans. Understanding it is essential to understanding why replanning works.

### Per-Iteration Residuals (always carried forward)

```
ActivatorRun {
    plan:              list[PlanStep]          # step checklist with complete flags
    messages:          list[dict]              # sliding window of LLM messages
    iterations:        int                     # total tool calls so far
    criterion_fail_streak: int                 # consecutive oracle failures
    replan_count:      int                     # 0=initial, 1=replanned, 2=free-form
    prior_attempts:    list[str]               # compressed log of past turns
    criterion_history: list[(iter, evidence)]  # oracle failure reasons
    free_form:         bool                    # F3 flag: plan structure dropped
    consecutive_nonwrite: int                  # W1: detects read-loop stalls
}
```

**Message window (P1):**
The executor sees only the last `window_size=4` turns. Older turns are
compressed into `prior_attempts` — one line per turn:
```
format: "t{turn:02d} {tool}({arg_preview}) → {OK|ERR}: {first_line_of_output}"
example: "t06 bash('grep -n def  /testbed/sphinx/directives/code.py | head -30')
          → OK: 40:    def run(self) -> List[Node]:"
```

This compression is **deterministic** — no LLM. The full LLM context never
grows unboundedly; old context degrades to a one-liner summary.

### Cross-Replan Residuals (injected into new plan context)

When F2 triggers a replan, these four residuals are injected as new
Representations passed to the planner:

```
1. prior_attempts      ← compressed tool log (last 15 entries)
   key:   "prior_attempts"
   value: "previous approach failed — generate a different plan"
   evidence:
     CRITERION FEEDBACK (why the goal was not yet satisfied):
       iter 03: FAIL: oracle (cd /testbed && ... pytest ...) exit_code=1
       iter 06: FAIL: oracle ... (same verdict repeated 3x)
       ...
     WHAT WAS TRIED (tool call log):
       t01 bash('grep -n ...') → OK: 172:class LiteralIncludeReader
       t02 read('testbed/sphinx/...') → ERR: No such file or directory
       t03 str_replace(path='testbed/sphinx/...') → ERR: file not found
       ...

2. latest_test_failure ← oracle output after most recent changes
   key:   "latest_test_failure"
   value: "oracle output after most recent changes (still failing)"
   evidence:
     LATEST TEST FAILURE (after your changes so far):
     Use this to understand what still needs to be fixed.

     FAILED tests/test_directive_code.py::test_LiteralIncludeReader_dedent
     AssertionError: assert '    <plugin>\n' == '<plugin>\n'
     E  + '    <plugin>\n'   ← prepend still being dedented

3. current_diff        ← git diff HEAD (what was changed so far)
   key:   "current_diff"
   value: "files changed so far (git diff HEAD)"
   evidence:
     Changes made so far (may be incomplete or incorrect):
     diff --git a/sphinx/directives/code.py b/sphinx/directives/code.py
     @@ -220,7 +220,7 @@ class LiteralIncludeReader:
     -    lines = self.prepend_filter(lines) + self.dedent_filter(lines) + ...
     +    lines = self.prepend_filter(lines) + lines + ...   ← wrong fix

4. initial_test_failure ← oracle pre-run (from REPRESENT, unchanged)
   still present from Step 2; shows what was failing BEFORE any changes
```

**What the planner sees at replan time:**
The full set of representations from REPRESENT (workdir, git_state, etc.)
**plus** all four cross-replan residuals above. It has:
- The original environment facts
- Every prior criterion failure reason
- The most recent test error output (after partial changes)
- A diff of exactly what was already edited (to not redo it)

**Real replan trigger (sphinx-10323):**
```
criterion_fail_streak = 10   (threshold = 10)
replan_count          = 0 → becomes 1

prior_attempts (last 5 of 31 entries):
  t27 str_replace('testbed/sphinx/directives/code.py') → ERR: file not found
  t28 bash('ls /testbed/sphinx/directives/') → OK: code.py  ← path confirmed
  t29 str_replace('testbed/sphinx/directives/code.py') → ERR: file not found
  t30 bash('sed -n '220,235p' /testbed/sphinx/directives/code.py') → OK: filter list
  t31 str_replace('testbed/sphinx/directives/code.py') → ERR: file not found

criterion_history (last 3 of many):
  iter 28: FAIL: oracle exit_code=1 (test not found — test file not written yet)
  iter 30: FAIL: oracle exit_code=1 (same)
  iter 31: FAIL: oracle exit_code=1 (same)

→ Planner generates simpler 3-step plan:
  s1: "Read the relevant section of /testbed/sphinx/directives/code.py
       around lines 220-240 to see how filters are applied."
  s2: "Modify the code so dedent_filter is applied only to included lines,
       not to prepend and append strings."
  s3: "Run the oracle test command and confirm it exits 0."
```

---

## Full Loop Budget (Real Numbers)

### scikit-learn-14141 — clean solve, no replan

```
Step                   LLM calls   Tokens in    Tokens out   Notes
────────────────────────────────────────────────────────────────────
REPRESENT (step 2)         0            0            0       pure tool calls
DECOMPOSE (step 3)         1         2,191          680      planner
ACTIVATE×12 (step 6)      12        31,647        1,439      executor
VERIFY judge×11 (step 7)  11         5,559        2,819      advisory judges
────────────────────────────────────────────────────────────────────
TOTAL                      24        39,397        4,938     44.3K tokens, $0.007
Wall time: ~2 minutes | Tool calls: 20 | Criterion checks: 1 (pass) + 11 judge
```

### sphinx-10323 — replan after 31 failed iterations

```
Step                   LLM calls   Tokens in    Tokens out   Notes
────────────────────────────────────────────────────────────────────
REPRESENT (step 2)         0            0            0
DECOMPOSE×2 (step 3)       2        ~4,500       ~2,300      2nd = replan
ACTIVATE×36 (step 6)      36       165,740        7,414      executor
VERIFY judge×35 (step 7)  35        15,288        9,604
────────────────────────────────────────────────────────────────────
TOTAL                      73       185,528       19,318     204.8K tokens, $0.037
Wall time: ~8 minutes | Tool calls: 46 | Iterations: 36
```

### React baseline (same 30 tasks)
```
Tokens per solved task: 354K  (vs CAT v14: 92K — 3.8× cheaper)
No REPRESENT, no plan checklist, no oracle-before-planning.
```

---

## Escape Hatches

Three escalating responses to criterion deadlock:

```
F2 — Dynamic replan (first deadlock)
  Trigger:  criterion_fail_streak >= max_criterion_fails // 2
  Action:   call planner again with prior_attempts + criterion_history
            + latest_test_failure + current_diff injected as representations
  Effect:   new plan replaces old; criterion_fail_streak = 0
  Cost:     1 LLM planner call + 1 bash (git diff)

F3 — Free-form (second deadlock)
  Trigger:  replan_count >= 1 and criterion_fail_streak >= threshold again
  Action:   drop plan structure; executor runs free (like ReAct)
  Effect:   no per-step judge overhead; criterion gate still active
  Cost:     no extra LLM call; just restructured prompt

F4 — Give up (third deadlock)
  Trigger:  free_form and criterion_fail_streak >= threshold again
  Action:   contract.state = FAILED, reason = "criterion_unreachable"
  Cost:     none
```

---

## Contract Schema (concrete)

```python
@dataclass
class Contract:
    id: str
    obligation: str                          # human-readable promise
    criteria: list[Criterion]                # AND'd; all must hold for fulfillment
    conditions: list[Condition]              # AND'd; all must hold for activation
    constraints: Constraints                 # budgets: tokens, time, money, depth
    capabilities: list[CapabilityRef]        # what *may* be activated
    parent: ContractId | None
    children: list[ContractId]
    state: Latent | Eligible | Active | Suspended | Fulfilled | Failed | Expired
    cost_so_far: Cost
    priority: int
    created_at: Timestamp
    deadline: Timestamp | None
    representations: list[Representation]   # facts established before decomposition

@dataclass
class Representation:
    key: str        # "workdir", "git_state", "initial_test_failure", ...
    value: str      # short verified claim: "/testbed", "clean", "tests fail"
    evidence: str   # raw probe output — the warrant for the claim

@dataclass
class ActivatorRun:          # mutable loop state, not persisted in Contract
    plan:                list[PlanStep]
    messages:            list[dict]          # sliding window (last window_size turns)
    iterations:          int
    criterion_fail_streak: int
    replan_count:        int                 # 0=initial, 1=replanned, 2=free-form
    free_form:           bool
    prior_attempts:      list[str]           # compressed one-liner per past turn
    criterion_history:   list[(int, str)]    # (iteration, evidence) per oracle FAIL
    consecutive_nonwrite: int                # W1: stall detection
```

---

## Why This Is Different from a Normal Agent Loop

| Normal agent loop | Contract-activation loop |
|---|---|
| `while True: think(); act(); observe()` | Idle until a driver fires |
| Tool calls dispatched ad hoc | Tool calls are activations on a contract |
| "Done" decided by the model | Done = oracle predicate returns True |
| Failure = exception or silent hallucination | Failure = explicit contract state with reason |
| Sub-tasks are stack frames | Sub-tasks are contracts with their own lifecycles |
| Budget is loose (max_tokens) | Budget is a hard constraint on the contract |
| Obligation acted on as-received | Obligation grounded in verified representations before decomposition |
| Context grows unboundedly | Context window = sliding window + compressed prior_attempts |
| No attribution when things go wrong | Every tool error is attributed to a layer: action_realization / environment_contract / residual_reasoning |

---

## Failure Attribution (LIFE-Harness Taxonomy)

Every `is_error=True` tool result is classified by priority order:

```
1. environment_contract  — precondition_failed flag set by capability
                         — OR output contains: "not found", "permission",
                           "escapes workdir", "timeout"
   Meaning: the environment did not meet the contract's assumed preconditions.
   Example: "cat: testbed/sphinx/directives/code.py: No such file or directory"
            → agent used relative path; file exists at /testbed/...

2. action_realization    — output contains: "missing", "unknown tool",
                           "invalid", "json", "type"
   Meaning: the agent's action specification was malformed.
   Example: wrong argument type, unrecognized tool name

3. residual_reasoning    — all other errors
   Meaning: the action was well-formed and the environment was valid,
            but the approach was wrong.
   Example: edited the wrong function; chose wrong fix strategy
```

**Real distribution (sphinx-10323, 46 tool calls, ~18 failures):**
```
environment_contract: ~14  (all "file not found" on relative path)
action_realization:    ~1  (grep tool not available, used bash instead)
residual_reasoning:    ~3  (str_replace applied wrong change)
```

---

## Open Design Questions (v2)

- **Event sourcing**: every state transition emits a replayable event? Strong yes — partial implementation exists in `trace.py`.
- **Speculative activation**: pre-activate capabilities while ELIGIBLE? Deferred.
- **Contract markets**: auction protocol for multi-agent bid on a contract? Out of scope v1.
- **Failure propagation**: child's FAILED state → parent replans once, then fails. Currently ad-hoc in `_do_replan`; needs formal sub-contract architecture.
- **SUSPENDED state**: currently only checked, never entered in practice. Needs full flow for rate-limit / long-running external dependencies.
- **pass^k consistency**: same contract run k times; measure variance. Currently 1 attempt per task.
