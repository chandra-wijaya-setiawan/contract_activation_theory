"""Unit tests for cat/plan_schema.py."""
import json

import pytest
from pydantic import ValidationError

from cat.plan_schema import StepExpects, PlanStep, Phase, PhasedPlan
from cat.llm import _parse_phased_plan


def test_step_expects_defaults():
    e = StepExpects()
    assert e.durable_change is False
    assert e.files_modified == []
    assert e.verifier_outcome == "N/A"
    assert e.result == ""


def test_step_expects_with_values():
    e = StepExpects(
        durable_change=True,
        files_modified=["a.py"],
        verifier_outcome="PASS",
        result="A is defined OR confirmed missing",
    )
    assert e.durable_change is True
    assert "OR confirmed" in e.result


def test_step_expects_verifier_outcome_enum():
    """verifier_outcome must be one of the allowed values."""
    with pytest.raises(ValidationError):
        StepExpects(verifier_outcome="MAYBE")  # type: ignore[arg-type]


def test_plan_step_with_result():
    step = PlanStep(
        description="locate is_zero",
        intent="investigate",
        expects=StepExpects(result="is_zero in numbers.py OR confirmed inherited"),
    )
    assert step.expects.result.startswith("is_zero in numbers.py")


def test_phased_plan_validates_json():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read x", "intent": "investigate",
                 "expects": {"result": "we read x"}}
            ]},
            {"name": "IMPLEMENT", "steps": [
                {"description": "edit y", "intent": "mutate",
                 "expects": {"durable_change": True, "files_modified": ["y.py"]}}
            ]},
            {"name": "VERIFY", "steps": [
                {"description": "run pytest", "intent": "verify",
                 "expects": {"verifier_outcome": "PASS"}}
            ]},
        ]
    })
    plan = PhasedPlan.model_validate_json(text)
    assert len(plan.phases) == 3
    assert plan.phases[0].name == "MAP"
    assert plan.phases[0].steps[0].expects.result == "we read x"
    assert plan.phases[1].steps[0].expects.durable_change is True
    assert plan.phases[2].steps[0].intent == "verify"


def test_phased_plan_rejects_missing_verify_step():
    """Stipulator must emit at least one verify step — otherwise the agent
    can exhaust the plan without running the test (django-12039 regression)."""
    from pydantic import ValidationError
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read x", "intent": "investigate"}
            ]},
            {"name": "IMPLEMENT", "steps": [
                {"description": "edit y", "intent": "mutate",
                 "expects": {"durable_change": True}}
            ]},
        ]  # no VERIFY phase, no verify-intent step
    })
    with pytest.raises(ValidationError) as exc:
        PhasedPlan.model_validate_json(text)
    assert "verify" in str(exc.value).lower()


def test_phased_plan_accepts_verify_outside_verify_phase():
    """The check is per-step intent, not phase name. A verify step inside
    an OTHER-named phase still satisfies the requirement."""
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read x", "intent": "investigate"}
            ]},
            {"name": "IMPLEMENT", "steps": [
                {"description": "edit y", "intent": "mutate",
                 "expects": {"durable_change": True}},
                {"description": "run pytest", "intent": "verify",
                 "expects": {"verifier_outcome": "PASS"}},
            ]},
        ]
    })
    plan = PhasedPlan.model_validate_json(text)
    assert any(s.intent == "verify" for p in plan.phases for s in p.steps)


def test_phased_plan_rejects_bad_intent():
    """Pydantic enforces intent enum."""
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "x", "intent": "whatever"}
            ]},
            {"name": "VERIFY", "steps": [
                {"description": "test", "intent": "verify",
                 "expects": {"verifier_outcome": "PASS"}}
            ]},
        ]
    })
    # _parse_phased_plan coerces unknown intent to 'other'
    plan_dict = _parse_phased_plan(text)
    assert plan_dict is not None
    assert plan_dict["phases"][0]["steps"][0]["intent"] == "other"


def _verify_phase():
    """Tail VERIFY phase appended to test fixtures so they satisfy the
    R12-mandatory verify-step validator without distracting from each test's
    actual focus."""
    return {"name": "VERIFY", "steps": [
        {"description": "run pytest", "intent": "verify",
         "expects": {"verifier_outcome": "PASS"}}
    ]}


