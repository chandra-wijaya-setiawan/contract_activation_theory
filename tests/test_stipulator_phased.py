"""Tests for the v2 phased Stipulator + in-loop ops parser."""
import json
from unittest.mock import MagicMock

from cat.llm import _parse_phased_plan, _parse_inloop_ops, call_stipulator_phased, LLMResult


_VERIFY_PHASE = {"name": "VERIFY", "steps": [
    {"description": "run pytest", "intent": "verify",
     "expects": {"verifier_outcome": "PASS"}}
]}


def test_parse_phased_plan_basic():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read X", "intent": "investigate", "expects": {"durable_change": False}}
            ]},
            {"name": "IMPLEMENT", "steps": [
                {"description": "edit Y", "intent": "mutate", "expects": {"durable_change": True, "files_modified": ["y.py"]}}
            ]},
            _VERIFY_PHASE,
        ]
    })
    plan = _parse_phased_plan(text)
    assert plan is not None
    assert len(plan["phases"]) == 3
    assert plan["phases"][0]["name"] == "MAP"
    assert plan["phases"][1]["steps"][0]["expects"]["durable_change"] is True


def test_parse_phased_plan_drops_empty_steps():
    text = json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "", "intent": "investigate"},
                {"description": "real one", "intent": "investigate"},
            ]},
            _VERIFY_PHASE,
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
            _VERIFY_PHASE,
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
            _VERIFY_PHASE,
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


def test_parse_inloop_ops_rejects_unknown_op():
    """Pydantic discriminated-union strict-rejects unknown ops; the whole
    batch fails so the PlanPatcher retry path (or _safe_fallback) handles it,
    rather than silently dropping novel-named ops and applying partial work."""
    text = json.dumps({
        "ops": [
            {"op": "WHIMSY", "node_id": "X", "reflection": "x"},
            {"op": "COMMIT", "node_id": "A1", "reason": "done", "reflection": "ok"},
        ]
    })
    assert _parse_inloop_ops(text) is None


def test_parse_inloop_ops_invalid_json():
    assert _parse_inloop_ops("{nope") is None


def _good_phased_json():
    return json.dumps({
        "phases": [
            {"name": "MAP", "steps": [
                {"description": "read X", "intent": "investigate",
                 "expects": {"durable_change": False}}
            ]},
            _VERIFY_PHASE,
        ]
    })


def _flat_v1_json():
    """A response in the OLD v1 flat schema — what we observed deepseek-v4-flash
    occasionally emit. Pydantic rejects this; the retry should fire."""
    return json.dumps([
        {"id": "s1", "description": "Edit X"},
        {"id": "s2", "description": "Run pytest"},
    ])


def _mk_llm_result(text):
    return LLMResult(text=text, tokens_in=10, tokens_out=10, usd=0.0, model="m",
                     finish_reason="stop")


def test_call_stipulator_phased_succeeds_first_try():
    """Happy path: first LLM call returns valid PhasedPlan → no retry."""
    llm = MagicMock()
    llm.chat.return_value = _mk_llm_result(_good_phased_json())
    plan, _ = call_stipulator_phased(llm, "obligation X")
    assert plan is not None
    assert len(plan["phases"]) == 2  # MAP + VERIFY (the verify-required validator)
    assert llm.chat.call_count == 1


def test_call_stipulator_phased_retries_on_flat_schema():
    """v1 flat schema rejected → retry → second attempt returns good phased."""
    llm = MagicMock()
    llm.chat.side_effect = [
        _mk_llm_result(_flat_v1_json()),
        _mk_llm_result(_good_phased_json()),
    ]
    plan, _ = call_stipulator_phased(llm, "obligation X")
    assert plan is not None
    assert llm.chat.call_count == 2
    # The second call must include the correction message
    second_messages = llm.chat.call_args_list[1].kwargs["messages"]
    assert any("PhasedPlan schema" in m.get("content", "") for m in second_messages)


def test_call_stipulator_phased_exhausts_retries_returns_none():
    """All 3 attempts return invalid schema → caller gets None (must fail contract)."""
    llm = MagicMock()
    llm.chat.return_value = _mk_llm_result(_flat_v1_json())
    plan, result = call_stipulator_phased(llm, "obligation X")
    assert plan is None
    assert result is not None  # last result returned so caller can log
    assert llm.chat.call_count == 3  # 1 initial + 2 retries


def test_call_stipulator_phased_empty_response_no_retry():
    """If LLM returns empty text, R11's chat() already handled retries —
    don't retry again here, just bail."""
    llm = MagicMock()
    llm.chat.return_value = _mk_llm_result("")
    plan, _ = call_stipulator_phased(llm, "obligation X")
    assert plan is None
    assert llm.chat.call_count == 1


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
