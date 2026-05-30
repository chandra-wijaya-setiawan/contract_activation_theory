# Manual Dijkstra Mapping — django-12039 walkthrough

**Purpose.** Before writing any code for v1.7 Phase A, manually map a real
agent trace into Dijkstra's variables. This forces us to confront the
state-formalization question with actual data, not abstract design.

**Source.** `data/runs/swebench/2026-05-28/16-28-49-swe10-v1.7-tools/django__django-12039/`. Final outcome: **PASS** (52 iterations, 445K tokens). Selected because: ≥10 iterations, varied tool paths, clearly visible struggle-then-recover pattern.

**Status.** Draft for review. Open questions flagged inline.

---

## 1. Mapping table — trace concepts → Dijkstra variables

Strict Dijkstra requires: `G = (V, E, w)` with a source `s`, target `T`,
non-negative edge weights `w: E → ℝ⁺`. The agent loop must map onto this.

| Trace concept | Dijkstra variable | Notes |
|---|---|---|
| Contract obligation text | identifies the **source** for this run | one task = one source. Source state = `state₀` |
| Contract state machine `fulfilled` | **GOAL** virtual sink node | every PASS trajectory's last state adds a 0-weight edge to GOAL |
| Contract state machine `failed`/`expired` | NON-GOAL terminal (no outgoing edges in corpus, except backwards-from-corpus links from other trajectories that passed through this state) |
| Single iteration (one `capability_perform` + its `tool_call`+`tool_result`+`criterion_check` chain) | one **edge** `(s_i, a_i, s_{i+1})` | the unit of state transition |
| `tool_call.tool` + canonicalized `tool_call.args` | `a_i` — the **action_class** | hashed for graph keys |
| Pre-iteration context (plan progress + workdir + last outcome) | `s_i` — the **state_class** | hashed for graph keys |
| `llm_usage.in + llm_usage.out` for the performer LLM call in this iter + tool wall_s converted to tokens-equivalent | `w(s_i, a_i)` — the **edge weight** | scalar; could go multi-objective later |
| `criterion_check.ok=True` at the iteration | not a graph concept; it's evidence FOR labeling the subtree outcome |
| `contract_state` → `fulfilled` | the trajectory's terminal state gets a 0-cost edge to GOAL |
| `contract_state` → `failed` | trajectory ends without GOAL edge; intermediate states it visited are still nodes in V |

What's NOT a graph concept:
- LLM reasoning text — only its cost (tokens) matters
- The tool's full output bytes — used to derive `next_state_class` but not stored as a graph attribute
- Multiple LLM calls within one iteration (performer + attestor) — both contribute to that iter's edge weight; not separate edges

---

## 2. State and action canonicalization — candidate recipe

These are the **only** decisions that matter for Dijkstra to work. Everything
else is mechanical.

### Candidate `state_class` recipe (v0 — coarsest)

```python
state_class = sha256(json.dumps({
    "obligation_hash": sha256(obligation_text)[:8],     # identifies this task
    "plan_step_idx":   plan_step_current,                # 0-indexed into the plan
    "last_outcome":    last_iter_outcome,                # "ok" | "error" | None
    "workdir_signature": file_set_hash,                  # see below
}, sort_keys=True))[:12]
```

Where `file_set_hash = sha256(sorted([p for p in ls -R /testbed]))` —
extremely coarse, only changes when files are created/deleted, not when
modified. For SWE-bench: should change after every successful `str_replace`
because file content changes implicitly? Actually NO — `ls` only lists names.
For content sensitivity we'd hash modification timestamps too.

**Open question 1:** Does `workdir_signature` include file *contents* or just
file *names*? Names-only means an unedited workdir and a half-edited workdir
have the same state_class — that's probably wrong (the agent's next action
should differ depending on whether it has already edited the target file).
Contents-only means every edit creates a new state_class, possibly making
collisions impossible across runs.

Likely answer: hash names + modification times of files inside the target
codebase, not full content. Gives some sensitivity to "I have edited X"
without exploding the state space.

### Candidate `action_class` recipe (v0)

```python
action_class = sha256(json.dumps({
    "tool":  tool_name,
    "args":  canonical_args(tool_name, args),
}, sort_keys=True))[:12]
```

