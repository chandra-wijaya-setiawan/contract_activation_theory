# iteration.md — loop architecture, working document

Status: WORKING DRAFT. Living document. Update as the design moves.
Owner: this session (2026-05-31 reset; A.1/A.2/A.3/A.4 PRDs are SCRAPPED).

This file captures the loop redesign discussion. It is *not* a PRD. No code
should reference it. Treat it as scratchpad for shared understanding.

---

## 1. What the loop is TODAY (factual)

`cat/activator.py:Activator.run()` implements an 8-step CAT loop:

```
Step 1  INGEST       — caller hands in Contract                   off-loop
Step 2  REPRESENT    — Representer probes verified facts          off-loop, once
Step 3  STIPULATE    — Stipulator LLM emits run.plan              off-loop, once
Step 4  ACTIVATE     — admission gate                             off-loop, once
                ↓
        ┌──── discharge loop ────┐
        │  budget + iter check    │
        │  Step 5+6  _perform_once │  ← LLM picks tool, executes
        │  Step 7    _verify       │  ← criterion goal-check (interval)
        │            _update_plan_checklist  ← judge marks boxes
        │            _prune_to_window         ← drop oldest turn
        └─────────────────────────┘
Step 8  CLOSE       — _finalize
```

**The inner loop is ReAct.** Strip CAT extras (admission gate, attestor,
librarian hint, election, verifier) and the engine is:

  Thought (LLM result.text) → Action (one tool call) → Observation (tool result)

CAT extras live AROUND this ReAct core; they don't replace it.

### Where "planning" lives today

