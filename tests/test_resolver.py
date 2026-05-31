"""Unit tests for cat/resolver.py — R6+ ResolverOutput layer."""
import json
from unittest.mock import MagicMock

import pytest

from cat.resolver import (
    Resolver,
    ResolverDecision,
    ResolverOutput,
    StallContext,
    StubResolver,
    HeuristicResolver,
    LLMResolver,
    enforce_guards,
    _safe_fallback,
)


def _mk_ctx(**overrides) -> StallContext:
    defaults = dict(
        attention_node_id="C1",
        attention_description="Edit foo.py to add SSL keys",
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
        plan_tree_outline="A1 [x] read\nC1 [>] edit\nD1 [ ] verify",
        recent_tool_calls=[
            {"tool": "read", "args": {"path": "foo.py"}, "output_summary": "got file"},
        ],
        obligation_brief="Add SSL env vars",
        state_now_class="abc123",
        attestor_reasons=["file not yet modified", "no edit detected"],
        files_expected=["foo.py"],
        files_actually_modified=[],
        intent_tool_counts={"investigate": 5, "mutate": 0, "verify": 0, "other": 0},
    )
    defaults.update(overrides)
    return StallContext(**defaults)


# === ResolverOutput basic ==============================================

def test_resolver_output_to_dict():
    o = ResolverOutput(decision="forward", reason="done", diagnosis="d", next_instruction="x")
    d = o.to_dict()
    assert d["decision"] == "forward"
    assert d["reason"] == "done"
    assert d["diagnosis"] == "d"
    assert d["next_instruction"] == "x"


# === StubResolver =======================================================

def test_stub_resolver_returns_output():
    r = StubResolver("backward", next_instruction="try X", diagnosis="diag")
    out = r.resolve(_mk_ctx())
    assert out.decision == "backward"
    assert out.next_instruction == "try X"
    assert out.diagnosis == "diag"


def test_stub_resolver_default_forward():
    r = StubResolver()
    out = r.resolve(_mk_ctx())
    assert out.decision == "forward"


# === HeuristicResolver ==================================================

def test_heuristic_forward_at_restip_cap():
    r = HeuristicResolver()
    out = r.resolve(_mk_ctx(restip_count=5))
    assert out.decision == "forward"


def test_heuristic_backward_on_verify_fail():
    r = HeuristicResolver()
    out = r.resolve(_mk_ctx(
        attention_intent="verify",
        surprise_dims={"verifier_match": False, "any_surprised": True},
        restip_count=2,
    ))
    assert out.decision == "backward"


def test_heuristic_give_up_on_long_stall():
    r = HeuristicResolver()
    out = r.resolve(_mk_ctx(stall_count=6, restip_count=2))
    assert out.decision == "give_up"


def test_heuristic_restipulate_default():
    r = HeuristicResolver()
    out = r.resolve(_mk_ctx(stall_count=3, restip_count=1, attention_intent="investigate",
                             intent_tool_counts={"investigate": 3, "mutate": 0}))
    assert out.decision == "restipulate"


def test_heuristic_mutate_no_mutate_tool_gives_directive():
    """Heuristic should restipulate with a directive when mutate-intent + 0 mutate tools."""
    r = HeuristicResolver()
    out = r.resolve(_mk_ctx(
        attention_intent="mutate",
        intent_tool_counts={"investigate": 5, "mutate": 0},
        restip_count=2,
    ))
    assert out.decision == "restipulate"
    assert "str_replace" in out.next_instruction or "write" in out.next_instruction


# === Deterministic guards ===============================================

def test_guard_overrides_forward_on_unmutated_mutate():
    """forward on mutate step with 0 mutate tools → restipulate."""
    out = ResolverOutput(decision="forward", reason="LLM said done")
    ctx = _mk_ctx(
        attention_intent="mutate",
        intent_tool_counts={"investigate": 5, "mutate": 0},
    )
    guarded = enforce_guards(out, ctx)
    assert guarded.decision == "restipulate"
    assert "GUARD" in guarded.reason
    assert "str_replace" in guarded.next_instruction


def test_guard_allows_forward_on_mutate_with_mutate_tool():
    """forward on mutate step with mutate tool used → unchanged."""
    out = ResolverOutput(decision="forward", reason="real done")
    ctx = _mk_ctx(
        attention_intent="mutate",
        intent_tool_counts={"investigate": 2, "mutate": 1},
    )
    guarded = enforce_guards(out, ctx)
    assert guarded.decision == "forward"


