# v2.x — Cross-domain working memory via two-tier features (DEFERRED)

> Forward note originated from v1.7.0 simulation analysis (2026-05-30).

## Background

v1.7 builds the offline-RL + online-search loop on swe10 (code domain).
The framework GENERALIZES to other domains (tau-bench, WebArena, OSWorld)
only if the canonicalization separates **domain-agnostic core features**
from **domain-specific extensions**.

## The two-tier feature set

### Domain-agnostic core (every domain populates these)

- `obligation_hash`, `plan_idx`, `last_outcome`
- `durable_change_count`, `iter_since_durable_change`
- `verify_attempt_count`, `investigate_attempt_count`, `mutate_attempt_count`
- `n_unique_actions`, `n_unique_errors`
- `action_intent ∈ {investigate, mutate, verify, other}`

### Domain-specific extensions (sparse dict)

- code-Python: `{workdir_sig, env_facts, last_bash_class}`
- tau-bench:   `{api_endpoint_class, db_table_touched, ...}`
- WebArena:    `{dom_section_class, last_form_id, ...}`

## How the math benefits

Each Capability registration declares an `intent` classification. The
CEGAR predicate tree learns predicates over domain-agnostic features
primarily — `verify_attempt_count > 3` fires identically in swe10 AND
tau-bench, catching domain-independent failure modes (the pytest-loop
pattern, but generalized).

- LIBRARIAN does cross-domain *action-level* transfer via embedding
- Predicate tree does cross-domain *reasoning-pattern* transfer via
  shared features

## v1.7.0 evidence

After v1.7.0.x two-tier refactor, the CEGAR loop on swe10 picked **22 of
30 splits over domain-agnostic features (73%)**. The 8 remaining
domain-specific splits live at depth 2-4 — they capture genuine code-
specific refinement that core features can't express, and they're cleanly
isolated in `domain_features` so a future tau-bench port wouldn't carry
them mistakenly.

## Open question deferred

Whether the cognitive layers themselves (REPRESENT / STIPULATE / PERFORM
/ VERIFY) need cross-domain abstraction. For now: each domain reuses the
same layer SHAPES but populates them with domain-specific Capabilities.
If a second domain creates pressure (REPRESENT probes need to differ
fundamentally — e.g., web DOM probing vs filesystem inspection), this
gets revisited.
