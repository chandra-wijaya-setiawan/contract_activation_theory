"""Unit tests for PlanComposer / HeuristicPlanScorer / PersonaStipulator."""
import json
from unittest.mock import MagicMock

from cat.plan_composer import (
    PlanComposer,
    PersonaStipulator,
    HeuristicPlanScorer,
    PERSONAS,
    PersonaSpec,
    ComposedPlan,
)
from cat.llm import LLMResult


def _minimal_plan(with_verify=True, with_mutate=True):
    phases = [
        {"name": "IMPLEMENT", "steps": [
            {"description": "edit", "intent": "mutate" if with_mutate else "investigate",
             "suggested_tool": "str_replace",
             "suggested_args": {"path": "/x.py", "old_str": "a", "new_str": "b"},
             "expects": {"durable_change": True, "files_modified": ["x.py"],
                         "result": "x.py has been edited at line 5 OR confirmed no change needed"}},
        ]},
    ]
    if with_verify:
        phases.append({"name": "VERIFY", "steps": [
            {"description": "test", "intent": "verify",
             "suggested_tool": "bash",
             "suggested_args": {"command": "pytest"},
             "expects": {"verifier_outcome": "PASS",
                         "result": "pytest exits 0"}},
        ]})
    return {"phases": phases}


def _llm_result(text=""):
    return LLMResult(text=text, tokens_in=100, tokens_out=50, usd=0.01,
                     model="m", finish_reason="stop")


# ── HeuristicPlanScorer ────────────────────────────────────────────────────


def test_scorer_hard_fails_without_verify():
    s = HeuristicPlanScorer()
    score = s.score(_minimal_plan(with_verify=False), "obligation", MagicMock())
    assert score == s.HARD_FAIL


def test_scorer_hard_fails_without_mutate():
    s = HeuristicPlanScorer()
    score = s.score(_minimal_plan(with_mutate=False), "obligation", MagicMock())
    assert score == s.HARD_FAIL


def test_scorer_rewards_concreteness():
    """A plan with concrete tools + specific expects beats a vague plan."""
    s = HeuristicPlanScorer()
    concrete = _minimal_plan()
    vague = {"phases": [
        {"name": "IMPLEMENT", "steps": [
            {"description": "edit", "intent": "mutate",
             "expects": {"durable_change": True}}  # no tool, no files, no result
        ]},
        {"name": "VERIFY", "steps": [
            {"description": "test", "intent": "verify",
             "expects": {}}  # no command, no result
        ]},
    ]}
    assert s.score(concrete, "x", MagicMock()) > s.score(vague, "x", MagicMock())


def test_scorer_penalizes_too_short():
    s = HeuristicPlanScorer()
    short_plan = _minimal_plan()  # 2 steps
    medium_plan = {"phases": [
        {"name": "MAP", "steps": [
            {"description": f"read file {i}", "intent": "investigate",
             "suggested_tool": "read",
             "suggested_args": {"path": f"/a/b{i}.py"},
             "expects": {"result": f"file {i} has been read OR confirmed missing"}}
            for i in range(3)
        ]},
        *_minimal_plan()["phases"],
    ]}
    # short_plan (n=2) gets size_penalty, medium (n=5) doesn't.
    assert s.score(medium_plan, "x", MagicMock()) > s.score(short_plan, "x", MagicMock())


def test_scorer_rewards_phase_ordering():
    """MAP → IMPLEMENT → VERIFY ordering should outscore reversed order."""
    s = HeuristicPlanScorer()
    ordered = {"phases": [
        {"name": "MAP", "steps": [
            {"description": "investigate x", "intent": "investigate",
             "suggested_tool": "read",
             "suggested_args": {"path": "/x.py"},
             "expects": {"result": "x has been read OR confirmed missing"}},
        ]},
        *_minimal_plan()["phases"],
    ]}
    reversed_ = {"phases": list(reversed(ordered["phases"]))}
    assert s.score(ordered, "x", MagicMock()) > s.score(reversed_, "x", MagicMock())


# ── PersonaStipulator ──────────────────────────────────────────────────────


