# CAT — Claude Code Guidelines

## ⚠️ CRITICAL: Domain-Agnostic Framework

**CAT is a domain-agnostic agent framework.** It must work for SWE-bench, CRM, web automation, database, API tasks, and any future domain.

**NEVER put domain-specific logic in:**
- System prompts (`STIPULATOR_SYSTEM_PROMPT`, `PERFORMER_SYSTEM_PROMPT`, `ATTESTOR_SYSTEM_PROMPT`)
- `cat/engine/activator.py` or any core framework file
- Schemas or trace events

**Domain-specific knowledge belongs ONLY in:**
- Task adapters (`benchmarks/swe_bench.py` → `TbSweAgent`)
- `PerformerTaskSchema.context` field (injected per task)
- `contract.representations` (auto-discovered by Representer)

**Examples of violations to avoid:**
- ❌ "call str_replace" in prompts → ✅ "apply the change"
- ❌ "read the file" in prompts → ✅ "inspect the target"  
- ❌ Checking for `bash/rg/read` as read-only tools → ✅ Use `inv.durable_change`
- ❌ Mentioning `FOUND_IN`/`NOT_FOUND` in prompts → ✅ Use knowledge generically
- ❌ "old_str not found" special-casing → ✅ "modification tool errored"

## Architecture

See `design.md` for the architecture.

Key entry point: `cat.engine.activator.HypothesisLoopActivator`  
Public alias: `cat.activator.Activator`

## Running tests

```bash
python -m pytest tests/
scripts/run_bench.sh swebench --run-id smoke --task-id django__django-11239 --concurrency 1 --n-attempts 1
```

## Model escalation tiers

```
_ESCALATION_TIERS in cat/engine/activator.py:
  Tier 0: CAT_MODEL (default: deepseek-v4-flash)
  Tier 1: CAT_ESCALATION_MODEL_T1 (default: deepseek-v4-pro)
  Tier 2: CAT_ESCALATION_MODEL_T2 (default: claude-haiku-4-5)
```

give_up at any tier → escalate to next tier. give_up at max tier → task fails.

## Docker

All benchmark runs need `DOCKER_HOST=ssh://cpubox`. Source `.env` first.
