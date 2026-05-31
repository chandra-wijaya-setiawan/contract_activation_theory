"""Tests for the v2 phased Stipulator + in-loop ops parser."""
import json

from cat.llm import _parse_phased_plan, _parse_inloop_ops


def test_parse_phased_plan_basic():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read X", "intent": "investigate", "expects": {"durable_change": False}}
            ]},
            {"name": "IMPLEMENT", "steps": [
                {"description": "edit Y", "intent": "mutate", "expects": {"durable_change": True, "files_modified": ["y.py"]}}
            ]},
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan is not None
    assert len(plan["phases"]) == 2
    assert plan["phases"][0]["name"] == "MAP"
    assert plan["phases"][1]["steps"][0]["expects"]["durable_change"] is True


def test_parse_phased_plan_drops_empty_steps():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "", "intent": "investigate"},
                {"description": "real one", "intent": "investigate"},
            ]},
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan is not None
    assert len(plan["phases"][0]["steps"]) == 1


def test_parse_phased_plan_normalizes_bad_intent():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "x", "intent": "whatever"},
            ]},
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan["phases"][0]["steps"][0]["intent"] == "other"


def test_parse_phased_plan_normalizes_bad_phase():
    text = json.dumps({
        "phases": [
            {"name": "WEIRD", "steps": [
                {"description": "x", "intent": "investigate"},
            ]},
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan["phases"][0]["name"] == "OTHER"


def test_parse_phased_plan_invalid_json():
    assert _parse_phased_plan("{not json") is None


def test_parse_phased_plan_no_phases():
    assert _parse_phased_plan(json.dumps({})) is None


def test_parse_inloop_ops_basic():
    text = json.dumps({
        "ops": [
            {"op": "COMMIT", "node_id": "A1", "reason": "done", "reflection": "test passed"},
            {"op": "CHECKOUT", "node_id": "A2", "reflection": "moving on"},
        ]
    })
    ops = _parse_inloop_ops(text)
    assert ops is not None
    assert len(ops) == 2
    assert ops[0]["op"] == "COMMIT"


def test_parse_inloop_ops_drops_no_reflection():
    text = json.dumps({
        "ops": [
            {"op": "COMMIT", "node_id": "A1", "reason": "done"},  # no reflection
        ]
    })
    assert _parse_inloop_ops(text) is None  # all ops dropped


def test_parse_inloop_ops_drops_missing_required():
    text = json.dumps({
        "ops": [
            {"op": "COMMIT", "reason": "done", "reflection": "x"},  # missing node_id
        ]
    })
    assert _parse_inloop_ops(text) is None


def test_parse_inloop_ops_drops_unknown_op():
    text = json.dumps({
        "ops": [
            {"op": "WHIMSY", "node_id": "X", "reflection": "x"},
            {"op": "COMMIT", "node_id": "A1", "reason": "done", "reflection": "ok"},
        ]
    })
    ops = _parse_inloop_ops(text)
    assert len(ops) == 1
    assert ops[0]["op"] == "COMMIT"


def test_parse_inloop_ops_invalid_json():
    assert _parse_inloop_ops("{nope") is None


def test_inloop_branch_with_alt_steps():
    text = json.dumps({
        "ops": [
            {
                "op": "BRANCH",
                "node_id": "D1",
                "alt_steps": [
                    {"description": "alt", "intent": "verify", "expects": {}}
                ],
                "reflection": "pytest failed; trying django test runner",
            }
        ]
    })
    ops = _parse_inloop_ops(text)
    assert ops is not None
    assert ops[0]["op"] == "BRANCH"
    assert ops[0]["alt_steps"][0]["description"] == "alt"