Where `canonical_args` strips path randomness, buckets numeric values:

```python
def canonical_args(tool, args):
    a = dict(args)
    # Strip /testbed/ prefix from path-like args
    for k in ("path", "paths", "root", "workdir"):
        if k in a and isinstance(a[k], str):
            a[k] = a[k].replace("/testbed/", "<repo>/")
    # Bucket numeric args
    for k in ("offset", "limit", "context"):
        if k in a and isinstance(a[k], (int, float)):
            v = int(a[k])
            a[k] = "small" if v < 10 else "medium" if v < 100 else "large"
    return a
```

**Open question 2:** Should `pattern` for `rg` be hashed verbatim, normalized,
or LLM-summarized? Verbatim → too many distinct action_classes. Normalized
(e.g., strip regex syntax, lowercase) → may conflate distinct intents.
LLM-summarized → defeats the "no LLM in scoring" rule.

Likely answer: verbatim for now. Revisit if pattern variety bloats the action
space without helping signal.

---

## 3. Iteration-by-iteration walkthrough — django-12039

The task: "Use proper whitespace in CREATE INDEX statements." Fix is in
`django/db/backends/ddl_references.py:Columns.__init__` — filter empty
strings from `col_suffixes`.

Plan (from contract_stipulate event):
- **s1**: Edit `django/db/backends/ddl_references.py` to filter out empty
  strings from `col_suffixes` in the `Columns` class `__init__` method.
- **s2**: Run `python -m pytest --tb=short -q` and confirm exit 0.

For each iteration I show: pre-state, action, observed result, edge weight,
post-state.

### Notation

- `S(i)` = state_class at start of iteration `i`. I'll write a human-readable
  summary; in reality this is a 12-char hash.
- `A(i)` = action_class taken in iteration `i`.
- `W(i)` = edge weight = performer LLM tokens-in + attestor LLM tokens-in.
- `→` = transitions to.

### Iter 1
- **S(1)**: `plan_idx=0, last_outcome=None, workdir=clean`
  ```
  s_1 = h(task_django-12039, plan_step=0, last=None, workdir=clean)
        = "a1b2c3d4..."  (illustrative)
  ```
- **A(1)**: `read(path=<repo>/django/db/backends/ddl_references.py)`
  ```
  a_1 = h(read, path=<repo>/django/db/backends/ddl_references.py, offset=None, limit=None)
        = "f1e2d3c4..."
  ```
- **Observation**: tool returned 6.5KB, no error, file content visible
- **W(1)** = 531 (attestor in) + 4745 (performer in) = **5,276 tokens**
- **S(2)**: `plan_idx=0, last_outcome=ok, workdir=read-ddl-refs`
  - **Question:** Should "I have now seen this file" be reflected in
    state_class? If yes, S(1) ≠ S(2). If not, S(2) might equal S(1) — but
    then iter 2 would face the same state and pick the same action, looping.

**Open question 3:** State must reflect *what the agent has learned*, not
just *what files exist*. Otherwise the graph has cycles where the same state
keeps emitting the same action. Three options:

a. Include "set of recently-read files" in state_class — clean but state
   space explodes
b. Encode "last K actions" in state_class — captures recency, doesn't
   explode as fast
c. Use "plan_progress" only — let the LLM handle short-term memory
   internally; state only changes when something durable changes (file
   edited, test passed)

Option (c) is the cleanest. It means S(1) = S(2) = ... until iter 4's
`str_replace` actually changes the file. The corpus would learn "from this
state, the cheapest action that leads anywhere new is `str_replace`."

I'll use option (c) for the rest of the walkthrough.

### Iter 1 (corrected with option c)
- **S(1)**: `plan_idx=0, last_outcome=None, workdir=clean`
- **A(1)**: `read(<repo>/django/db/backends/ddl_references.py)`
- **W(1)** = 5,276 tokens
- **S(2)**: **same as S(1)** under option (c) — no durable state change yet

### Iter 2
- **S(2)**: same as S(1)
- **A(2)**: `find_refs(col_suffixes)`
- **W(2)** = 284 + 5154 = **5,438**
- Observation: empty (warning about pip; jedi may not have been installed).
- **S(3)**: same as S(2). Action produced nothing usable.

