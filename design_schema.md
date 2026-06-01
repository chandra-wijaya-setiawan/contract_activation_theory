# Schema Library Design

Single source of truth for the data shapes that flow between layers in CAT.
This document is the contract: any new Pydantic model added to the project
must follow the conventions here.

Companion to `DESIGN.md` (architecture) and `docs/iteration.md` (loop semantics).

---

## 1. Why this exists

Three different categories of name share the project's namespace today, with
no consistent suffix to tell them apart:

| Category | Examples | Pattern today |
|---|---|---|
| **Roles** (active components, LLM-call sites) | `Stipulator`, `Performer`, `Attestor`, `Resolver`, `PlanPatcher`, `Activator` | nouns, no suffix |
| **Schemas** (Pydantic data shapes) | `PlanStep`, `PlanNode`, `StepExpects`, `ToolInvocation`, `PerformerOutcome`, `AttestorVerdict`, `ResolverVerdict`, `RevisorOps` | inconsistent — mix of "Step", "Node", "Outcome", "Verdict", plain |
| **Operations** (tree-edit verbs) | `CommitOp`, `RefineOp`, `BranchOp`, `DropOp`, `MergeOp`, `CommitNewOp`, `CheckoutOp` | `...Op` suffix (good — keep) |

The schema category is the messy one. Renaming during R26 establishes one
clear rule: **every Pydantic data shape ends in `Schema`.** Operations stay
with `...Op`. Roles stay without suffix.

---

## 2. Naming convention

### Rule 1 — Roles: noun, no suffix

`Stipulator`, `Performer`, `Attestor`, `Resolver`, `PlanPatcher`, `Activator`,
`Representer`. These are *components* that do work (typically backed by an
LLM call). They're code, not data.

### Rule 2 — Schemas (Pydantic, validated): noun + `Schema` suffix

Every `BaseModel` subclass ends in `Schema`. This makes a glance at a class
name tell you "this is a Pydantic data shape with validation, safe to
serialize / cross trust boundaries."

### Rule 3 — Runtime objects (dataclasses, no validation): noun, no suffix

Plain `@dataclass`-decorated containers — `Contract`, `State`, `Cost`,
`LLMResult`, `Capability`, `ToolResult`, `Criterion`, `Evidence`,
`Constraints`, `Representation` — stay as nouns. They're in-memory
runtime state, not crossing trust boundaries. Mixing schema validation
into them would be premature.

### Rule 4 — Operations: verb + `Op` suffix

Tree-edit ops emitted by the PlanPatcher: `CommitOp`, `RefineOp`, `DropOp`,
`BranchOp`, `MergeOp`, `CommitNewOp`, `CheckoutOp`, plus the new
`UpdateStepOp` (R27). Each `Op` is itself a `Schema` (Pydantic), but its
verb-shaped role earns the `Op` suffix instead of `Schema` for readability.

### Rule 5 — File locations

- `cat/schemas.py` — single library file for all schemas (start here; split
  into a subpackage if it grows past ~600 lines).
- Roles live in their own files (`cat/resolver.py`, `cat/activator.py`,
  `cat/representer.py`, etc.) and *import from `cat.schemas`* when they
  need a data type.
- Runtime-object dataclasses stay in their natural home
  (`cat/contracts.py`, `cat/capabilities.py`, `cat/llm.py`).

---

## 3. Migration map (rename table) — full inventory

Includes every `BaseModel` subclass in the repo. Dataclasses are unchanged.

### Plan-graph schemas (cat/plan_schema.py → cat/schemas.py)

| Current name | New name | Rationale |
|---|---|---|
| `PlanStep` | **`ExecutionSchema`** | The execution contract — what action to perform with which tool & args. Reads better as `node.execution`. |
| `PlanNode` (dataclass, becomes Pydantic) | **`NodeSchema`** | A tree position carrying an `ExecutionSchema`. State + execution. |
| `StepExpects` | **`ExpectsSchema`** | Post-action predictions. |
| `Phase` | **`PhaseSchema`** | A grouping of executions. |
| `PhasedPlan` | **`PhasedPlanSchema`** | The Stipulator's full output. |

### Performer-turn schemas

| Current name | New name | Rationale |
|---|---|---|
| `ToolInvocation` | **`InvocationSchema`** | One tool call record within an outcome. |
| `PerformerOutcome` | **`OutcomeSchema`** | One Performer turn result. Disambiguated from `Outcome` below by suffix. |

