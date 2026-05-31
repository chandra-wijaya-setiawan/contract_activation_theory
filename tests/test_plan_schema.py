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
        ]
    })
    plan = PhasedPlan.model_validate_json(text)
    assert len(plan.phases) == 2
    assert plan.phases[0].name == "MAP"
    assert plan.phases[0].steps[0].expects.result == "we read x"
    assert plan.phases[1].steps[0].expects.durable_change is True


def test_phased_plan_rejects_bad_intent():
    """Pydantic enforces intent enum."""
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "x", "intent": "whatever"}
            ]}
        ]
    })
    # _parse_phased_plan coerces unknown intent to 'other'
    plan_dict = _parse_phased_plan(text)
    assert plan_dict is not None
    assert plan_dict["phases"][0]["steps"][0]["intent"] == "other"


def test_phased_plan_rejects_bad_phase_name():
    """_parse_phased_plan coerces unknown phase to OTHER."""
    text = json.dumps({
        "phases": [
            {"name": "WHATEVER", "steps": [
                {"description": "x", "intent": "investigate"}
            ]}
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
            ]}
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
            ]}
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
            ]}
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