### Iter 3
- **S(3)**: same
- **A(3)**: `rg(col_suffixes, paths=/testbed)`
- **W(3)** = 658 + 7708 = **8,366**
- Observation: 7.8KB of matches across multiple files.
- **S(4)**: same. (Information was gathered into LLM context but no durable
  state change.)

### Iter 4 — **first state change**
- **S(4)**: same as S(1)–S(3)
- **A(4)**: `str_replace(<repo>/django/db/backends/ddl_references.py)`
- **W(4)** = 339 + 8150 = **8,489**
- Observation: "OK: replaced 1 occurrence."
- **S(5)**: `plan_idx=0, last_outcome=ok, workdir={ddl_references.py modified}` ← **NEW STATE_CLASS**

Now the graph has an actual edge:
```
S(1) --A(4) str_replace ddl_refs, W=8489--> S(5)
```

But the attestor's check (`criterion_check`) said `ok=None` ("the replacement
string appears truncated; no filtering of empty strings was added"). So
**S(5)** is "edit attempted but plan step still incomplete." Plan_idx
remains 0.

### Iter 5
- **S(5)**: edit applied, plan_idx still 0
- **A(5)**: `read(<repo>/django/db/backends/ddl_references.py, offset=76, limit=10)`
- **W(5)** = 543 + 8263 = **8,806**
- **S(6)**: same as S(5). The narrow read didn't include the right region.

### Iter 6 — environment failure
- **S(6)**: same
- **A(6)**: `bash(cd /testbed && python -m pytest --tb=short -q)`
- **W(6)** = 242 + 8182 = **8,424**
- Observation: `is_error=True`. "No module named pytest."
- **S(7)**: `plan_idx=0, last_outcome=error, workdir=ddl_modified, env=no_pytest` ← **another new state**

Plan-step still 0 because s2 (run pytest) needs s1 to be attested complete.

### Iter 7 — env fix
- **S(7)**: env=no_pytest
- **A(7)**: `bash(pip install pytest)`
- **W(7)** = 295 + 5961 = **6,256**
- Observation: success.
- **S(8)**: `plan_idx=0, last_outcome=ok, workdir=ddl_modified, env=pytest_ok` ← new state

### Iter 8
- **S(8)**: env=pytest_ok
- **A(8)**: `rg(tuple(s for s in col_suffixes if s), <repo>/django/db/backends/ddl_references.py)` ← agent verifies its own edit
- **W(8)** = 236 + 5821 = **6,057**
- Observation: 0 matches! The edit didn't actually do what the agent intended.
- **S(9)**: same as S(8). Information gathered: edit was wrong.

### Iter 9
- **S(9)**: same
- **A(9)**: `read(<repo>/django/db/backends/ddl_references.py, offset=78, limit=8)`
- **W(9)** = 563 + 5853 = **6,416**
- **S(10)**: same.

### Iter 10 — first test run
- **S(10)**: same
- **A(10)**: `bash(cd /testbed && python -m pytest --tb=short -q 2>&1)`
- **W(10)** = 605 + 8012 = **8,617**
- Observation: `is_error=True`. "ERRORS collecting tests" (Django app registry not ready).
- **S(11)**: `plan_idx=0, last_outcome=error_test_collection, ...`

### Iters 11-14 — environment / test discovery
- Various exploration: find test_sqlite.py, read it, try `DJANGO_SETTINGS_MODULE=test_sqlite python -m pytest`, look for config files. State stays roughly "still trying to figure out how to run tests."

### Iter 15
- Agent re-reads `ddl_references.py` from the top — basically refocusing on s1 after the test-discovery diversion.

### Continuing past 15 (summary)
- Iter 16: second `str_replace` — the actual fix
- Iter 17–30+: continued struggle with how to run Django's test suite
- Iter 31: finally finds `tests/runtests.py schema` — the right runner
- Iter 37+: more debugging
- Iter 52: PASS — the harness's oracle accepts the fix

The trajectory's terminal state — *after* `str_replace` at iter 16 was
correct and the tests eventually passed — connects to GOAL with weight 0.

---

## 4. The graph that emerges from this single trace

Listing only state-CHANGING transitions (action that altered durable state):