### Verdict schemas (LLM-judge outputs)

| Current name | New name | Rationale |
|---|---|---|
| `AttestorVerdict` | **`AttestorSchema`** | What the Attestor LLM returns. |
| `ResolverVerdict` | **`ResolverSchema`** | What the Resolver LLM returns. |
| `AttributionVerdict` | **`AttributionSchema`** | What the auditor LLM returns. |
| `LayerStatus` | **`LayerStatusSchema`** | One per-layer entry within `AttributionSchema`. |

### PlanPatcher ops (cat/schemas.py)

| Current name | New name | Rationale |
|---|---|---|
| `_PlanPatcherOpBase` | **`_OpBaseSchema`** | Internal base, underscore-private. |
| `PlanPatcherOps` | **`PatcherSchema`** | The PlanPatcher's full emission (list of ops). |
| `CommitOp`, `CheckoutOp`, `DropOp`, `CommitNewOp`, `RefineOp`, `BranchOp`, `MergeOp` | keep as-is | Already follow `...Op` rule. |
| (new) | **`UpdateStepOp`** | R27 — refine `node.execution` in place. |

### Trace schemas (cat/trace.py — also renamed)

| Current name | New name | Rationale |
|---|---|---|
| `TraceEvent` | **`TraceEventSchema`** | Already Pydantic; suffix added for consistency. |
| `Trajectory` | **`TrajectorySchema`** | A trace event collection. |

### Task / outcome schemas (cat/tasks.py)

| Current name | New name | Rationale |
|---|---|---|
| `BudgetCaps` | **`BudgetCapsSchema`** | Token / call / wall-clock limits. |
| `OracleSpec` | **`OracleSpecSchema`** | Oracle test configuration. |
| `TaskSpec` | **`TaskSpecSchema`** | One benchmark task definition. |
| `Outcome` | **`TaskResultSchema`** | Final task outcome (PASS/FAIL + metrics). Disambiguated from `OutcomeSchema` (per-iter Performer turn). |

### Unchanged (dataclasses, runtime objects)

- `cat/contracts.py`: `Contract`, `ContractState`, `State`, `Cost`,
  `Constraints`, `Criterion`, `Evidence`, `Representation` — runtime
  containers, no rename.
- `cat/capabilities.py`: `Capability`, `ToolResult`,
  `CapabilityRegistry` — runtime, no rename.
- `cat/llm.py`: `LLMResult` — runtime, no rename.
- `cat/activator.py`: `ActivatorRun`, `_PlanStep` (legacy mirror, may be
  deletable post-R26) — runtime, no rename for now.

**Touch count audit**: ~18 Pydantic classes renamed; ~10 runtime dataclasses
untouched.

### Decision: `node.execution` not duplicated fields

`NodeSchema` does **not** duplicate `description`, `intent`,
`suggested_tool`, `suggested_args`, `expects`. It nests `execution:
ExecutionSchema`. Single source of truth.

```python
class ExecutionSchema(BaseModel):
    description: str
    intent: Literal["investigate", "design", "mutate", "verify", "other"]
    suggested_tool: Literal[...] | None
    suggested_args: dict
    expects: ExpectsSchema

class NodeSchema(BaseModel):
    # tree position
    id: str
    parent_id: Optional[str]
    phase_name: str
    status: Literal["pending", "active", "done", "dropped", "branched"]
    children: list[str]
    created_at_iter: int
    completed_at_iter: Optional[int]
    completion_reason: Optional[str]
    branch_of: Optional[str]
    # execution contract
    execution: ExecutionSchema
```

Access:
- `node.execution.suggested_tool` (planner concern)
- `node.execution.expects.durable_change` (planner concern)
- `node.status` (tree concern)
- `node.children` (tree concern)

---

## 4. Lifecycle of an `ExecutionSchema`

| Stage | Who creates / updates | Mechanism |
|---|---|---|
| Cold-start plan | Stipulator | `call_stipulator_phased` → `PhasedPlanSchema` (list of `PhaseSchema(steps: list[ExecutionSchema])`) |
| Tree build | Activator | `PlanTree.from_phased_plan(...)` wraps each `ExecutionSchema` in a `NodeSchema` |
| Mid-loop refinement | PlanPatcher (via `UpdateStepOp`) | `node.execution = new_execution_schema` — Pydantic-validated assignment |
| Mid-loop create | PlanPatcher (via `CommitNewOp`) | constructs a new `NodeSchema` wrapping a new `ExecutionSchema` |
| Branch / refine | PlanPatcher (via `BranchOp`, `RefineOp`) | each substep/alt-step is an `ExecutionSchema` |

