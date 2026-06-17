# v2.x — Procedural memory / automaticity (DEFERRED)

## What

Task classification + cued retrieval of past DAGs as reusable skills.
Brain analogy: declarative (in-context per run) vs procedural (DAG corpus
across runs). See DESIGN.md "Declarative vs procedural memory" section.

## Predicted observable

Token cost per task drops on recurring patterns as the agent develops
automaticity. Requires v1.7 DAG infrastructure first (now in place via
A.1 corpus).

## What v1.7 already covers

The Phase A LIBRARIAN is effectively a single-step procedural memory:
state → action recommendation. Phase A.2 (ADVISOR + ELECTION) makes that
recommendation load-bearing. **Procedural memory in the brain sense**
(consolidating across many runs into compressed, reusable patterns) is
the next level up.

## Concrete extensions toward full procedural memory

1. **Multi-step path retrieval** — instead of "next action," LIBRARIAN
   returns a 3-5-step trajectory fragment from a successful past run
   that matches the current state.

2. **Task-class clustering** — group obligations by sentence-transformer
   similarity; learn predicate trees PER CLUSTER so cross-task transfer
   doesn't dilute signal.

3. **Consolidation step** — periodically distill the corpus: prune
   trajectories whose subgraphs are subsumed by cheaper alternatives;
   tag "canonical skill" trajectories that are reference paths.

4. **Skill-as-name** — give frequently-used trajectory fragments human-
   readable names ("django-orm-distinct-fix"); LLM can reference them
   directly in plans.

## Open question

Does CAT need a separate "skill execution mode" where the agent commits
to executing a known skill end-to-end without per-step LLM calls? That's
the procedural-memory promise but it conflicts with the contract
discipline (skipping CRITERIA check). Probably not — corpus-informed
single-step recommendations are the more honest framing.