```
S(initial: plan_idx=0, env=ok, workdir=clean)
   │
   │  W=8489  A=str_replace(ddl_references.py)   [iter 4]
   ▼
S(plan_idx=0, edit_attempt_1, env=ok)
   │
   │  W=8424  A=bash(pytest)                     [iter 6]   ENV FAIL
   ▼
S(plan_idx=0, edit_attempt_1, env=no_pytest)
   │
   │  W=6256  A=bash(pip install pytest)         [iter 7]
   ▼
S(plan_idx=0, edit_attempt_1, env=ok_again)
   │
   │  W=8617  A=bash(pytest)                     [iter 10]  TEST COLLECT FAIL
   ▼
S(plan_idx=0, edit_attempt_1, env=ok, last=collect_error)
   │
   │  ~~~~~ many iterations of trial and error ~~~~~
   │  W=~150K  total
   ▼
S(plan_idx=0, edit_attempt_2_correct, env=ok, runtests_found)   [iter 16+]
   │
   │  W=~5K   A=bash(python tests/runtests.py schema)   [iter 31]
   ▼
S(plan_idx=1, edit_correct, tests_pass)
   │
   │  W=0   (trajectory terminates with PASS)
   ▼
GOAL
```

**Total edge weight from S(initial) to GOAL on this path: ~445K tokens.**

If a future task starts at the same state_class as `S(initial)`, what should
Dijkstra recommend?

If this is the ONLY corpus trajectory, Dijkstra has no choice but to
recommend `str_replace(ddl_references.py)` as the first action — it's the
only known edge leaving `S(initial)`.

But the *cost* of this path is 445K tokens. The corpus is recording: "going
through state X, the cheapest known path costs 445K tokens." That's a poor
guide. Future runs would need at least one PASS trajectory at lower cost to
have a "shortest" path worth following.

**This is the key observation: Dijkstra doesn't tell you "is this a good
path?" — it just tells you "is this the cheapest known path?" The corpus
quality determines what's "cheap." If the only path is expensive, Dijkstra
will faithfully replicate the expense.**

This is fine in steady state but means **the first ~10 trajectories per
state_class teach the corpus what's possible; only after ~10+ does Dijkstra
start delivering optimization.**

---

## 5. What Dijkstra would have done — counterfactual analysis

Imagine the corpus already contained a *different* prior trajectory that
solved this same task in 100K tokens via the path:

```
S(initial) → A=goto_def(Columns) → S' → A=read(ddl_refs, offset=X) → S'' → A=str_replace [correct] → S''' → A=bash(runtests.py schema) → GOAL
```

If both trajectories are in the corpus, Dijkstra from `S(initial)` would
pick whichever first-edge leads to the lower total-cost path: probably the
`goto_def`-first path (100K total) over the `read`-first path (445K total).

But **for that recommendation to be possible, both trajectories must exist
in the corpus AND their state_classes after the first action must connect
to known cheap subpaths.**

The cold-start problem: with one trajectory, Dijkstra is just "do what the
prior agent did, including all its mistakes."

The interesting question is whether we can detect, within a single
trajectory, that two state_classes are actually the same state and the agent
wasted iterations re-discovering. E.g., iter 9's state is "essentially the
same" as iter 5's — both are "edit attempted, agent uncertain whether it
worked, re-reading file." If state_class hashing captured this equivalence,
the corpus would learn that `read(ddl_refs)` is a *cycle-creating* action
from that state — Dijkstra would route around it.

This is **state_class canonicalization as an optimization on its own**, not
just a corpus lookup key.

---

## 6. Open canonicalization questions surfaced by this walkthrough