**Every emission of a step's content is an `ExecutionSchema`.** Same
validation runs in every case. R19.9 auto-fill of `suggested_tool` fires
identically whether the Stipulator emitted the step or the PlanPatcher
created it via `UpdateStepOp`.

---

## 5. Outcome flow (what the Performer produces)

```
PerformerCall → OutcomeSchema {
    text_response: str
    invocations: list[InvocationSchema]
    finish_reason: str
    tokens_in, tokens_out: int
}

InvocationSchema {
    tool_name, args, succeeded, output_text, error,
    durable_change, files_modified
}
```

`OutcomeSchema` is consumed by:
- `_observe_phase` → produces `SurpriseVector` (the typed surprise dims)
- `_attestor_verdict` → reads `outcome.text_response` + last invocation's output (R19.8)
- `_invoke_resolver` → reads accumulated outcomes for intent_counts (R19.7)
- `performer_response` trace event → for offline diagnosis (R19.10)

---

## 6. Verdict schemas (what LLM judges return)

| Layer | Schema | Key fields |
|---|---|---|
| Attestor | `AttestorSchema` | `complete: bool`, `reason: str` |
| Resolver | `ResolverSchema` | `decision: Literal["forward", "patch_plan", "backward", "give_up"]`, `reason`, `diagnosis`, `next_instruction` |
| Auditor (post-task) | `AttributionSchema` | per-layer `LayerStatusSchema` + diagnostic fields |

These are all Pydantic-validated at the LLM boundary. Retry-on-correction
handles malformed emissions.

---

## 7. PlanPatcher emission (`PatcherSchema`)

The PlanPatcher emits a list of typed ops. Each op is a discriminated-union
class inheriting from `_OpBaseSchema`:

```python
class PatcherSchema(BaseModel):
    ops: list[CommitOp | CheckoutOp | DropOp | CommitNewOp |
              RefineOp | BranchOp | MergeOp | UpdateStepOp]
```

Each op carries:
- The verb (`op: Literal["COMMIT"]` etc., the discriminator)
- The targets (`node_id`, `parent_id`, `branch_id`, `target_id`)
- The payload (e.g. `step_patch: dict` for `UpdateStepOp`, `substeps:
  list[ExecutionSchema]` for `RefineOp`)
- `reflection: str` (mandatory — why)

### New op: `UpdateStepOp`

```python
class UpdateStepOp(_OpBaseSchema):
    op: Literal["UPDATE_STEP"]
    node_id: str
    step_patch: dict  # partial ExecutionSchema fields; merged via model_copy
    reflection: str
```

Apply semantics:
```python
node = tree.nodes[op.node_id]
new_execution = node.execution.model_copy(update=op.step_patch)
node.execution = new_execution  # Pydantic revalidates; R19.9 auto-fill fires
```

This is the surgical fix for the bad-args case (django-13315 trial 1):
the PlanPatcher sees `node.execution.suggested_args.old_str = "<hallucinated>"`
plus the read output that revealed the actual code, and emits:

```json
{
  "op": "UPDATE_STEP",
  "node_id": "C1",
  "step_patch": {
    "suggested_args": {
      "path": "/testbed/django/db/models/fields/related.py",
      "old_str": "<actual code from read>",
      "new_str": "<actual code with .distinct()>"
    }
  },
  "reflection": "C1's str_replace failed because Stipulator's old_str was a hallucination; refining args from the file content learned at iter 13."
}
```

---

## 8. Per-PlanPatcher input visibility (R28)

`render_outline()` today returns a sparse text view of the tree —
only `status_marker + node_id + intent + description[:80]`. After R28
it surfaces:

```
[>] C1 (mutate, tool=str_replace) Modify formfield to add .distinct()
    args: {"path": "...", "old_str": "if isinstance(limit_choices_to, Q):...", "new_str": "..."}
    expects: durable_change=True, files=["..."], result="..."
```

Combined with the typed `OutcomeSchema` access added in R19.7-8, the
PlanPatcher gets sufficient context to make `UpdateStepOp` decisions
rather than DROP+COMMIT_NEW.

---

## 9. Backward compatibility

For older trace files emitted under previous names (R10, R11, etc.):
- Trace events stored on disk are JSONL with `kind` strings — those are
  already values, not class names. No impact.
