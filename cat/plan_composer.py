"""PlanComposer — generates K candidate plans, scores them, picks the best.

The Stipulator emits ONE plan today. The variance of that single plan
dominates agent-loop variance (we've measured ±20% on swe10 from single-
sample stipulation). This module generates K candidate plans via persona-
driven prompts (conservative / minimal / exploratory), scores each
deterministically against the obligation, and returns the highest-scoring
plan. The discarded candidates are kept in the trace for offline analysis.

Architectural shape (extension points are deliberate, see docs/roadmap.md):

  PlanGenerator (ABC)         — emits N candidates
    └── PersonaStipulator     — current: persona-driven multi-sample

  PlanScorer (ABC)            — scores a plan
    └── HeuristicPlanScorer   — current: deterministic boolean+count metrics
    └── (future) LearnedScorer        — trained on librarian corpus
    └── (future) LLMJudgeScorer       — LLM ranks the candidates

  PlanComposer                — orchestrator: Generator × Scorer → winner
                                Single configurable surface for the activator.

Future parameters (NOT implemented yet, in roadmap.md):
  - optimize_for: Literal["cost", "quality", "speed", "balanced"]
  - intelligence_level: Literal["fast", "mid", "deep"]
  - persona_weights: per-task-type persona ranking
  - K: number of candidates (today 3, fixed)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any

from cat.contracts import Contract
from cat.llm import LLM, LLMResult, call_stipulator_phased


# ────────────────────────────────────────────────────────────────────────────
# Personas — different planning mindsets for the Stipulator
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PersonaSpec:
    """One planner mindset. Threaded into the Stipulator system prompt
    as an additional user-side directive."""
    name: str
    instruction: str
    temperature: float = 0.1


PERSONAS: dict[str, PersonaSpec] = {
    "conservative": PersonaSpec(
        name="conservative",
        instruction=(
            "Build a CAREFUL plan: small steps, thorough investigation before "
            "mutating, defensive verification. Optimize for reliability over "
            "speed. 8–15 steps."
        ),
    ),
    "minimal": PersonaSpec(
        name="minimal",
        instruction=(
            "Build the SHORTEST plan that solves the obligation. If the "
            "obligation already names the file and the fix is straightforward, "
            "go directly from a minimal probe → mutate → verify. 3–5 steps."
        ),
    ),
    "exploratory": PersonaSpec(
        name="exploratory",
        instruction=(
            "Build a plan that explicitly considers MULTIPLE approaches at "
            "uncertain decision points. Use BRANCH-friendly intent labelling. "
            "Optimize for not getting stuck on a wrong assumption. 6–10 steps."
        ),
    ),
}


# ────────────────────────────────────────────────────────────────────────────
# Scoring — deterministic plan quality metrics
# ────────────────────────────────────────────────────────────────────────────


class PlanScorer(ABC):
    """Score a plan against an obligation. Higher = better."""

    @abstractmethod
    def score(self, plan: dict, obligation: str, contract: Contract) -> float:
        ...


class HeuristicPlanScorer(PlanScorer):
    """Deterministic scoring — no LLM call. Cheap, reproducible, debuggable.

    Rubric:
      Hard fails (return -inf):
        - no verify-intent step
        - no mutate-intent step

      Soft signal (higher is better):
        - concrete suggested_tool present on each step
        - expects.result is specific (> 30 chars, distinctive verbs)
        - mutate steps name expected files_modified
        - plan size is in the goldilocks zone (3-12 steps)
        - phase shape matches expected MAP→IMPLEMENT→VERIFY flow

    Each axis is normalized so heterogeneous plan shapes can be compared.
    """

    HARD_FAIL = float("-inf")

    def score(self, plan: dict, obligation: str, contract: Contract) -> float:
        all_steps: list[dict] = []
        for phase in plan.get("phases", []):
            all_steps.extend(phase.get("steps", []))
        if not all_steps:
            return self.HARD_FAIL

        # Hard requirements (verify-required is already enforced by Pydantic,
        # but we keep the check here so the scorer is self-contained).
        has_verify = any(s.get("intent") == "verify" for s in all_steps)
        has_mutate = any(s.get("intent") == "mutate" for s in all_steps)
        if not has_verify or not has_mutate:
            return self.HARD_FAIL

        n = len(all_steps)

        # Concreteness: fraction of steps with a concrete suggested_tool
        tool_concrete_frac = sum(1 for s in all_steps if s.get("suggested_tool")) / n

        # Specificity: fraction of steps whose expects.result reads like a falsifiable claim
        def _is_specific(result: str) -> bool:
            if not result or len(result) < 30:
                return False
            # weak: long but generic ("we have understood the file")
            # strong: mentions identifiers / paths / specific outcomes
            specific_tokens = ("OR ", " in ", "/", "exits", "returns", "raises")
            return any(t in result for t in specific_tokens)

        specific_frac = sum(
            1 for s in all_steps
            if _is_specific((s.get("expects") or {}).get("result", ""))
        ) / n

        # Mutate steps with files_modified named
        mutate_steps = [s for s in all_steps if s.get("intent") == "mutate"]
        mutate_w_files = sum(
            1 for s in mutate_steps
            if (s.get("expects") or {}).get("files_modified")
        )
        mutate_specificity = mutate_w_files / max(len(mutate_steps), 1)

        # Size goldilocks: 5-10 steps optimal, penalize outside
        size_penalty = 0.0
        if n < 3:
            size_penalty -= 2.0
        elif n > 14:
            size_penalty -= 0.5 * (n - 14)

        # Phase coherence: investigate→mutate→verify ordering bonus.
        phase_names = [p.get("name") for p in plan.get("phases", [])]
        ordered_bonus = 0.0
        ordering_priority = {"MAP": 0, "DESIGN": 1, "IMPLEMENT": 2, "VERIFY": 3}
        prio_seq = [ordering_priority.get(n, 4) for n in phase_names]
        if prio_seq == sorted(prio_seq):
            ordered_bonus += 1.0

        return (
            5.0 * tool_concrete_frac +
            5.0 * specific_frac +
            4.0 * mutate_specificity +
            ordered_bonus +
            size_penalty
        )


# ────────────────────────────────────────────────────────────────────────────
# Generator — produces N candidate plans
# ────────────────────────────────────────────────────────────────────────────


class PlanGenerator(ABC):
    """Generates one or more PhasedPlan candidates from an obligation."""

    @abstractmethod
    def generate(
        self,
        obligation: str,
        contract: Contract,
        representations: "Optional[list[Any]]" = None,
        plan_templates: "Optional[list[dict]]" = None,
        anti_templates: "Optional[list[dict]]" = None,
        parent_event_id: Optional[str] = None,
    ) -> list[tuple[dict, LLMResult, PersonaSpec]]:
        """Return list of (plan_dict, llm_result, persona_used).
        Caller charges each llm_result to the contract budget."""
        ...


class PersonaStipulator(PlanGenerator):
    """Runs the Stipulator once per persona; returns all valid candidates."""

    def __init__(
        self,
        llm: LLM,
        personas: Optional[list[PersonaSpec]] = None,
    ):
        self.llm = llm
        self.personas = personas or [
            PERSONAS["conservative"],
            PERSONAS["minimal"],
            PERSONAS["exploratory"],
        ]

    def generate(
        self,
        obligation: str,
        contract: Contract,
        representations=None,
        plan_templates=None,
        anti_templates=None,
        parent_event_id=None,
    ) -> list[tuple[dict, LLMResult, PersonaSpec]]:
        candidates: list[tuple[dict, LLMResult, PersonaSpec]] = []
        for persona in self.personas:
            # Persona instruction is appended to the obligation so the system
            # prompt stays unchanged (back-compat with the existing schema-retry
            # logic in call_stipulator_phased).
            persona_obligation = (
                f"{obligation}\n\nPLANNING STYLE for this attempt: {persona.instruction}"
            )
            plan, result = call_stipulator_phased(
                self.llm,
                persona_obligation,
                representations=representations,
                plan_templates=plan_templates,
                anti_templates=anti_templates,
                parent_event_id=parent_event_id,
            )
            if plan is not None:
                candidates.append((plan, result, persona))
            elif result is not None:
                # Schema failure for this persona — record the cost anyway by
                # passing an empty-plan tuple? No — the caller needs to know
                # this persona failed. We just skip it.
                pass
        return candidates


# ────────────────────────────────────────────────────────────────────────────
# Composer — orchestrates generator + scorer
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ComposedPlan:
    """Output of PlanComposer.compose: winning plan + diagnostic record of
    all candidates considered (for trace + offline analysis)."""
    plan: Optional[dict]
    winning_persona: Optional[str]
    winning_score: float
    all_candidates: list[tuple[dict, float, str]] = field(default_factory=list)
    all_results: list[LLMResult] = field(default_factory=list)


class PlanComposer:
    """Multi-candidate plan generation + deterministic selection.

    Usage:
        composer = PlanComposer(llm)
        composed = composer.compose(obligation, contract, ...)
        if composed.plan:
            plan_tree = PlanTree.from_phased_plan(composed.plan, ...)
        for r in composed.all_results:
            activator._charge_llm(contract, r)  # all candidates cost real tokens

    Future parameters (see docs/roadmap.md):
        - optimize_for=...      cost / quality / speed / balanced
        - intelligence_level=...  fast / mid / deep (model tier per persona)
        - K=...                 number of candidates
        - scorer_weights=...    importance per axis
    """

    def __init__(
        self,
        llm: LLM,
        generator: Optional[PlanGenerator] = None,
        scorer: Optional[PlanScorer] = None,
    ):
        self.llm = llm
        self.generator = generator or PersonaStipulator(llm)
        self.scorer = scorer or HeuristicPlanScorer()

    def compose(
        self,
        obligation: str,
        contract: Contract,
        representations=None,
        plan_templates=None,
        anti_templates=None,
        parent_event_id=None,
    ) -> ComposedPlan:
        candidates = self.generator.generate(
            obligation, contract,
            representations=representations,
            plan_templates=plan_templates,
            anti_templates=anti_templates,
            parent_event_id=parent_event_id,
        )
        all_results = [c[1] for c in candidates if c[1] is not None]

        if not candidates:
            return ComposedPlan(
                plan=None, winning_persona=None, winning_score=float("-inf"),
                all_candidates=[], all_results=all_results,
            )

        scored = [
            (self.scorer.score(plan, obligation, contract), plan, persona)
            for plan, _result, persona in candidates
        ]
        scored.sort(key=lambda t: t[0], reverse=True)

        best_score, best_plan, best_persona = scored[0]
        diagnostic = [
            ({"phases": p["phases"]}, s, persona.name)
            for s, p, persona in scored
        ]
        return ComposedPlan(
            plan=best_plan,
            winning_persona=best_persona.name,
            winning_score=best_score,
            all_candidates=diagnostic,
            all_results=all_results,
        )