def test_phased_plan_rejects_bad_phase_name():
    """_parse_phased_plan coerces unknown phase to OTHER."""
    text = json.dumps({
        "phases": [
            {"name": "WHATEVER", "steps": [
                {"description": "x", "intent": "investigate"}
            ]},
            _verify_phase(),
        ]
    })
    plan_dict = _parse_phased_plan(text)
    assert plan_dict is not None
    assert plan_dict["phases"][0]["name"] == "OTHER"


def test_phased_plan_drops_empty_step_descriptions():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "", "intent": "investigate"},
                {"description": "real one", "intent": "investigate"},
            ]},
            _verify_phase(),
        ]
    })
    plan = _parse_phased_plan(text)
    assert len(plan["phases"][0]["steps"]) == 1
    assert plan["phases"][0]["steps"][0]["description"] == "real one"


def test_phased_plan_drops_invalid_expects_dict():
    """When expects is not a dict, it gets coerced to {}."""
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "x", "intent": "investigate", "expects": "not-a-dict"}
            ]},
            _verify_phase(),
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan is not None
    step = plan["phases"][0]["steps"][0]
    assert step["expects"]["durable_change"] is False
    assert step["expects"]["result"] == ""


def test_phased_plan_invalid_json():
    assert _parse_phased_plan("{not json") is None


def test_phased_plan_no_phases():
    assert _parse_phased_plan(json.dumps({"phases": []})) is None


def test_phased_plan_backward_compat_knowledge_gained():
    """Old plans with 'knowledge_gained' should still parse (field is dropped, not crash)."""
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "x", "intent": "investigate",
                 "expects": {"knowledge_gained": "we will know x"}}
            ]},
            _verify_phase(),
        ]
    })
    plan = _parse_phased_plan(text)
    # Pydantic ignores extra fields by default → step parses but knowledge_gained dropped
    assert plan is not None
    # result stays empty (no migration of knowledge_gained → result)
    assert plan["phases"][0]["steps"][0]["expects"]["result"] == ""


def test_step_expects_model_dump_roundtrip():
    e = StepExpects(durable_change=True, files_modified=["a.py"], result="check x")
    d = e.model_dump()
    e2 = StepExpects(**d)
    assert e2.durable_change is True
    assert e2.files_modified == ["a.py"]
    assert e2.result == "check x"


def test_plan_step_with_suggested_tool_and_args():
    """R10b: PlanStep can carry suggested_tool + suggested_args."""
    step = PlanStep(
        description="edit foo.py",
        intent="mutate",
        suggested_tool="str_replace",
        suggested_args={
            "path": "/testbed/foo.py",
            "old_str": "return False",
            "new_str": "return None",
        },
        expects=StepExpects(durable_change=True, files_modified=["foo.py"]),
    )
    assert step.suggested_tool == "str_replace"
    assert step.suggested_args["path"] == "/testbed/foo.py"


def test_plan_step_without_suggestion_defaults_to_none():
    step = PlanStep(description="x", intent="design")
    assert step.suggested_tool is None
    assert step.suggested_args == {}


def test_phased_plan_with_suggestion_round_trips():
    """JSON with suggested_tool/args parses correctly via _parse_phased_plan."""
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read x", "intent": "investigate",
                 "suggested_tool": "read",
                 "suggested_args": {"path": "/testbed/x.py"},
                 "expects": {"result": "x is read"}}
            ]},
            _verify_phase(),
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan is not None
    s = plan["phases"][0]["steps"][0]
    assert s["suggested_tool"] == "read"
    assert s["suggested_args"]["path"] == "/testbed/x.py"


def test_plan_node_constructed_from_dict_with_suggestion():
    """plan_tree.PlanTree.from_phased_plan picks up suggested_tool/args."""
    from cat.plan_tree import PlanTree
    phased = {
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "rg is_zero", "intent": "investigate",
                 "suggested_tool": "rg",
                 "suggested_args": {"pattern": "def is_zero", "paths": "/testbed"},
                 "expects": {"result": "located"}}
            ]}
        ]
    }
    tree = PlanTree.from_phased_plan(phased)
    node = tree.nodes["A1"]
    assert node.suggested_tool == "rg"
    assert node.suggested_args["pattern"] == "def is_zero"