def test_guard_overrides_forward_on_verify_fail():
    """forward on verify step with verifier_match=False → restipulate."""
    out = ResolverOutput(decision="forward", reason="LLM said done")
    ctx = _mk_ctx(
        attention_intent="verify",
        surprise_dims={"verifier_match": False, "any_surprised": True},
    )
    guarded = enforce_guards(out, ctx)
    assert guarded.decision == "restipulate"
    assert "GUARD" in guarded.reason


def test_guard_preserves_non_forward_decisions():
    """Guards don't touch backward / restipulate / give_up."""
    for d in ("backward", "restipulate", "give_up"):
        out = ResolverOutput(decision=d, reason="x")
        # Use any context where guards might apply if d were forward
        ctx = _mk_ctx(attention_intent="mutate", intent_tool_counts={"mutate": 0})
        guarded = enforce_guards(out, ctx)
        assert guarded.decision == d


def test_guard_preserves_existing_next_instruction():
    """If LLM already supplied next_instruction, guard keeps it (doesn't overwrite)."""
    out = ResolverOutput(
        decision="forward",
        reason="LLM forward",
        next_instruction="LLM's directive",
    )
    ctx = _mk_ctx(attention_intent="mutate", intent_tool_counts={"mutate": 0})
    guarded = enforce_guards(out, ctx)
    assert guarded.decision == "restipulate"
    assert guarded.next_instruction == "LLM's directive"  # preserved


# === StallContext payload ===============================================

def test_stall_context_payload_includes_intent_tool_counts():
    payload = _mk_ctx().to_prompt_payload()
    assert "intent_tools" in payload
    assert "investigate=5" in payload
    assert "mutate=0" in payload


def test_stall_context_payload_includes_files_diff():
    payload = _mk_ctx().to_prompt_payload()
    assert "expected to touch ['foo.py']" in payload
    assert "actually touched []" in payload


def test_stall_context_payload_includes_attestor_history():
    payload = _mk_ctx().to_prompt_payload()
    assert "RECENT ATTESTOR VERDICTS" in payload
    assert "file not yet modified" in payload


# === LLMResolver ========================================================

def test_llm_resolver_parses_full_output():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text=json.dumps({
        "decision": "restipulate",
        "reason": "missing edit",
        "diagnosis": "agent searched but never edited",
        "next_instruction": "Use str_replace on /testbed/foo.py",
    }))
    r = LLMResolver(llm)
    out = r.resolve(_mk_ctx())
    assert out.decision == "restipulate"
    assert out.reason == "missing edit"
    assert "searched but never edited" in out.diagnosis
    assert "str_replace" in out.next_instruction


def test_llm_resolver_falls_back_on_invalid_json():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text="{not valid")
    r = LLMResolver(llm)
    out = r.resolve(_mk_ctx())
    assert out.decision in ("forward", "restipulate", "backward", "give_up")


def test_llm_resolver_falls_back_on_empty():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text="")
    r = LLMResolver(llm)
    out = r.resolve(_mk_ctx())
    assert out.decision in ("forward", "restipulate", "backward", "give_up")


def test_llm_resolver_falls_back_on_exception():
    llm = MagicMock()
    llm.chat.side_effect = RuntimeError("api down")
    r = LLMResolver(llm)
    out = r.resolve(_mk_ctx())
    assert out.decision in ("forward", "restipulate", "backward", "give_up")


def test_llm_resolver_falls_back_on_unknown_decision():
    llm = MagicMock()
    llm.chat.return_value = MagicMock(text=json.dumps({"decision": "huh"}))
    r = LLMResolver(llm)
    out = r.resolve(_mk_ctx())
    assert out.decision in ("forward", "restipulate", "backward", "give_up")


# === safe_fallback ======================================================

def test_safe_fallback_forward_on_long_stall():
    out = _safe_fallback(_mk_ctx(stall_count=5))
    assert out.decision == "forward"


def test_safe_fallback_restipulate_default():
    out = _safe_fallback(_mk_ctx(stall_count=2))
    assert out.decision == "restipulate"


def test_resolver_abc_not_instantiable():
    with pytest.raises(TypeError):
        Resolver()  # type: ignore[abstract]