def test_persona_stipulator_calls_one_per_persona():
    """Each persona triggers one stipulator LLM call."""
    llm = MagicMock()
    # Stub call_stipulator_phased via monkeypatching the symbol used in the module.
    from cat import plan_composer as pc

    calls = []
    def fake_stipulate(llm, obligation, **kwargs):
        calls.append(obligation)
        return _minimal_plan(), _llm_result()
    pc.call_stipulator_phased = fake_stipulate
    try:
        gen = PersonaStipulator(llm)
        candidates = gen.generate("the real obligation", MagicMock())
        assert len(candidates) == 3  # 3 personas
        assert len(calls) == 3
        # Each call's obligation has a persona suffix
        assert all("PLANNING STYLE" in c for c in calls)
        # The persona names are recorded
        names = {persona.name for _, _, persona in candidates}
        assert names == {"conservative", "minimal", "exploratory"}
    finally:
        # Restore the original (import lazily to avoid circular)
        from cat.llm import call_stipulator_phased as real
        pc.call_stipulator_phased = real


def test_persona_stipulator_skips_failed_persona():
    """If one persona returns None plan, it's dropped; others remain."""
    llm = MagicMock()
    from cat import plan_composer as pc
    counter = [0]
    def fake_stipulate(llm, obligation, **kwargs):
        counter[0] += 1
        if counter[0] == 2:  # second call fails
            return None, _llm_result()
        return _minimal_plan(), _llm_result()
    pc.call_stipulator_phased = fake_stipulate
    try:
        gen = PersonaStipulator(llm)
        candidates = gen.generate("obligation", MagicMock())
        assert len(candidates) == 2  # one persona dropped
    finally:
        from cat.llm import call_stipulator_phased as real
        pc.call_stipulator_phased = real


# ── PlanComposer end-to-end ────────────────────────────────────────────────


def test_composer_picks_highest_scoring():
    """Composer scores all candidates and returns the winner."""
    llm = MagicMock()
    from cat import plan_composer as pc

    plans_pool = [
        _minimal_plan(),  # concrete + verify + mutate → high
        {"phases": [  # no mutate → HARD_FAIL
            {"name": "MAP", "steps": [
                {"description": "look", "intent": "investigate"}
            ]},
            {"name": "VERIFY", "steps": [
                {"description": "test", "intent": "verify"}
            ]},
        ]},
        _minimal_plan(),  # another concrete one
    ]
    idx = [0]
    def fake_stipulate(llm, obligation, **kwargs):
        p = plans_pool[idx[0] % len(plans_pool)]
        idx[0] += 1
        return p, _llm_result()
    pc.call_stipulator_phased = fake_stipulate
    try:
        composer = PlanComposer(llm)
        composed = composer.compose("obligation", MagicMock())
        assert composed.plan is not None
        # The winning persona should be one of the two that produced a valid plan
        assert composed.winning_persona in ("conservative", "exploratory")
        assert composed.winning_score > 0  # not the hard-fail one
        # Diagnostic: all 3 candidates kept
        assert len(composed.all_candidates) == 3
    finally:
        from cat.llm import call_stipulator_phased as real
        pc.call_stipulator_phased = real


def test_composer_returns_none_when_all_fail():
    llm = MagicMock()
    from cat import plan_composer as pc
    pc.call_stipulator_phased = lambda llm, obligation, **kw: (None, _llm_result())
    try:
        composer = PlanComposer(llm)
        composed = composer.compose("obligation", MagicMock())
        assert composed.plan is None
        assert composed.winning_persona is None
    finally:
        from cat.llm import call_stipulator_phased as real
        pc.call_stipulator_phased = real


def test_composer_charges_all_results():
    """Even discarded candidates' LLM results are returned for cost charging."""
    llm = MagicMock()
    from cat import plan_composer as pc
    pc.call_stipulator_phased = lambda llm, obligation, **kw: (_minimal_plan(), _llm_result(text="x"))
    try:
        composer = PlanComposer(llm)
        composed = composer.compose("obligation", MagicMock())
        # 3 personas × 1 result each = 3 LLMResults the caller must charge
        assert len(composed.all_results) == 3
    finally:
        from cat.llm import call_stipulator_phased as real
        pc.call_stipulator_phased = real


# ── Personas ────────────────────────────────────────────────────────────────


def test_personas_dict_has_three_canonical_entries():
    assert set(PERSONAS) == {"conservative", "minimal", "exploratory"}
    for name, persona in PERSONAS.items():
        assert isinstance(persona, PersonaSpec)
        assert persona.name == name
        assert persona.instruction  # non-empty


def test_persona_stipulator_custom_personas():
    """Constructor accepts a custom persona list (extension point)."""
    custom = [PersonaSpec(name="custom", instruction="do whatever")]
    gen = PersonaStipulator(MagicMock(), personas=custom)
    assert len(gen.personas) == 1
    assert gen.personas[0].name == "custom"