- Stipulate runs ONCE before the loop. Plan is static through the loop.
- Inside the loop: LLM picks one tool call. That's *reaction*, not planning.
- `_update_plan_checklist` marks plan steps done/not-done. Never revises the plan.
- Stipulate output is **prose + 2 fields**: `{id, description}`. The prompt
  actively **bans** investigation steps ("DO NOT include identify/examine/
  determine"). Result: a typical 2-step plan that doesn't describe most of
  the actual work.

### Where "observation" lives today

- Tool result text goes into `run.messages`. That's the full observation surface.
- `_verify_criteria` is a binary goal-test, not a state summary.
- `_update_plan_checklist` is a judge over messages; observation only of the checklist.
- There is **no event** recording "what changed in the world this iter" — no
  durable_change flag, no state delta.

### The asymmetry to fix

| Phase | What it should be | What it is today |
|---|---|---|
| Beginning of iter | PLAN: given state, what's the multi-step path? | LLM reactively picks one tool |
| Action | Execute one step of the plan | Execute one tool call |
| End of iter | OBSERVE: what changed? Are we closer? | Goal-check + checklist-mark only |

---

## 2. Root cause of plan staleness (the thing we're solving)

Claude Code's planner (and every flat-checklist planner) goes stale because
**the plan is not the same object as the agent's working state.** It's a
snapshot. Snapshots stale. Pivots either ignore the plan or regenerate it
wholesale (losing context).

The fix: **the plan IS the working state.** Updating the plan = updating
reality's record. No drift possible because there's no separate source of
truth. This requires three things:

1. **Plan as a tree, not a flat list** — phases hold steps, steps may refine
   into substeps, dead branches get pruned without losing history.
2. **Plan persists in the librarian** — survives the run, gets indexed by
   obligation_class, becomes transfer-learning material.
3. **Re-planning is tree-edit ops** — never wholesale regen. Each edit is a
   trace event. Replayable, auditable, cheap.

---

## 3. The plan as a working tree (CENTRAL DATA STRUCTURE)

### Shape

```
PlanTree (per task; persists in librarian after run)
  ├── root: obligation
  ├── phase A (MAP)              [active]
  │     ├── A1 — read client.py             [complete]
  │     ├── A2 — read base/client.py        [complete]
  │     ├── A3 — read test_postgresql.py    [active]   ← attention here
  │     ├── A4 — list libpq env vars        [pending]
  │     └── A5 — find callers               [pending]
  ├── phase B (DESIGN)            [pending]
  │     ├── B1 — decide return type
  │     └── B2 — decide signature change
  ├── phase C (IMPLEMENT)         [pending]
  │     ├── C1 — edit postgresql/client.py
  │     ├── C2 — edit base/client.py
  │     └── C3 — edit other backends
  └── phase D (VERIFY)            [pending]
        └── D1 — run dbshell test
```

### Node schema

```python
@dataclass
class PlanNode:
    id: str                          # "A3"
    parent_id: Optional[str]         # "A" (phase)
    description: str                 # prose for the actor
    intent: Literal["investigate", "design", "mutate", "verify"]
    expects: dict                    # {durable_change, files_modified, verifier_outcome}
    status: Literal["pending", "active", "complete", "pruned", "branched"]
    children: list[str]              # child node IDs (for refinement)
    created_at: str
    completed_at: Optional[str]
    completion_reason: Optional[str]
```

- **`intent`** classifies the node (used by canonicalization + librarian).
- **`expects`** is the structured prediction. Mechanically verifiable.
- **`status`** drives the activator's attention pointer.

### The agent has a single attention pointer

`run.plan_tree.attention_id` = the node ID the actor is currently working on.
The actor LLM sees only `attention_node.description` (narrow focus) plus a
summary of the upcoming nodes in the chain (lookahead). All other nodes are
out of the actor's working context.

---

## 4. Tree-edit operations (the unit of re-planning)

Re-stipulate is **never** wholesale regen. It's a sequence of these ops, each
recorded as a trace event. **Op names follow Git Context Controller (GCC)
vocabulary where applicable** — see §14.1.

| Op | When fired | Trace event kind |
|---|---|---|
| `COMMIT(node_id, reason)` | step finished + attestor verdict YES | `plan_commit` |
| `CHECKOUT(node_id)` | attention moves to this node | `plan_checkout` |
| `REFINE(node_id, [substeps])` | discovered the step has subparts | `plan_refine` |
| `DROP(node_id, reason)` | step turned out unnecessary (history kept) | `plan_drop` |
| `BRANCH(node_id, [alt_steps])` | trying alternative approach | `plan_branch` |
| `COMMIT_NEW(parent_id, new_step)` | append new sibling node | `plan_commit_new` |
| `MERGE(branch_id, target_id)` | fold successful branch back into main | `plan_merge` |

Every op payload optionally carries a `reflection: str` field (from LATS,
§14.3) — a short LLM-written note attached to high-surprise ops, e.g., on a
BRANCH or DROP. Stored with the op for retrospective analysis and as
material for the librarian's anti-template entries.

The Stipulator's in-loop call produces **a list of ops against the current
tree**, not a fresh plan. Cold start (iter 0) is the only time the
Stipulator emits a full tree from scratch.

### Surprise → tree op mapping

| Surprise type | Triggered op(s) |
|---|---|
| Action did nothing observable (state unchanged) | `DROP(current) + COMMIT_NEW(parent, alternative)` |
| Action surprised but advanced | `REFINE(current)` — discovered substructure |
| Verifier loop (FAIL→FAIL same error class) | `BRANCH(current, alternative_approach)` |
| Step done early (no further substeps needed) | `COMMIT + DROP(remaining_siblings)` |
| New work discovered | `COMMIT_NEW(current, new_step)` |
| `expects.durable_change` mismatch | `BRANCH(current, retry_with_different_action)` |
| `expects.files_modified` mismatch | `REFINE(current)` — sub-step missed something |
| `expects.verifier_outcome` mismatch | `BRANCH(current, alternative_verifier)` |
| Alternative BRANCH succeeded | `MERGE(branch_id, main)` — fold the win back |

Each is a small local edit. Cheap. The plan stays alive without massive
re-stipulates.

---

## 5. The redesigned loop (end-to-end)

Three phases: **pre-loop setup**, **iter N**, **post-loop persistence**.

### 5.1 Pre-loop setup (off-loop, once)

```
1. INGEST              contract handed in by caller
2. REPRESENT           Representer probes world; produces representations
3. obligation_class    = compute_obligation_class(contract.obligation,
                                                  representations)  [§8.1]
4. templates           = librarian.get_plan_templates(obligation_class)  [§7 role 3]
5. anti_templates      = librarian.get_anti_templates(obligation_class)  [§7 role 4]
6. STIPULATE-cold      = call_stipulator(
                          obligation, representations,
                          templates, anti_templates,
                          schema=§6.1 cold-start
                        )
                       → full PlanTree with ids assigned by activator
7. ACTIVATE            admission gate; grants capabilities
8. attention_id        = first leaf node of MAP phase
```

### 5.2 Iter N (the discharge loop)

```
─── ITER N ─────────────────────────────────────────────
PLAN phase
  1. WHERE-AM-I         state_now = compute_state_class(run_state,
                                                        plan_tree)  [§8.5]
  2. ATTENTION          attention_node = plan_tree.get(attention_id)
  3. LOOKAHEAD          chain = path(attention_node → leaves of
                                     VERIFY phase via plan_tree)
                        // surfaced to PERFORMER prompt as upcoming-work hint
  4. SIMULATE
       deterministic    predict = librarian.predict_det(state_now,
                                                         action_class)  [§7 role 2]
                        // returns {cost_mean, cost_std, next_state_dist,
                        //          outcome_dist, d_star_dest}
                        // cold edge → per-tool global averages, large σ
       reasoning        = attention_node.description + attention_node.expects
  5. DECIDE             if predict.cost_mean > remaining_budget OR
                           predict.outcome_dist.PASS < threshold:
                          call Stipulator-in-loop [§6.2] BEFORE acting
                          apply returned ops to plan_tree [§6.4]
                          retry from step 1
                        else proceed to ACTION

ACTION
  6. EXECUTE            PERFORMER LLM picks tool call to advance
                        attention_node; activator invokes tool
                        (the existing _perform_once path)

OBSERVE phase
  7. WHERE-AM-I-NOW     state_next = compute_state_class(run_state_post,
                                                          plan_tree)  [§8.5]
  8. SURPRISE-CHECK     6-dimensional vector:
                          cost_z          = |actual - predict.cost_mean| /
                                            max(predict.cost_std, ε)  > 2 ?
                          state_match     = state_next == argmax(predict.next_state_dist)
                          outcome_match   = actual_outcome == argmax(predict.outcome_dist)
                          durable_match   = (git diff --quiet failed?) ==
                                            attention_node.expects.durable_change
                          files_match     = actual_files ⊆ attention_node.expects.files_modified
                          verifier_match  = actual_verifier_outcome ==
                                            attention_node.expects.verifier_outcome
                        (attestor verdict = fallback prose comparator)
  9. APPLY TREE OP      if any surprise dim set:
                          call Stipulator-in-loop [§6.2] with surprise_dims
                          validate + apply ops to plan_tree [§6.4]
                        else:
                          op = COMMIT(attention_id, reason, reflection)
                          attention_id = next pending node in plan_tree
                          apply
 10. RECORD             librarian.record_edge(
                          state_now, action_class, state_next,
                          actual_cost, actual_outcome
                        )
                        trace.append all tree-edit events with reflection
                        librarian.update_simulation_accuracy(edge, predict, actual)
 11. VERIFY (interval)  every K iters OR at phase boundary:
                          run _verify_criteria; on PASS → FULFILLED, exit loop
─────────────────────────────────────────────────────────
```

### 5.3 Post-loop persistence (off-loop, once, on terminal state)

```
1. FINALIZE plan_tree   stamp outcome (FULFILLED / FAILED / SUSPENDED)
                        gather all reflections from tree-edit events
2. RECORD plan_tree     librarian.record_plan_tree(
                          plan_tree,
                          outcome,
                          reflections,
                          indexed_by=obligation_class
                        )
                        // PASS → templates bucket
                        // FAIL → anti-templates bucket [§14.2 SWE-Exp]
3. CLOSE                _finalize: write parquet, dag.json, outcome.json
```

### 5.4 What's new vs. ReAct

| Piece | ReAct | This loop |
|---|---|---|
| Plan scope | none (LLM picks next tool) | working tree, multi-phase, persistent |
| Attention | implicit | explicit (`plan_tree.attention_id`) |
| Predict | none | deterministic (5+ dims via librarian) + reasoning (expects+description) |
| Observe | tool result in msgs | 6-dim structured surprise vector |
| Replan | none | tree-edit ops [§4] with reflection [§6.3] |
| Record | append to msgs | edges + tree-edit events + simulation accuracy + plan trees indexed by obligation_class |
| Transfer | none | plan-tree templates + edge stats both indexed by canonicalization hashes |

### 5.5 What's new vs. today's CAT loop

| Piece | Today (CAT v1.7) | This loop |
|---|---|---|
| Stipulate cadence | once, pre-loop | cold pre-loop + in-loop on surprise |
| Plan structure | flat `list[_PlanStep]`, prose-only | PlanTree, structured `expects` per node |
| Investigation steps | banned in prompt | first-class plan nodes |
| End-of-iter | `_verify_criteria` + `_update_plan_checklist` | structured surprise vector + tree-edit op |
| Librarian | dead `recommend()` | 4 roles (cost, forward-model, templates, anti-templates) |
| State_class | computed but too narrow | 9-component bucketed hash (§8.5) |
| Cross-task transfer | none | plan-tree templates indexed by `obligation_class` |
| Persistence after run | trajectory parquet | trajectory + plan tree in librarian |

---

## 6. Stipulator output schemas (Q2 resolved)

The Stipulator has **two distinct call modes** with two distinct JSON
schemas. The activator chooses the mode based on call site.

### 6.1 Cold-start schema (iter 0, Step 3 STIPULATE)

Returns a full PlanTree. Investigation steps are first-class. Typical
plans are 10-20 steps across 3-5 phases.

```json
{
  "phases": [
    {
      "name": "MAP",
      "steps": [
        {
          "description": "Read postgresql/client.py to locate settings_to_cmd_args_env",
          "intent": "investigate",
          "expects": {
            "durable_change": false,
            "files_modified": [],
            "verifier_outcome": "N/A",
            "knowledge_gained": "current signature of settings_to_cmd_args_env"
          }
        }
      ]
    },
    { "name": "DESIGN", "steps": [...] },
    { "name": "IMPLEMENT", "steps": [
      {
        "description": "Edit settings_to_cmd_args_env to build env dict from OPTIONS SSL keys",
        "intent": "mutate",
        "expects": {
          "durable_change": true,
          "files_modified": ["django/db/backends/postgresql/client.py"],
          "verifier_outcome": "N/A"
        }
      }
    ]},
    { "name": "VERIFY", "steps": [
      {
        "description": "Run python -m django test dbshell.test_postgresql",
        "intent": "verify",
        "expects": {
          "durable_change": false,
          "files_modified": [],
          "verifier_outcome": "PASS"
        }
      }
    ]}
  ]
}
```

**Note: no node `id` fields.** The activator assigns hierarchical IDs
(`A`, `A1`, `A2`, `B`, `B1`, …) after parsing. This frees the LLM from
ID-uniqueness bookkeeping.

**Field constraints**:
- `intent` ∈ {`investigate`, `design`, `mutate`, `verify`}
- `expects.verifier_outcome` ∈ {`PASS`, `FAIL`, `N/A`}
- `expects.durable_change`: bool
- `expects.files_modified`: list of paths (may be empty)
- `expects.knowledge_gained`: optional, only meaningful for `investigate`

### 6.2 In-loop schema (re-stipulate on surprise)

Returns a list of tree-edit ops drawn from the §4 vocabulary. Applied
in order. Each op has type-specific payload + optional `reflection`.

```json
{
  "ops": [
    {
      "op": "COMMIT",
      "node_id": "A3",
      "reason": "test expectations confirmed: expects (args, env) tuple",
      "reflection": "test_postgresql.py asserts on a 2-tuple return; signature change is the right direction"
    },
    {
      "op": "CHECKOUT",
      "node_id": "A4",
      "reflection": "investigation continuing; next: enumerate libpq env var names"
    },
    {
      "op": "DROP",
      "node_id": "A5",
      "reason": "no callers ripple — settings_to_cmd_args_env not called externally",
      "reflection": "grep showed only base/client.py calls this; signature change is local"
    },
    {
      "op": "COMMIT_NEW",
      "parent_id": "C",
      "new_step": {
        "description": "Update base/client.py runshell() to accept env dict",
        "intent": "mutate",
        "expects": {
          "durable_change": true,
          "files_modified": ["django/db/backends/base/client.py"],
          "verifier_outcome": "N/A"
        }
      },
      "reflection": "base/client.py runshell also needs to pass env to subprocess.run"
    },
    {
      "op": "REFINE",
      "node_id": "C1",
      "substeps": [
        {
          "description": "Build env dict from OPTIONS SSL keys",
          "intent": "mutate",
          "expects": {"durable_change": true, "files_modified": ["..."], "verifier_outcome": "N/A"}
        },
        {
          "description": "Change return type to (args, env) tuple",
          "intent": "mutate",
          "expects": {"durable_change": true, "files_modified": ["..."], "verifier_outcome": "N/A"}
        }
      ],
      "reflection": "the edit decomposes into two distinct sub-edits: env-dict construction, then return-type change"
    },
    {
      "op": "BRANCH",
      "node_id": "D1",
      "alt_steps": [
        {
          "description": "Run python -m django test dbshell instead of pytest",
          "intent": "verify",
          "expects": {"durable_change": false, "files_modified": [], "verifier_outcome": "PASS"}
        }
      ],
      "reflection": "pytest failed 3× with ImproperlyConfigured; this project uses django test runner, not pytest"
    },
    {
      "op": "MERGE",
      "branch_id": "D1-alt-1",
      "target_id": "D1",
      "reflection": "django test runner produced PASS; folding the alternative back as the canonical verify step"
    }
  ]
}
```

### 6.3 Per-op payload schemas

**`reflection: str` is REQUIRED on every op.** It's the learning signal —
the librarian's PASS/FAIL templates are only as good as the reflections
attached to them. Cost is ~30-80 tokens per op; value is the corpus
richness that makes cross-task transfer actually work.

| Op | Required fields |
|---|---|
| `COMMIT` | `node_id`, `reason`, `reflection` |
| `CHECKOUT` | `node_id`, `reflection` |
| `BRANCH` | `node_id`, `alt_steps[]`, `reflection` |
| `MERGE` | `branch_id`, `target_id`, `reflection` |
| `DROP` | `node_id`, `reason`, `reflection` |
| `COMMIT_NEW` | `parent_id`, `new_step`, `reflection` |
| `REFINE` | `node_id`, `substeps[]`, `reflection` |

`alt_steps[]`, `substeps[]`, and `new_step` use the same step schema as
cold-start (description, intent, expects — no id; activator assigns).

**What goes in a reflection** depends on the op:
- `COMMIT` — what specifically confirmed completion. "test expectations confirmed: expects (args, env) tuple, not just args"
- `CHECKOUT` — why attention is moving here. "investigation phase complete, beginning design"
- `BRANCH` — why the alternative is worth trying. "pytest failed 3× ImproperlyConfigured; trying Django's test runner instead"
- `MERGE` — what the branch proved. "alternative verifier confirmed PASS; folding result"
- `DROP` — why this path is abandoned. "no callers ripple — signature change is local"
- `COMMIT_NEW` — what missing work was discovered. "base/client.py runshell signature also needs update"
- `REFINE` — what substructure was discovered. "the edit is two steps: build env dict, then change return type"

### 6.4 Validation + fallback

The activator validates the op list before applying:

1. **JSON parse** — invalid JSON → fallback to wholesale re-stipulate
2. **Per-op schema** — missing required field → reject that op, log warning
3. **Reference integrity** — `node_id` / `branch_id` / `parent_id` /
   `target_id` must exist in the current tree (or be created by an
   earlier op in the same batch). Invalid refs → reject that op.
4. **No cycles** — REFINE/BRANCH cannot point to ancestors.
5. **Apply in order** — later ops may reference IDs created by earlier ops.

If `>50% of ops` are rejected → fallback to wholesale re-stipulate (the
in-loop LLM is confused; cold-restart the plan from current state).

### 6.5 Prompt differences

**Cold-start prompt** (iter 0):
- Obligation text
- Verified representations
- Optional: K PASS plan-tree templates from librarian (similar obligation_class)
- Optional: K FAIL anti-templates with reflections
- Instruction: emit full PlanTree per §6.1 schema

**In-loop prompt** (on surprise):
- Obligation text (brief)
- Current PlanTree serialized
- Current `attention_id`
- `state_class` summary (counts, last_outcome, durable_change_count)
- Recent trace events (last 3-5 iters)
- Surprise dimensions that triggered this call (which bits flipped)
- Optional: relevant librarian edge stats for current state
- Instruction: emit minimal ops list per §6.2 schema. Don't regen — only
  change what needs changing.

---

## 7. The librarian's four jobs

The library has four roles:

1. **Cost/path provider** — given state, return shortest path edge stats.
2. **Forward model** — given (state, action), predict next state + cost dist.
3. **Plan-tree template provider** — given an `obligation_class`,
   return K prior **PASS** plan trees. The Stipulator uses these to seed
   iter-0 instead of starting blank.
4. **Anti-template provider** (from SWE-Exp, §14.2) — given an
   `obligation_class`, return K prior **FAIL** plan trees with their
   reflection annotations. Tells the Stipulator which subtrees to avoid.
   Failure patterns are equally valuable signal — knowing what doesn't
   work is often cheaper than rediscovering it.

### What gets stored per task

- **Edges**: `(state_class, action_class, state_class') → (visit_count,
  cost_history, outcome_dist, simulation_accuracy)`.
- **Plan trees**: serialized post-run, indexed by `obligation_class`. With
  metadata: which nodes fired, which surprised, final outcome (PASS/FAIL).
- **Tree-edit traces**: every op, replayable. Useful for analyzing where
  plans pivot.

### Cross-task transfer

Two levels:

| Level | Granularity | Triggers when |
|---|---|---|
| Edge-cost | per (state, action) step | local move scoring |
| Plan-tree | per (obligation_class) task | initial Stipulate seeding |

Plan-tree transfer is qualitatively richer than edge-cost transfer. It's
saying *"the SHAPE of work for this obligation type looks like this,"* not
just *"this micro-step is cheap."*

---

## 8. Canonicalization, made concrete

Three hashes, three scopes:

| Hash | Computed from | Used by | Scope |
|---|---|---|---|
| `obligation_class` | framework + subsystem (L1) — see §8.1 | plan-tree retrieval | task-level |
| `state_class` | progress shape (see Q1 below) | edge graph | iter-level |
| `action_class` | tool + canonical args | edge graph | action-level |

- A **branch** is emergent in the edge graph: a state_class node with ≥2
  outgoing edges (came from earlier multi-viable-action decisions).
- **Backtrack** is emergent: when `state_next` has worse d* than `state_now`,
  next iter applies a `BRANCH` tree op and rewinds attention.
- **Plan-tree branches** (the `BRANCH` op) are different from edge-graph
  branches — same name, different layer. Plan-tree branches live in the
  tree as alternative subtree attempts; edge-graph branches live in the
  state-action graph as observed multi-action states. They CAN coincide but
  don't have to.

### 8.1 obligation_class — concrete definition (Q4 resolved 2026-05-31)

obligation_class is a hash over categorical features extracted from
obligation + representations at iter 0. **PoC level: L1** = (framework,
subsystem).

```python
def compute_obligation_class(obligation: str, representations: list) -> str:
    framework = extract_framework(representations)    # "django", "sphinx", "flask"
    subsystem = extract_subsystem(representations)    # "db.backends", "http.middleware"
    return sha256(f"{framework}|{subsystem}".encode()).hexdigest()[:12]

def extract_framework(reps):
    # From repo root in Representer evidence, OR top-level import path.
    # django/django → "django"; sphinx-doc/sphinx → "sphinx"

def extract_subsystem(reps):
    # File paths from Representer evidence (regex-extract *.py).
    # Take longest common prefix; drop framework; take top 2 components.
    # E.g., LCP "django/db/backends/" → after framework drop → "db/backends"
    # → top 2 → "db.backends"
```

### 8.2 Granularity ladder (for future tuning)

| Level | Hash inputs | When to use |
|---|---|---|
| L0 | framework only | Way too broad; do not use |
| **L1** | **framework + subsystem-top-2** | **PoC default** |
| L2 | + task_intent (bug_fix / feature_add / refactor) | If L1 over-transfers |
| L3 | + error_signature (exception class) | If L2 still over-transfers |

**Retrieval ladder**: search at finest level first; fall back to coarser
if no hits. PoC starts at L1 with no fallback; add fallback once we have
data.

### 8.3 Why categorical, not embedding

For PoC, we use categorical features rather than LLM-embedding similarity:

| Categorical (L1) | Embedding |
|---|---|
| Transparent — can read the class string | Opaque vector |
| Debuggable — "why didn't this collide?" → check features | Hard to debug |
| No external dependency | Embedding model dependency |
| Deterministic across model versions | Possibly unstable |
| Failure modes visible | Failure modes hidden |
| Cheap (regex + hash) | API call or local model |

If categorical proves too coarse (visible: wrong templates transfer →
many tree edits in first 3 iters), we can swap or layer in embedding
similarity within an L1 bucket.

### 8.4 Honest risks for obligation_class

1. **L1 might be too narrow**: < 20% of tasks share a class. Mitigation:
   log hit-rate, broaden to L0 or top-3 subsystem components.
2. **L1 might be too broad**: wrong templates transfer. Mitigation:
   track tree-edit count in first 3 iters when templates were used;
   spike → narrow to L2.
3. **Subsystem extraction brittle for multi-subsystem tasks.** Mitigation
   for PoC: use first-mentioned file's subsystem. Accept imperfection.
4. **Framework extraction needs Representer to expose repo root.** If the
   Representer doesn't currently probe repo root, add it.

### 8.5 state_class — concrete definition (Q1 resolved 2026-05-31)

state_class is a hash over the agent's **progress shape** at the current
iter. NOT world state, NOT file paths, NOT message history. 9 components:

```python
def compute_state_class(run_state, plan_tree) -> str:
    parts = [
        run_state.obligation_class,                        # task-level bucket
        plan_tree.attention_node.phase_name,               # MAP/DESIGN/IMPLEMENT/VERIFY
        plan_tree.attention_node.intent,                   # investigate/design/mutate/verify
        bucket(run_state.investigate_count, [0,1,3,6,11]),  # 5 buckets
        bucket(run_state.mutate_count, [0,1,2,4]),          # 4 buckets
        bucket(run_state.verify_count, [0,1,2,4]),          # 4 buckets
        bucket(run_state.durable_change_count, [0,1,2,4]),  # 4 buckets
        bucket(run_state.iter_since_durable_change, [0,2,6,11]),  # 4 buckets
        run_state.last_outcome,                            # PASS/FAIL/ERR/None
    ]
    return sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]

def bucket(value: int, edges: list[int]) -> int:
    # Returns bucket index. edges=[0,1,3,6,11] → buckets: =0, 1-2, 3-5, 6-10, 11+
    for i, e in enumerate(edges):
        if value < edges[i+1] if i+1 < len(edges) else value < 10**9:
            return i
    return len(edges) - 1
```

### 8.6 Field rationale

| Field | Why include |
|---|---|
| obligation_class | Within-class cost signal is reliable; cross-class is theoretical |
| phase_name | MAP vs IMPLEMENT have very different action cost profiles |
| intent | Investigate-heavy vs mutate-heavy states differ structurally |
| investigate_count (bucketed) | "How much exploration?" — strong progress signal |
| mutate_count (bucketed) | "Have we started writing?" — strongest signal |
| verify_count (bucketed) | "Stuck verifying?" detector |
| durable_change_count (bucketed) | Filesystem-confirmed mutations — the big one |
| iter_since_durable_change (bucketed) | "Stuck investigating?" detector |
| last_outcome | Post-PASS / post-FAIL / pre-verify differ in next-action |

**State-space size**: `obligation_class × 4 phases × 4 intents × 5×4×4×4×4
× 4 outcomes ≈ 5,120 states per obligation_class.` Per task ~30 unique
states; after 50 tasks per obligation_class → ~1,500 observations across
5,120 possible states → ~30% coverage with multi-observations on common
transit states. That's where edge predictions become useful.

### 8.7 What's deliberately NOT in state_class

| Excluded | Why |
|---|---|
| Specific file paths | Progress shape, not world state |
| Specific tool names | Covered by action_class |
| Message history | Way too narrow |
| `expects.files_modified` | Same as file paths |
| Plan node ID (e.g., "A3") | Run-specific; not portable |
| Current attention node ID | Run-specific |

### 8.8 Honest risks for state_class

1. **Bucketing thresholds arbitrary.** PoC uses fixed; later switch to
   percentile-based from observed data.
2. **Cumulative vs per-phase counts.** Chose cumulative. A "2nd investigate
   after a mutate" hashes the same as "2nd investigate before any mutate"
   — semantically different. For PoC: accept the conflation.
3. **Phase normalization required.** Stipulator must emit phases from
   {MAP, DESIGN, IMPLEMENT, VERIFY}. Prompt constraint + schema validation;
   on violation, fall back to "OTHER" or reject.
4. **Counts only grow within a run.** No within-run state recurrence by
   design. Cross-run is the only collision source — exactly what edge
   transfer needs.
5. **obligation_class inclusion = narrow scope.** Within-class signal
   reliable; cross-class is bet. Easy to drop later; hard to recover from
   noise if we started without.

---

## 9. Cost model

| Iter shape | LLM calls | Notes |
|---|---|---|
| Steady, no surprise | action + attestor = 2 | Same as today |
| Surprise → tree-edit | action + attestor + tree-edit-LLM = 3 | One extra for the diff |
| Cold (iter 0) | stipulate-full + action + attestor = 3 | Same as today's first iter |

The attestor runs every iter already. The new cost is ONE additional LLM
call per surprised iter (to produce the tree-edit diff). If 30% of iters
surprise: expected cost ≈ 2.3× the steady-state. Roughly 2× ReAct overall.

**Critically: tree-edit is cheaper than wholesale re-stipulate.** A diff is
~3-10 op records; a fresh plan is 10-20 step records. Both are LLM JSON
calls; the diff is meaningfully smaller in output tokens.

---

## 10. Honest open questions

Load-bearing. If any answer is wrong, the rest collapses.

### Q1. What goes into `state_class`? ✓ RESOLVED 2026-05-31

**Resolution**: see §8.5. 9 components, bucketed counts to enable
cross-run collision. Field rationale in §8.6, exclusions in §8.7,
risks in §8.8.

### Q2. What does the plan-tree-edit LLM actually produce? ✓ RESOLVED 2026-05-31

**Resolution**: see §6. Two distinct schemas:
- Cold-start (§6.1): full PlanTree JSON, no IDs (activator assigns)
- In-loop (§6.2): minimal ops list drawn from §4 vocabulary, each with
  type-specific payload + optional `reflection`
- Validation (§6.4): JSON parse → schema → ref integrity → cycles →
  apply in order. >50% rejection → fallback to wholesale re-stipulate.

### Q3. What triggers a tree-edit?
Surprise signals (from §5 step 8):
- cost z-score > 2
- next state_class mismatch
- outcome mismatch
- expects.durable_change mismatch
- expects.files_modified mismatch
- expects.verifier_outcome mismatch
- attestor verdict NO (fallback)

OR-gate: any → tree-edit fires. Could weight later.

### Q4. How is `obligation_class` computed? ✓ RESOLVED 2026-05-31

**Resolution**: see §8.1. L1 categorical hash over (framework, subsystem-top-2).
Granularity ladder L0→L1→L2→L3 in §8.2 for future tuning. Categorical
over embedding for PoC (§8.3); reasoning + risks in §8.4.

### Q5. Cold-start mental simulation is unreliable
LLM-as-simulator can be overconfident or overcautious. Until library has
data, surprise-check fires on hallucination. Mitigation: only trust
deterministic surprise dimensions on cold edges; reasoning-surprise needs
attestor + n≥2 to be trusted.

### Q6. `d*` is meaningless until library is warm
First N tasks: no d*. Math layer can't pay off until n≥2 PASS trajectories
per state_class — may take 30-50 tasks. PoC will look weak early.

### Q7. Tree-edit op LLM may emit malformed diffs
Asking LLM for "edit ops against current tree" is harder than asking for a
fresh plan. Mitigation: structured output mode, JSON schema validation,
fallback to wholesale regen on parse failure.

### Q8. Plan tree rigidity
A tree with attention pointers + completion status is more rigid than a flat
plan. If attention is wrong, system breaks. Need careful design of
attention-shift so LLM can override smoothly.

### Q9. Storage / indexing of persistent plan trees
N tasks × tree ≈ a lot of JSON. Indexing by `obligation_class` matters.
Need a real DB-side index, not full table scans.

---

## 11. What's scrapped

- `cat/librarian.py` single-edge `recommend()` (degenerate Dijkstra)
- `state_class` as currently hashed (too narrow, never collides)
- ELECTION mechanism (dead code, never overrides)
- `librarian_hint` event kind + emission path (premature)
- `_PlanStep` 2-field schema (replaced by PlanNode)
- Stipulator's "no investigation steps" ban
- "Re-stipulate as wholesale regen" idea (replaced by tree-edit ops)
- A.1 / A.2 / A.3 / A.4 PRDs (kept in git history; not the spec anymore)

---

## 12. What's kept

- Trace recorder + JSONL/parquet schema (`cat/trace.py`)
- Contract → criterion → activation philosophical core (`cat/contracts.py`)
- 8-step CAT scaffold (Steps 1–4 stay; loop interior is what we redesign)
- Stipulator's existence + role (extended schema; same role)
- Attestor's existence + role (demoted from primary to fallback)
- Representer (provides obligation context for plan-tree templates)
- Terminal-bench Docker setup, swe10/swe30 harness, `scripts/run_bench.sh`
- Diagnostic capability (we can hand-walk any trace in minutes)

## 12b. What's added (NEW)

- **Chronological eval harness** (from SWE-Bench-CL, §14.5) — tasks run
  in order; librarian persists between runs. New cmd:
  `scripts/run_bench.sh --chronological --persist-librarian`.
- **`cat/plan_tree.py`** — PlanTree class + ops (COMMIT, CHECKOUT, BRANCH,
  MERGE, DROP, COMMIT_NEW, REFINE), serialization/deserialization.
- **New trace event kinds**: `plan_commit`, `plan_checkout`, `plan_branch`,
  `plan_merge`, `plan_drop`, `plan_commit_new`, `plan_refine`.
- **Librarian extension**:
  - `get_plan_templates(obligation_class)` → list of PASS plan trees
  - `get_anti_templates(obligation_class)` → list of FAIL plan trees with reflections
  - `record_plan_tree(plan_tree, outcome, reflections)` → write
- **Stipulator JSON schema rewrite** — phased plan tree output, structured
  `expects` per step, investigation steps allowed.

---

## 13. Conceptual threads this design unifies

The plan-tree-in-librarian design pulls together threads we've been circling:

| Earlier thread | How it maps |
|---|---|
| "Git for reasoning" | The plan tree IS the git tree. Phases = branches. Tree-edit ops = commits. |
| "Multi-step lookahead chain" | A chain = a path through the tree. |
| "Attention" | Single pointer to one node in the tree. |
| "Mental simulation" | `expects` field + librarian's `predict_det()`. |
| "Surprise / replan" | Surprise vector → maps to a tree-edit op. |
| "Cross-task transfer" | Plan-tree templates retrieved by obligation_class. |
| "Backtrack" | Tree `branch` op + attention rewind. |
| "Re-stipulate" | Tree-edit ops, not full regen. |
| "Don't let plan stale" | Plan IS the working state; no separate snapshot. |

---

## 14. Related work and what we integrate

Adjacent research surveyed 2026-05-31. Nobody assembles the full design,
but most pieces have been explored in isolation. This section says what
we **borrow** and where we **diverge**.

### 14.1 Git Context Controller (GCC), arxiv 2508.00031

**What it is**: a structured context-management framework with git-like
ops (COMMIT, BRANCH, MERGE, CONTEXT) for agent memory. COMMIT
checkpoints progress, BRANCH explores alternatives, MERGE synthesizes,
CONTEXT retrieves history at multiple granularities.

**Borrow**:
- **Adopt their vocabulary** for our tree-edit ops where it fits. Their
  naming is well-thought-out; reusing it improves readability and
  positions us in the literature.
- The "main.md global roadmap" concept maps cleanly to our PlanTree
  root + obligation summary.

**Diverge**:
- GCC manages **agent context/memory**, not **plan trees**. Their
  COMMIT records "loose sequence of observation–thought–action," not
  a planned step's completion. Our COMMIT marks a tree node done.
- GCC is **single-task** only. We extend to cross-task via the
  librarian's plan-tree index.
- GCC records **past branches only**. We record both past (completed)
  and future (pending) branches.

**Concrete change to our op table (§4)**:

| Our current name | GCC name we adopt | Semantics |
|---|---|---|
| `mark_complete` | `COMMIT` | Checkpoint node as done |
| `mark_active` | `CHECKOUT` | Move attention to a node |
| `branch` | `BRANCH` | Open alternative subtree |
| (new) | `MERGE` | Fold a successful branch back into main |
| `add_after` | `COMMIT_NEW` | Append a new sibling node |
| `prune` | `DROP` | Abandon a subtree (history kept) |
| `refine` | `REFINE` (no git analog) | Split a leaf into substeps |

### 14.2 SWE-Exp, arxiv 2507.23361

**What it is**: an experience bank for SWE issues that stores both
successful AND failed attempts. Transfers reusable issue-resolution
knowledge by retrieving prior task summaries.

**Borrow**:
- **Store FAILED plan trees separately from PASSED ones.** Today our
  design implicitly assumes the librarian stores PASS trees. SWE-Exp
  shows that failed-attempt patterns are equally valuable — they tell
  the next agent which subtrees to avoid.
- The success/failure split should be **first-class in the librarian
  schema**: `plan_tree_templates(obligation_class, outcome=PASS)` and
  `plan_tree_anti_templates(obligation_class, outcome=FAIL)`.

**Diverge**:
- SWE-Exp stores task **summaries**, not tree **structures**. We keep
  full trees so we can do structural retrieval (tree-shape similarity),
  not just text similarity.

**Concrete change**: §7 librarian roles get a 4th job — **anti-template
provider**: given obligation_class, return K prior FAIL plan trees so
the Stipulator can avoid known dead-ends.

### 14.3 LATS — Language Agent Tree Search (ICML 2024), arxiv 2310.04406

**What it is**: MCTS over LLM actions. Uses an LLM-powered value
function + self-reflection for exploration and backtracking. Trees are
within-task and discarded.

**Borrow**:
- **Self-reflection on FAIL**: LATS uses an LLM reflection step when a
  path fails. We can fold this into our surprise→tree-op mapping: on
  high-surprise events, before applying a tree-edit, an LLM reflection
  pass writes a short note that gets attached to the BRANCH/DROP op.
  This makes the librarian's anti-template entries richer.

**Diverge**:
- LATS does **MCTS within a task**; we do **Dijkstra over a persistent
  cross-task library**. Different objective (per-task value vs. global
  shortest path), different scope.
- LATS's value function is LLM-only. Ours is deterministic-first
  (mechanical surprise dimensions) with LLM attestor as fallback.

**Concrete change**: tree-edit op payloads get an optional
`reflection: str` field, written by an LLM call when surprise is high
(e.g., cost z > 3 or verifier-FAIL loop). Stored with the op. Cost:
one extra LLM call per high-surprise iter (~5-15% of iters).

### 14.4 SWE-Search, arxiv 2410.20285

**What it is**: MCTS specifically for SWE-bench. Hybrid value function
combining LLM qualitative judgment + numerical scores.

**Borrow**:
- **Validates SWE-bench as the testbed for this kind of design** —
  multiple groups converging here.
- **Hybrid value-function idea** could later combine our deterministic
  surprise (numerical) with attestor reasoning verdict (qualitative).

**Diverge**:
- SWE-Search is within-task MCTS. We're cross-task persistent tree
  library.

**Concrete change**: none for now. Validates direction; defer hybrid
value function.

### 14.5 SWE-Bench-CL, arxiv 2507.00014

**What it is**: SWE-Bench Verified organized into **chronological
sequences** explicitly designed to evaluate transfer learning across
SWE tasks.

**Borrow**:
- **Use chronological eval methodology**. Today we evaluate swe10/swe30
  independently. For cross-task transfer to be testable, we need to
  evaluate IN ORDER, with the librarian growing between tasks. Adopt
  this as our cross-task evaluation harness.

**Diverge**: none — this is a benchmark, not a method.

**Concrete change**: §12 ("What's kept") needs an additional item: add
chronological run-order to `scripts/run_bench.sh` so the librarian
accumulates between tasks. New harness command:
`run_bench.sh --chronological --persist-librarian`.

### 14.6 ReAcTree, arxiv 2511.02424

**What it is**: hierarchical agent trees with control-flow nodes
(sequence, parallel, conditional). Each tree node may be its own
sub-agent. Episodic + working memory shared across nodes.

**Borrow**:
- **Validates our phase structure** as a real research direction.
- The **working memory shared across nodes** concept maps to our
  `state_class` accumulating across iters.

**Diverge**:
- ReAcTree's nodes can be sub-agents. We keep one agent + one tree.
  Hierarchical sub-agents add complexity we don't need yet.

**Concrete change**: none. Defer hierarchical sub-agents.

### 14.7 Tree-of-Code (ToC), arxiv 2412.14212

**What it is**: tree of code attempts with BFS over candidate code
solutions, deterministic execution as the value signal.

**Borrow**:
- **For mutate steps with multiple viable edits**, we can BFS over
  candidates and use the deterministic verifier (tests) as the
  selection signal. Defer for now; useful once we have multi-candidate
  mutate scenarios.

**Diverge**:
- ToC is purely tree-of-code-attempts. Our tree spans investigation,
  design, mutate, verify. Code is one of four intents.

**Concrete change**: none for now. Future: when a mutate step has
multiple viable str_replace candidates, BFS over them.

### 14.8 Other adjacents we don't use yet

- **Tree-GRPO** (arxiv 2509.21240): tree search for RL training. We're
  not training models; irrelevant unless we later fine-tune.
- **Agent Git** (GitHub MAS-Infra-Layer): LangGraph-based version
  control framework. We don't use LangGraph; conceptually aligned with
  GCC.

### 14.9 What's genuinely ours (after borrowing)

Even after pulling pieces from all the above:

1. **Persistent plan tree as both per-task AND cross-task structure** —
   nobody does this. GCC has persistence but not trees; SWE-Exp has
   trees-as-summaries but not structure; LATS has tree structure but
   no persistence.
2. **Past AND future branches recorded** as first-class. Existing work
   either records past (git-like) or explores future (tree-search) —
   not both unified.
3. **Dijkstra (offline math) over the merged cross-task tree library**.
   Nobody applies offline shortest-path over a multi-task corpus of
   plan trees + edge stats.
4. **CAT contract framing as the index** (`obligation_class`). Nobody
   uses contract-discipline as the key for plan-tree retrieval.
5. **Mechanical-first surprise** with structured `expects` field on
   each plan node. LATS uses LLM value functions; we use deterministic
   comparison first, LLM as fallback.

### 14.10 Summary of design changes from related work

| Change | Source | Where in this doc |
|---|---|---|
| Adopt COMMIT/CHECKOUT/BRANCH/MERGE/DROP names | GCC | §4 (op table) |
| Add `MERGE` op (synthesize successful branch into main) | GCC | §4 |
| Librarian stores both PASS and FAIL plan trees | SWE-Exp | §7 (4th role) |
| Tree-edit op payload includes optional LLM `reflection` | LATS | §4 (op schema) |
| Chronological eval harness | SWE-Bench-CL | §12 |

---

## 15. Next pinning question

Decisions pinned so far:
- ✓ §4 — op vocabulary (COMMIT, CHECKOUT, BRANCH, MERGE, DROP, COMMIT_NEW, REFINE)
- ✓ §6 — Q2 Stipulator output schemas (cold-start + in-loop)
- ✓ §6.3 — `reflection` required on every op
- ✓ §8.1 — Q4 obligation_class = L1 categorical (framework + subsystem-top-2)
- ✓ §8.5 — Q1 state_class = 9-component hash with bucketed counts

The design is now end-to-end specified. Remaining open questions:

- **Q3 (surprise trigger logic)** — OR-gate vs weighted. Can be answered
  with data; don't need to pin in advance.
- **Q5–Q9** — concerns rather than blockers; addressable in implementation.

**Next step is no longer a design pin** — it's deciding whether to start
writing code, and what's the smallest first slice that proves the design.