- Pydantic class names are internal — no on-disk encoding.
- The class-name rename is a pure code refactor. Tests that import
  `from cat.plan_schema import PlanStep` will need updating to
  `from cat.schemas import ExecutionSchema`.

Provide a temporary back-compat shim in `cat/plan_schema.py`:

```python
# cat/plan_schema.py — kept as a re-export shim until external users migrate
from cat.schemas import (
    ExecutionSchema as PlanStep,
    NodeSchema as PlanNode,
    ExpectsSchema as StepExpects,
    PhaseSchema as Phase,
    PhasedPlanSchema as PhasedPlan,
    InvocationSchema as ToolInvocation,
    OutcomeSchema as PerformerOutcome,
    AttestorSchema as AttestorVerdict,
    ResolverSchema as ResolverVerdict,
    AttributionSchema as AttributionVerdict,
    PatcherSchema as PlanPatcherOps,
    # ...
)
```

Delete the shim after one release cycle.

---

## 10. Execution checklist (R26)

Ordered for minimum breakage. After each step, run `pytest tests/
--ignore=tests/_legacy`. Commit at each green checkpoint.

### Step 1: New schemas file (additive only)
- [ ] Create `cat/schemas.py` with ALL new-named classes
- [ ] `cat/plan_schema.py` becomes a re-export shim (back-compat alias)
- [ ] Run tests — all should pass (nothing in production code touches the
      new names yet, but imports of old names go through the shim)

### Step 2: Rename `NodeSchema` (the duplicated-fields fix)
- [ ] Refactor `PlanNode` (in `cat/plan_tree.py`) to wrap an
      `ExecutionSchema` instead of duplicating fields
- [ ] Update `PlanTree.from_phased_plan`, `_op_*` handlers to construct
      via `NodeSchema(execution=...)`
- [ ] Update internal accessors: `node.intent` → `node.execution.intent`,
      `node.suggested_tool` → `node.execution.suggested_tool`,
      `node.expects` → `node.execution.expects`,
      `node.description` → `node.execution.description`
- [ ] Test suite

### Step 3: Cascade access patterns through consumers
- [ ] `cat/activator.py` — every reference to old fields gets `.execution.`
- [ ] `cat/resolver.py` — same
- [ ] `cat/plan_composer.py` — same (scoring reads `node.execution.intent` etc.)
- [ ] Test suite

### Step 4: Trace event payloads (in `cat/activator.py`)
- [ ] `contract_stipulate` payload now serializes `node.execution.model_dump()`
- [ ] `performer_response` payload reads `attention_node.execution.suggested_tool` etc.
- [ ] Test suite

### Step 5: Rename schema classes (find/replace per file)
- [ ] In `cat/schemas.py`: rename in the file itself
- [ ] Re-export shim in `cat/plan_schema.py` covers the old names
- [ ] All consumer files import from `cat.schemas` directly
- [ ] Tests use the new names (mass-replace imports)
- [ ] Test suite

### Step 6: Delete shim
- [ ] Remove `cat/plan_schema.py` once no imports reference old names
- [ ] Final test suite + smoke test on 1 swe task
- [ ] Commit

### Step 7: Smoke + swe10×5
- [ ] Smoke 1 task (django-10880 — most-tested) end-to-end
- [ ] If clean, swe10×5 for measurement

Touch count budget: ~200 lines total. All mechanical. Test suite + 1
smoke trial is the safety net at each step.

---

## 11. Roadmap items unblocked by this refactor

1. **R27 — `UpdateStepOp`** (the bad-args fix from django-13315 analysis)
2. **R28 — enrich `render_outline`** to surface `node.execution.suggested_tool/args/expects`
3. **R29 — `ExecutionSchema.action` regroup** (optional): move `suggested_tool` + `suggested_args` into a nested `action: ActionSchema` inside `ExecutionSchema`, distinguishing pre-action recommendation from post-action `expects` predictions
4. **R30 — tool effects metadata** (already in roadmap.md, deferred)

---

## 12. Open questions

1. Should `ActionSchema` and `ExpectsSchema` be siblings under
   `ExecutionSchema` (input/output prediction symmetry)? Open for R29.
2. Should `OutcomeSchema` be persisted to a separate `OutcomeStore` for
   offline analysis (instead of just trace events)? Defer.
3. Future schemas (cross-task `LibrarianEntrySchema`, `BenchmarkRunSchema`)
   follow the same `Schema` suffix rule. Mark in roadmap.
