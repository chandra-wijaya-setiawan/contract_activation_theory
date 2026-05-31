"""Unit tests for cat/resolver.py."""
import json
from unittest.mock import MagicMock

import pytest

from cat.resolver import (
    Resolver,
    ResolverDecision,
    StallContext,
    StubResolver,
    HeuristicResolver,
    LLMResolver,
    _safe_fallback,
)


def _mk_ctx(**overrides) -> StallContext:
    defaults = dict(
        attention_node_id="C1",
        attention_description="Edit foo.py to add the SSL keys",
        attention_intent="mutate",
        attention_phase="IMPLEMENT",
        expects={"durable_change": True, "files_modified": ["foo.py"]},
        surprise_dims={
            "any_surprised": True, "durable_match": False, "files_match": False,
            "verifier_match": True, "cost_z": 0.1, "outcome_match": True,
            "state_match": True,
        },
        stall_count=3,
        restip_count=1,
        plan_tree_outline="A1 [x] read foo.py\nA2 [x] outline\nC1 [>] edit foo.py\nD1 [ ] verify",
        recent_tool_calls=[
            {"tool": "read", "args": {"path": "foo.py"}, "output_summary": "got file"},
            {"tool": "outline", "args": {"path": "foo.py"}, "output_summary": "classes listed"},
        ],
        obligation_brief="Add SSL env vars (sslmode, sslcert, sslkey) to settings_to_cmd_args_env",
        state_now_class="abc123",
    )
    defaults.update(overrides)
    return StallContext(**defaults)


def test_stub_resolver_returns_fixed():
    r = StubResolver("backward")
    assert r.resolve(_mk_ctx()) == "backward"
    r = StubResolver("forward")
    assert r.resolve(_mk_ctx()) == "forward"


def test_stub_resolver_default_forward():
    r = StubResolver()
    assert r.resolve(_mk_ctx()) == "forward"


def test_heuristic_resolver_forward_when_max_restip():
    r = HeuristicResolver()
    assert r.resolve(_mk_ctx(restip_count=5)) == "forward"
    assert r.resolve(_mk_ctx(restip_count=10)) == "forward"


def test_heuristic_resolver_backward_on_verify_fail():
    r = HeuristicResolver()
    ctx = _mk_ctx(
        attention_intent="verify",
        surprise_dims={"verifier_match": False, "any_surprised": True},
        restip_count=2,
    )
    assert r.resolve(ctx) == "backward"


def test_heuristic_resolver_give_up_on_long_stall():
    r = HeuristicResolver()
    assert r.resolve(_mk_ctx(stall_count=6, restip_count=2)) == "give_up"
    assert r.resolve(_mk_ctx(stall_count=10, restip_count=2)) == "give_up"


def test_heuristic_resolver_default_restipulate():
    r = HeuristicResolver()
    # not at restip cap, not verify-fail, not at long stall
    ctx = _mk_ctx(stall_count=3, restip_count=1)
    assert r.resolve(ctx) == "restipulate"


def test_stall_context_prompt_payload():
    ctx = _mk_ctx()
    payload = ctx.to_prompt_payload()
    assert "OBLIGATION (brief)" in payload
    assert "STUCK AT" in payload
    assert "C1" in payload
    assert "WHY STUCK" in payload
    assert "stall_count = 3" in payload
    assert "PLAN TREE" in payload
    assert "RECENT TOOL ACTIVITY" in payload


def test_llm_resolver_parses_valid_decision():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(
        text=json.dumps({"decision": "backward", "reason": "previous edit looked wrong"}),
    )
    r = LLMResolver(llm)
    assert r.resolve(_mk_ctx()) == "backward"


def test_llm_resolver_all_four_decisions():
    for d in ("forward", "restipulate", "backward", "give_up"):
        llm = MagicMock()
        llm.chat.return_value = MagicMock(text=json.dumps({"decision": d, "reason": "test"}))
        r = LLMResolver(llm)
        assert r.resolve(_mk_ctx()) == d


def test_llm_resolver_falls_back_on_bad_json():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text="{not valid json")
    r = LLMResolver(llm)
    # falls back to safe default
    d = r.resolve(_mk_ctx())
    assert d in ("forward", "restipulate", "backward", "give_up")


def test_llm_resolver_falls_back_on_empty_text():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text="")
    r = LLMResolver(llm)
    d = r.resolve(_mk_ctx())
    assert d in ("forward", "restipulate", "backward", "give_up")


def test_llm_resolver_falls_back_on_unknown_decision():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text=json.dumps({"decision": "whatever", "reason": "x"}))
    r = LLMResolver(llm)
    d = r.resolve(_mk_ctx())
    assert d in ("forward", "restipulate", "backward", "give_up")


def test_llm_resolver_falls_back_on_exception():
    llm = MagicMock()
    llm.chat.side_effect = RuntimeError("network failure")
    r = LLMResolver(llm)
    d = r.resolve(_mk_ctx())
    assert d in ("forward", "restipulate", "backward", "give_up")


def test_safe_fallback_forward_on_long_stall():
    ctx = _mk_ctx(stall_count=5)
    assert _safe_fallback(ctx) == "forward"


def test_safe_fallback_restipulate_otherwise():
    ctx = _mk_ctx(stall_count=2)
    assert _safe_fallback(ctx) == "restipulate"


def test_resolver_abc_cannot_be_instantiated():
    with pytest.raises(TypeError):
        Resolver()  # type: ignore[abstract]
