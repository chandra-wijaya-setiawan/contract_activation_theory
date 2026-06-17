"""state_class v2 — iter-level canonicalization (docs/iteration.md §8.5).

9-component hash with bucketed counts. Indexes edges in the librarian for
cross-run cost transfer. The OLD cat/state_class.py is preserved for v1.7
compatibility; this module is the v2 redesign.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunCounts:
    """Running counters tracked by the activator during a discharge loop."""
    investigate_count: int = 0
    mutate_count: int = 0
    verify_count: int = 0
    durable_change_count: int = 0
    iter_since_durable_change: int = 0
    last_outcome: str = "None"  # PASS | FAIL | ERR | None

    def bump(self, intent: str) -> None:
        if intent == "investigate":
            self.investigate_count += 1
        elif intent == "mutate":
            self.mutate_count += 1
        elif intent == "verify":
            self.verify_count += 1

    def mark_durable_change(self) -> None:
        self.durable_change_count += 1
        self.iter_since_durable_change = 0

    def tick_no_change(self) -> None:
        self.iter_since_durable_change += 1


# Bucket edges per docs/iteration.md §8.5
_INV_EDGES = [0, 1, 3, 6, 11]            # =0, 1-2, 3-5, 6-10, 11+
_MUT_EDGES = [0, 1, 2, 4]                # =0, 1, 2-3, 4+
_VER_EDGES = _MUT_EDGES
_DUR_EDGES = _MUT_EDGES
_ISD_EDGES = [0, 2, 6, 11]               # 0-1, 2-5, 6-10, 11+


def _bucket(value: int, edges: list[int]) -> int:
    """Map int to bucket index based on edges.

    Edges define inclusive lower bounds: edges=[0,1,3,6,11] →
        value=0 → 0
        value=1,2 → 1
        value=3,4,5 → 2
        value=6..10 → 3
        value=11+ → 4
    """
    if value < edges[0]:
        return 0
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return i
    return len(edges) - 1


def compute_state_class(
    obligation_class: str,
    phase_name: str,
    intent: str,
    counts: RunCounts,
) -> str:
    """Return the state_class hash (12 hex chars).

    See docs/iteration.md §8.5.
    """
    parts = [
        obligation_class,
        phase_name,
        intent,
        str(_bucket(counts.investigate_count, _INV_EDGES)),
        str(_bucket(counts.mutate_count, _MUT_EDGES)),
        str(_bucket(counts.verify_count, _VER_EDGES)),
        str(_bucket(counts.durable_change_count, _DUR_EDGES)),
        str(_bucket(counts.iter_since_durable_change, _ISD_EDGES)),
        counts.last_outcome or "None",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def compute_state_class_features(
    obligation_class: str,
    phase_name: str,
    intent: str,
    counts: RunCounts,
) -> dict[str, object]:
    """Hash + underlying features (for debugging / trace events)."""
    h = compute_state_class(obligation_class, phase_name, intent, counts)
    return {
        "obligation_class": obligation_class,
        "phase_name": phase_name,
        "intent": intent,
        "inv_b": _bucket(counts.investigate_count, _INV_EDGES),
        "mut_b": _bucket(counts.mutate_count, _MUT_EDGES),
        "ver_b": _bucket(counts.verify_count, _VER_EDGES),
        "dur_b": _bucket(counts.durable_change_count, _DUR_EDGES),
        "isd_b": _bucket(counts.iter_since_durable_change, _ISD_EDGES),
        "last_outcome": counts.last_outcome,
        "raw_counts": {
            "investigate": counts.investigate_count,
            "mutate": counts.mutate_count,
            "verify": counts.verify_count,
            "durable_change": counts.durable_change_count,
            "iter_since_durable_change": counts.iter_since_durable_change,
        },
        "hash": h,
    }
