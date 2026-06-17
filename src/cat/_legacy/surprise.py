"""Surprise vector — end-of-iter comparison of prediction vs actual.

Per docs/iteration.md §5.2 step 8. Six dimensions:
  cost_z          — z-score of cost vs librarian.predict_det
  state_match     — state_class change matches predicted next-state
  outcome_match   — observed outcome matches predicted outcome
  durable_match   — git diff post-action matches expects.durable_change
  files_match     — modified files ⊆ expects.files_modified
  verifier_match  — observed verifier outcome matches expects.verifier_outcome

Attestor verdict is the fallback prose comparator; not part of the vector
directly but provided alongside for the in-loop Stipulator prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SurpriseVector:
    cost_z: float = 0.0
    cost_surprised: bool = False
    state_match: bool = True
    outcome_match: bool = True
    durable_match: bool = True
    files_match: bool = True
    verifier_match: bool = True
    # Detail strings (for the in-loop Stipulator prompt)
    details: dict[str, str] = field(default_factory=dict)

    @property
    def any_surprised(self) -> bool:
        return (
            self.cost_surprised
            or not self.state_match
            or not self.outcome_match
            or not self.durable_match
            or not self.files_match
            or not self.verifier_match
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_z": round(self.cost_z, 2),
            "cost_surprised": self.cost_surprised,
            "state_match": self.state_match,
            "outcome_match": self.outcome_match,
            "durable_match": self.durable_match,
            "files_match": self.files_match,
            "verifier_match": self.verifier_match,
            "any_surprised": self.any_surprised,
            "details": self.details,
        }


def compute_surprise(
    expects: dict[str, Any],
    actual_cost: float = 0.0,
    predicted_cost_mean: float = 0.0,
    predicted_cost_std: float = 0.0,
    state_now: str = "",
    state_next: str = "",
    predicted_next_state: str = "",
    actual_outcome: Optional[str] = None,
    predicted_outcome: Optional[str] = None,
    actual_files_modified: "Optional[list[str]]" = None,
    actual_durable_change: bool = False,
    actual_verifier_outcome: Optional[str] = None,
    cost_z_threshold: float = 2.0,
) -> SurpriseVector:
    """Compare expected outcome to actual; return surprise vector."""
    sv = SurpriseVector()

    # 1. cost_z (only when we have a meaningful prediction)
    if predicted_cost_mean > 0:
        eps = max(predicted_cost_std, 1.0)
        sv.cost_z = abs(actual_cost - predicted_cost_mean) / eps
        sv.cost_surprised = sv.cost_z > cost_z_threshold
        if sv.cost_surprised:
            sv.details["cost"] = f"predicted ~{int(predicted_cost_mean)} ±{int(predicted_cost_std)}, actual {int(actual_cost)}"

    # 2. state_match
    if predicted_next_state:
        sv.state_match = (state_next == predicted_next_state)
        if not sv.state_match:
            sv.details["state"] = f"predicted next={predicted_next_state[:12]}, actual={state_next[:12]}"
    # else: no prediction → assume match (don't surprise on missing prediction)

    # 3. outcome_match
    if predicted_outcome and actual_outcome:
        sv.outcome_match = (actual_outcome == predicted_outcome)
        if not sv.outcome_match:
            sv.details["outcome"] = f"predicted={predicted_outcome}, actual={actual_outcome}"

    # 4. durable_match — compare against expects.durable_change
    expected_durable = bool(expects.get("durable_change", False))
    sv.durable_match = (actual_durable_change == expected_durable)
    if not sv.durable_match:
        sv.details["durable"] = (
            f"expected durable_change={expected_durable}, actual={actual_durable_change}"
        )

    # 5. files_match — actual files modified should be a subset of expected
    # (extra files modified is OK as long as expected ones changed)
    expected_files = set(expects.get("files_modified", []) or [])
    actual_files = set(actual_files_modified or [])
    if expected_files:
        sv.files_match = expected_files.issubset(actual_files)
        if not sv.files_match:
            missing = expected_files - actual_files
            sv.details["files"] = f"expected to modify {sorted(expected_files)[:3]}; missing {sorted(missing)[:3]}"
    # if no expected files, we don't check

    # 6. verifier_match
    expected_verifier = expects.get("verifier_outcome", "N/A")
    if expected_verifier != "N/A" and actual_verifier_outcome:
        sv.verifier_match = (actual_verifier_outcome == expected_verifier)
        if not sv.verifier_match:
            sv.details["verifier"] = (
                f"expected verifier={expected_verifier}, actual={actual_verifier_outcome}"
            )

    return sv
