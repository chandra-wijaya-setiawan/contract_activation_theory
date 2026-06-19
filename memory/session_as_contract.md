---
name: session-as-contract
description: Design idea — chat session as a meta-contract with turn sub-contracts; foundation for sub-agent orchestration
metadata:
  type: project
---

User proposed: chat session = one open orchestrating contract; each turn = sub-contract discharged within it.

**Why:** workspace probes, history, and prior outcomes are already part of the parent contract's evidence — not a bolt-on history list. Sub-agents are just contracts dispatched by an orchestrating contract.

**Why:** current design creates one Contract per turn (fresh). History is carried separately as `chat_history`. Workspace reps re-run per turn (just fixed with session-level cache as a stopgap).

**Tension to resolve before implementing:**
- CAT contracts are designed to close (fulfilled/failed). A session contract is intentionally open-ended — it advances incrementally across turns.
- Options: (a) session as orchestrator that *generates* turn-contracts, never a contract itself; (b) extend contract lifecycle to support "open" state with incremental obligations.

**How to apply:** Do NOT implement this yet. Requires a design note with schema changes before touching code. User said think deeper first.