| # | Question | Provisional answer |
|---|---|---|
| 1 | What constitutes a state-changing action? | str_replace, multi_edit, apply_patch (file mods), bash with side effects (install, env var, file create), criterion_check with ok=True (plan progress). Everything else is "information gathering" and doesn't change state_class. |
| 2 | How to detect bash side effects? | Hard. Conservative: treat every `bash` as state-changing if it ran without error. Liberal: parse the command for known patterns (pip install, rm, cp, mkdir, redirect). Liberal probably right. |
| 3 | Should plan_step_idx be part of state, or independent dimension? | Part of state. The same workdir at plan_step=0 vs plan_step=1 should be different states (different actions are appropriate). |
| 4 | Should obligation_hash be part of state? | Yes — otherwise states from totally different tasks would collide. This means cross-task transfer requires SIMILAR-obligation matching, not exact-hash matching. (LIBRARIAN's tie-breaking job.) |
| 5 | How to encode env state (pytest installed, etc.)? | Track a small canonical fact set: `{pytest_installed, runtests_found, last_test_error_class, …}`. Hand-curated initially, learned later. |
| 6 | How fine-grained should action_class be? | Tool name + canonical args. `read(X)` and `read(X, offset=10, limit=50)` should be different action_classes — they have different costs and different post-state implications. |
| 7 | What about identical (state, action) pairs in the same trajectory (cycles)? | The graph naturally records them as a single edge with averaged weight. The fact of cycle-in-trajectory is a **dead-end signal** for outcome-tagging, separately. |

---

## 7. What this walkthrough proves / disproves

### Proves
- The mapping is well-defined: every trace event becomes either an edge, a
  state-change observation, or noise (logging).
- Edge weights are derivable from the trace (LLM token costs).
- GOAL connection is unambiguous (oracle says PASS → terminal state of
  trajectory gets 0-edge to GOAL).
- The mapping naturally surfaces the "did the workdir actually change"
  question that the v1.6 cost analysis identified as load-bearing.

### Disproves (or at least challenges)
- **The "naive state hash" approach (full workdir snapshot) won't work** —
  every iteration would have a unique state_class, no collisions, no
  learning. The state must abstract away from raw file contents.
- **A SINGLE trajectory provides almost no Dijkstra value** — it gives one
  known path, and Dijkstra just replicates it. The interesting learning
  happens when 3+ trajectories cross at the same state_class via different
  edges.

### Doesn't yet answer
- What collision rate to expect across our ~30 historical traces. This
  needs the simulation in 1.7.0 to compute empirically.
- Whether state_class equivalence (iter 5 ≈ iter 9 in this trace)
  actually surfaces useful "you're going in circles" signal for
  in-trajectory pruning, or whether it's epiphenomenal.

---

## 8. Implications for Phase A design

If this walkthrough is representative:

1. **State_class recipe needs at minimum:** `(obligation_hash, plan_step_idx,
   workdir_signature, env_facts, last_outcome)`. The `last_outcome` is
   important for distinguishing "I just made progress" from "I just
   errored" — different actions are appropriate from each.

2. **Action_class recipe is the simpler half:** `(tool, canonical_args)`
   with light bucketing of numeric args. The canonical_args function will
   need test cases to lock down.

3. **The corpus needs to grow before Dijkstra contributes anything.** Cold
   start is the dominant regime for the first ~10 swe10 runs. Plan A's
   validation must measure cold vs warm explicitly.

4. **Cycle detection within a single trajectory** is a separate, simpler
   win — it doesn't need a corpus, just state_class equivalence detection
   in the current run. Could be a Phase A side benefit.

5. **The `bash` action class is the messy edge case.** Half of django-12039's
   iterations are `bash(...)` with different commands serving wildly
   different intents. If we hash command strings literally, every bash is
   a distinct action_class, killing aggregation. If we canonicalize too
   aggressively, we conflate `bash(pytest)` and `bash(pip install)`. This
   needs a deliberate decision before Phase A — probably hash the COMMAND
   TYPE (`pytest`, `pip-install`, `python-script`, `ls`, etc.) extracted
   by a small regex parser, not the literal string.

---

---

## 9. POC output — what `dijsktra_simulation.py` actually reveals (2026-05-30)

Built `scripts/dijsktra_simulation.py` as the companion to this doc. Single
pass through django-12039's 356-event trace produces the graph below.

### Numerical summary

```
52 iterations
 7 unique pre-states
 6 unique post-states
 3 state-changing transitions   (str_replace×2 + pip-install)
 0 criterion_check ok=True       (attestor never approved a step)
```

The **3 state changes in 52 iterations** is the key statistic. The agent
spent ~94% of its iterations in self-loops.

### The actual graph (real hashes from the run)

```
e34209b1   (plan=0, workdir=v0, env=∅, last=None)
   │
   │  [read]  w=6,345
   ▼
1da7ccf9   (plan=0, workdir=v0, env=∅, last=ok)
   ├─[find_refs]  w=7,049    → self-loop
   ├─[rg]         w=9,950    → self-loop
   └─[str_replace] w=8,918   → state change ★
       ▼
   168d5f2a   (plan=0, workdir=v1, last=ok)
       ├─[read]         w=9,246  → self-loop
       └─[bash pytest]  w=8,678  → error transition ✗
            ▼
       68525910   (plan=0, workdir=v1, last=error)
            └─[bash pip-install]  w=6,550  → env transition ★
                ▼
            129510bd   (plan=0, workdir=v1, env={pytest_installed}, last=ok)
                ├─[rg/read/bash/etc × 8]    → all self-loops
                ├─[bash pytest]  w=9,019    → error transition ✗
                │   ▼
                │   cf272ac7   (plan=0, workdir=v1, env=..., last=error)
                │   └─[fd]  w=8,647  → back to 129510bd
                └─[str_replace]  w=8,117    → state change ★
                    ▼
                f59669ec   (plan=0, workdir=v2, env=..., last=ok)
                    └─[× 36 self-loops]  total w ≈ 305K tokens
                        (read/bash/fd/git_diff/rg cycled in every combo;
                         nothing advanced state; trace eventually ended
                         with harness-oracle saying PASS)
```

### What this concrete graph reveals

**1. The corpus would already be useful with just THIS one trace.**

For each pre-state with multiple actions tried, the corpus knows which one
advanced state vs which were loops. Future runs from the same state could
skip the dead-end actions:

| State | Cheapest forward-advancing action | Wasted actions in this trace |
|---|---|---|
| `1da7ccf9` | `str_replace` (1 of 3 tried) | find_refs, rg (looped) |
| `168d5f2a` | `bash pytest` … which errored, route via `pip-install` | bare `read` |
| `129510bd` | `str_replace` (1 of 8+ tried) | rg, multiple reads, bash variants |
| `f59669ec` | **nothing in this trace advanced** — yet outcome=PASS | all 36 iterations |

The corpus is already telling us: "if you reach `129510bd`, go straight to
`str_replace`; don't grep around first."

**2. The terminal `f59669ec` self-loop is the cost story made visible.**

36 iterations in the same state class. The agent kept trying to *verify* its
fix was correct (bash pytest, bash django-runtests, bash python-c-script,
git_diff, etc.) but every attempt landed back in `f59669ec` without
advancing the agent's internal plan_step.

Critically: the trace ended with harness PASS, but the agent's contract
never closed FULFILLED internally. This is the agent-state vs oracle-truth
gap. **For the corpus to learn from this trace, GOAL must come from the
harness oracle, not from the agent's state machine.**

**3. The `bash` command-class taxonomy works.**

In the 36-loop terminal state, the agent tried at least these distinct bash
classes: `pytest`, `django-runtests`, `python-c-script`, `python-script`,
`git-diff`, `grep`, `ls`, `bash-other`. They all returned to the same
state. The classifier did its job — it didn't collapse meaningfully
different commands.

**4. State collisions WITHIN the trace are massive.** 52 iterations →
7 unique states. That's >7× collision rate inside a single trajectory.
Cross-trace collisions remain to be measured but this is encouraging.

### Bugs / decisions discovered while building the POC

1. **First version had a two-pass bug** — `pre_state[N]` didn't match
   `post_state[N-1]` because `last_outcome` was only updated in the post-pass.
   Fixed by going single-pass with progressive state tracking. **Decision:
   pre/post must match by construction; no two-pass.**

2. **`criterion_ok=True` literally never fires in this trace.** Plan_step
   never advances. For the corpus, this means `plan_step_idx` is uninformative
   on this trajectory — every state has `plan_step_idx=0`. **Decision:
   plan_step_idx is a weak signal at best; we may need to rely on
   workdir_version + env_facts + last_outcome for state distinction.**

3. **The `env_facts` set so far has exactly one entry: `pytest_installed`.**
   Hardcoded. Need a way to declare the set more cleanly, or extract it
   from bash command_classes that we know mutate env.

4. **`f59669ec` having 36 same-state actions is suspicious.** Either:
   - Our state_class is too coarse (some state DID change but we don't capture
     it — e.g., "agent has now tried N pytest variants, knows none work"), OR
   - The agent really was just wasting iterations (which fits the cost
     analysis from 1.6.6.13)
   
   The honest read: probably 90% wasted iterations + 10% missing state
   distinction. **Decision pending — needs more traces to compare.**

### What the script doesn't yet do

- Cross-trace collisions (the `--collide` flag is implemented but only
  pairwise; needs to fan out across all swe10 traces)
- GOAL determination from harness oracle (currently uses agent state)
- Edge-weight averaging when multiple traces visit the same (state, action)
- A* heuristic computation (cost-to-goal from past trajectories)

### Next iteration (proposed)

| Iteration | What | Why |
|---|---|---|
| v1 | Wire in harness `is_resolved` to mark trajectory GOAL correctly | The 36-loop ending PASSed at oracle level; corpus must see that |
| v2 | Run on all swe10 v1.7-tools traces → cross-trace collision report | Test if state hashes collide across DIFFERENT tasks (probably no due to obligation_hash) OR ACROSS the SAME task at different points (yes by construction) |
| v3 | Run on v1.6.6-final swe10 traces too → corpus of 18+ trajectories | The minimum corpus to test "warm" vs "cold" Dijkstra |
| v4 | Compute shortest-path-from-S(initial)-to-GOAL on the merged corpus | THE actual Dijkstra validation |

Each iteration should land both a script change AND an `.md` section
update. Don't lock decisions ahead — let the data drive.

---

## 10. POC iteration v1 — CEGAR-shaped (2026-05-30)

After external research surfaced the CEGAR + value-bisimulation framework
(TriCEGAR mechanism + Ferns/Precup criterion + Dijkstra objective),
rewrote the POC around it. Key shift: raw_features stay UNHASHED. The
predicate tree applies a function over them; refinement just changes that
function over the same data — no committed-hash one-way-door.

Full v1 output in `docs/v1_simulation_output.txt`.

### Merged corpus: v1.7-tools (9 traces) + v1.6.6-final (10 traces) = 19 traces, 392 samples

```
leaf | n_samples | n_traces | mean_real_cost | stdev_real_cost | CV
-----|-----------|----------|----------------|------------------|----
   0 |        80 |        2 |         146300 |           108900 | 0.74
   1 |        67 |        2 |         129050 |            74185 | 0.57
   2 |       104 |        2 |         192425 |           115415 | 0.60
   4 |        15 |        2 |          98578 |            52449 | 0.53
   5 |         9 |        2 |          11722 |             8367 | 0.71
   6 |        12 |        2 |          16485 |            13325 | 0.81
   7 |         9 |        2 |          13065 |             9884 | 0.76
   8 |        37 |        2 |          93912 |            73177 | 0.78
   9 |        27 |        1 |         101238 |            67569 | 0.67   ← FAIL leaf (django-13315 v1.7 only)
```

### Observations

**1. The base-key abstraction `(obligation_hash, plan_idx)` produces one
leaf per task** — because `plan_idx` never advances past 0 in any trace.
(That's the v1.6.6 attestor-divergence problem made visible in the
abstraction.) 10 swe10 tasks → 10 leaves, with n_traces=2 for tasks that
appear in both runs and n_traces=1 for the one task only in v1.7-tools.

**2. Every reachable leaf has d* = 0** under this abstraction. Why: each
trajectory's terminal state is in the SAME leaf as every other sample in
that trajectory (single leaf per task). The 0-cost edge to GOAL therefore
makes the whole leaf's d* = 0. Dijkstra has nothing meaningful to compute
yet — there's no multi-step path because there's effectively one node per
task.

**3. The counterexample signal is REAL and STRONG.** CV ranges 0.53–0.81
across leaves. Within each leaf, realized_cost_to_goal varies wildly —
some samples are early in the trajectory (high remaining cost), others are
late (low remaining cost). This violates value bisimulation: states with
different cost-to-goal should NOT be in the same leaf.

**4. Cross-trace consistency exists.** Same-task leaves in v1.7-tools and
v1.6.6-final land in the same leaf (n_traces=2) — so when v2's CEGAR
loop splits leaf 0, the predicate will be learned from samples drawn
across BOTH runs. Cross-trace transfer is structurally possible (under
this abstraction).

**5. Most-frequent action across the corpus: `bash(pytest)`.** Appears as
the dominant action_class in leaves 0, 2, 6, 7, 8 — anywhere from 14 to
24 visits. Across all of those, **durable=0**. The corpus is screaming:
"calling pytest doesn't advance state — yet the agent keeps calling it."
This is the f59669ec self-loop pattern generalized across the whole swe10.

**6. The actions that DO advance state (durable=1) are rare and hide in
the long tail.** In the top-3 actions per leaf, exactly ONE durable
action is visible: `dfa4a970 str_replace _show_versions.py` in leaf 6
(sklearn). Most durable actions appear with n=1 — they're outliers in
visit count even when they're load-bearing.

### What v1 has proven

- **Ingestion + raw_features storage works** across runs. 19 traces,
  392 samples, all carry their pre/post raw_features.
- **The harness-oracle GOAL wiring is correct** — terminal states of PASS
  trajectories get 0-cost edges to GOAL; FAIL trajectories don't.
- **Backward Dijkstra runs** but produces a degenerate result under v1's
  abstraction (one leaf per task).
- **The CV signal is high enough to drive refinement** — every reachable
  leaf has CV > 0.5, meaning realized_cost_to_goal varies by ~50% within
  the leaf. CEGAR's split criterion will fire on the first iteration.

### What v1 doesn't yet do (v2 work)

- **No splits.** PredicateTree has the structure but no `split` method
  yet. v2 implements `choose_predicate` (DecisionTreeRegressor on raw_features
  for value_split, Classifier for belief_split).
- **No counterexample test.** Variance is reported; no automatic
  trigger to call `tree.split()`.
- **No DEFER logic.** Recommend always returns the only-path; cold-state
  fallback to LLM isn't gated.
- **No LIBRARIAN.** Cross-task transfer relies entirely on shared base
  keys; embedding similarity not implemented.

### Provisional answers to questions raised in §6

| # | Question | v1 evidence-based answer |
|---|---|---|
| 1 | What constitutes a state-changing action? | `is_state_changing()` rule works. ~3 changes per 52-iter trace is plausible. |
| 2 | How to detect bash side effects? | Pattern-based `bash_command_class` works. `pip-install` correctly flagged as state-changing. |
| 3 | Should plan_step_idx be in state? | Yes per framework, but **the attestor rarely says OK in our traces**, so plan_idx is a weak signal in this corpus. The CEGAR loop won't earn it as a predicate split because there's no variance along that axis. |
| 4 | Should obligation_hash be in state? | Yes — already base key. Confirmed by cross-trace consistency observation #4. |
| 5 | How to encode env state? | `env_facts` set works for `pytest_installed`. Tree can learn more env predicates if data demands. |
| 6 | Action_class granularity? | Tool + canonical_args works. Same action classes appear across leaves (confirming aggregation works). |
| 7 | Cycles within trajectory? | Visible as self-loop edges; will become a belief_split trigger in v2. |
| 8 | bash command-class hashing? | Working — pytest / django-runtests / pip-install / etc. all separately classified. |

### Next: v2 — implement the CEGAR loop

The split that should be tried first: **leaf 0 (CV=0.74, 80 samples,
2 traces)**. The variance is high, the sample count supports stump fitting
(>>3), and the leaf includes the dominant `bash pytest` self-loop pattern
that's the failure mode we want to learn from.

v2 prediction: the stump will split on either `workdir_version` (early
vs late in trajectory — the "I've now made my edit" boundary) or
`n_attempted_bash` (have we already tried pytest N times — the
diminishing-returns signal).

Both would be load-bearing CEGAR predicates: a future agent at a state
matching the "pytest already tried 3+ times" predicate would be steered
AWAY from another pytest call.

---

## Sources

- Trace file: `data/runs/swebench/2026-05-28/16-28-49-swe10-v1.7-tools/django__django-12039/django__django-12039.1-of-1.swe10-v1.7-tools/agent-logs/cat-tb-8d35f3fb.jsonl`
- Plan: 2 steps (edit ddl_references.py + run pytest)
- Outcome: PASS at iter 52, 445,282 input tokens
- Tool mix: bash×~30, read×~10, str_replace×2, rg×3, find_refs×1, fd×3, git_diff×1
