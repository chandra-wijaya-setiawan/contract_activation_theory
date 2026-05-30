# v2.x — Cost-efficiency optimization layer (DEFERRED)

> Forward note originated from v1.6.6.13 cost-per-PASS analysis
> (2026-05-29).

## Background

v1.7-tools cost analysis revealed three structural sources of per-call
cost inflation. No single fix at the loop layer cleanly addresses them.
Together they form a coherent "cost-efficiency layer" that needs an
architectural answer in v2.x.

## The three sources

### 1. Static tool-schema overhead

Every performer LLM call carries all 14 tool schemas (~1KB of JSON),
even when the contract only needs 3 of them.

**Fix:** capability granting at the admission gate (Step 4). Only
register tools the contract genuinely requires. Co-developed with v1.7
procedural memory which learns the contract → required-tool mapping
from past DAGs.

### 2. History accumulation through context

By turn 10, the LLM re-reads 9 prior tool results every call. CAT today
has no in-context compaction beyond gross turn-summary squashing.

**Fix:** tool-result summarization at history-write time. Replace
verbose results with their salient fact once the agent has moved on
from them. Likely a new attestor-adjacent role that compresses tool
outputs into "what mattered" before they go into history.

### 3. LLM tool-selection bias

The LLM defaults to `read` (familiar from pretraining) over `outline`
(cheaper, less familiar), even when outline would dominate. 1.6.6.14
patches with a description nudge.

**Principled fix:** cost-aware multi-candidate generation at Step 5b.
The Dijkstra ADVISOR could score candidates partly on expected-bytes-out,
not just on plan fit.

## Why a unified "layer"

These three want each other's infrastructure:

- Capability granting needs the corpus to learn which tools are needed
  per contract class (v1.7+ procedural memory)
- History compaction needs the attestor's judgment about what's relevant
- Cost-aware scoring needs both: corpus telling us what's been wasteful
  AND attestor telling us what's salient

None ships before v1.7+/v1.8. The placeholder name is intentionally bland:
this is not premature optimization, it's a *known* tax we're carrying
that needs an architectural answer in v2.x.
