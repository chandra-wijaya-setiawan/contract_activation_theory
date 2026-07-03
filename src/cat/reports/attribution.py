"""Layer attribution — credit/blame across the 8-step CAT loop."""
from __future__ import annotations

from dataclasses import dataclass


LAYERS = ("represent", "stipulate", "perform", "verify")
STATUSES = ("OK", "DEGRADED", "FAILED", "NA")


@dataclass
class Attribution:
    layer: str = ""
    status: str = "NA"
    reason: str = ""
    confidence: float = 0.0


def attribute_task(trace_path: str, mode: str = "llm") -> list[Attribution]:
    """Attribute outcome across layers for one task."""
    ...


def attribute_task_llm(trace_path: str) -> list[Attribution]:
    """LLM-driven attribution (higher signal on subtle failures)."""
    ...


def attribute_task_heuristic(trace_path: str) -> list[Attribution]:
    """Deterministic rule-based attribution (fallback)."""
    ...
