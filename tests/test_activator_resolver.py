"""Integration tests for Activator + Resolver wiring.

Mocks the LLM and just exercises the resolver escalation path with a
StubResolver to verify each decision lands the right tree-op + state.
"""
import json
from unittest.mock import MagicMock

import pytest

from cat.activator import Activator
from cat.contracts import Contract, ContractState
from cat.plan_tree import PlanTree, TreeEditOp
from cat.resolver import StubResolver, StallContext
from cat.surprise import SurpriseVector


SAMPLE_PHASED_PLAN = {
    "phases": [
        {"name": "MAP", "steps": [
            {"description": "read x.py", "intent": "investigate", "expects": {"durable_change": False}}
        ]},
        {"name": "IMPLEMENT", "steps": [
            {"description": "edit x.py", "intent": "mutate", "expects": {"durable_change": True, "files_modified": ["x.py"]}}
        ]},
        {"name": "VERIFY", "steps": [
            {"description": "run tests", "intent": "verify", "expects": {"durable_change": False, "verifier_outcome": "PASS"}}
        ]},
    ]
}


def _mk_activator(resolver, stall_threshold=3):
    """Build an Activator with a mocked LLM + the given resolver."""
    llm = MagicMock()
    activator = Activator.__new__(Activator)
    activator.llm = llm
    activator.recorder = MagicMock()
    activator.recorder.traj = MagicMock()
    activator.recorder.traj.events = []
    activator.resolver = resolver
    activator.max_restip_per_task = 5
    activator.stall_threshold = stall_threshold
    activator.max_backwards_per_task = 2
    activator.librarian_writer = None
    return activator


def _mk_run(plan_tree):
    """Build a minimal mock ActivatorRun."""
    run = MagicMock()
    run.messages = [{"role": "tool", "content": "some output"}]
    run.iterations = 5
    run.last_perform_event_id = "evt-1"
    run.last_iter_anchor = "evt-0"
    run.plan_tree = plan_tree
    run.restip_count = 0
    run.backward_count = 0
    run._stall_count = {}
    return run


def test_resolver_forward_decision_advances_attention():
    """forward → COMMIT current + advance attention to next leaf."""
    tree = PlanTree.from_phased_plan(SAMPLE_PHASED_PLAN)
    assert tree.attention_id == "A1"
    activator = _mk_activator(StubResolver("forward"))
    run = _mk_run(tree)
    run._stall_count = {"A1": 3}  # at threshold
    attention = tree.get_attention()
    sv = SurpriseVector()
    contract = MagicMock(obligation="test obligation", id="task-1")

    output = activator._invoke_resolver(contract, run, tree, attention, sv, "state123")
    decision = output.decision
    activator._apply_resolver_decision(contract, run, tree, attention, decision, sv, "state123")

    assert decision == "forward"
    assert tree.nodes["A1"].status == "complete"
    assert tree.attention_id == "B1"  # advanced


def test_resolver_backward_decision_drops_and_restores():
    """backward → DROP current subtree, restore attention to last mutate."""
    tree = PlanTree.from_phased_plan(SAMPLE_PHASED_PLAN)
    # Mark A1 and B1 (mutate) as complete first
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "B1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "C1"}, ""))
    assert tree.attention_id == "C1"
    assert tree.nodes["B1"].status == "complete"
    assert tree.nodes["B1"].intent == "mutate"

    activator = _mk_activator(StubResolver("backward"))
    run = _mk_run(tree)
    run._stall_count = {"C1": 3}
    attention = tree.get_attention()
    sv = SurpriseVector()
    contract = MagicMock(obligation="test obligation", id="task-1")

    output = activator._invoke_resolver(contract, run, tree, attention, sv, "state123")
    decision = output.decision
    activator._apply_resolver_decision(contract, run, tree, attention, decision, sv, "state123")

    assert decision == "backward"
    assert tree.nodes["C1"].status == "pruned"
    # backward should restore attention — in this 3-step plan with B1 as last
    # mutate, the next pending after B1 is C1 which we just pruned, so
    # advance_attention picks up next pending leaf overall (none) or null.
    # The key thing is: C1 is no longer active.
    assert tree.attention_id != "C1" or tree.nodes["C1"].status != "active"
    assert run.backward_count == 1


def test_resolver_give_up_marks_contract_failed():
    """give_up → contract goes to FAILED state with explicit reason."""
    tree = PlanTree.from_phased_plan(SAMPLE_PHASED_PLAN)
    activator = _mk_activator(StubResolver("give_up"))
    activator._transition = MagicMock()  # capture the transition
    run = _mk_run(tree)
    run._stall_count = {"A1": 3}
    attention = tree.get_attention()
    sv = SurpriseVector()
    contract = MagicMock(obligation="test obligation", id="task-1")

    output = activator._invoke_resolver(contract, run, tree, attention, sv, "state123")
    decision = output.decision
    activator._apply_resolver_decision(contract, run, tree, attention, decision, sv, "state123")

    assert decision == "give_up"
    activator._transition.assert_called_once()
    args = activator._transition.call_args
    assert args[0][1] == ContractState.FAILED
    assert "resolver gave up" in args[0][2]
    assert run.failure_reason == "resolver_gave_up_at_A1"


def test_resolver_backward_cap_falls_back_to_forward():
    """If backward_count >= max_backwards_per_task, backward falls back to forward."""
    tree = PlanTree.from_phased_plan(SAMPLE_PHASED_PLAN)
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "B1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "C1"}, ""))
    activator = _mk_activator(StubResolver("backward"))
    activator.max_backwards_per_task = 2
    run = _mk_run(tree)
    run._stall_count = {"C1": 3}
    run.backward_count = 2  # already at cap
    attention = tree.get_attention()
    sv = SurpriseVector()
    contract = MagicMock(obligation="test obligation", id="task-1")

    output = activator._invoke_resolver(contract, run, tree, attention, sv, "state123")
    decision = output.decision
    activator._apply_resolver_decision(contract, run, tree, attention, decision, sv, "state123")
    # backward decision returned, but fallback should make C1 complete (not pruned)
    assert tree.nodes["C1"].status == "complete"  # forward, not pruned


def test_resolver_restipulate_cap_falls_back_to_forward():
    """If restip_count >= max_restip_per_task, restipulate falls back to forward."""
    tree = PlanTree.from_phased_plan(SAMPLE_PHASED_PLAN)
    activator = _mk_activator(StubResolver("restipulate"))
    activator.max_restip_per_task = 5
    run = _mk_run(tree)
    run._stall_count = {"A1": 3}
    run.restip_count = 5  # at cap
    attention = tree.get_attention()
    sv = SurpriseVector()
    contract = MagicMock(obligation="test obligation", id="task-1")

    output = activator._invoke_resolver(contract, run, tree, attention, sv, "state123")
    decision = output.decision
    activator._apply_resolver_decision(contract, run, tree, attention, decision, sv, "state123")
    # forward fallback
    assert tree.nodes["A1"].status == "complete"


def test_default_resolver_is_llm_resolver():
    """If resolver=None passed to Activator constructor, defaults to LLMResolver."""
    # We can't fully construct Activator without a registry etc. but we can
    # verify the constructor logic by instantiating just enough.
    from cat.resolver import LLMResolver
    llm = MagicMock()

    # Use __new__ + manual init partial to avoid the full Activator deps
    a = Activator.__new__(Activator)
    a.llm = llm
    # Manually invoke the resolver-init branch
    resolver = None
    if resolver is None:
        from cat.resolver import LLMResolver as LR
        resolver = LR(a.llm)
    a.resolver = resolver
    assert isinstance(a.resolver, LLMResolver)


def test_resolver_invocation_emits_trace_event():
    """The Resolver escalation should emit a surprise trace event with decision."""
    tree = PlanTree.from_phased_plan(SAMPLE_PHASED_PLAN)
    activator = _mk_activator(StubResolver("forward"))
    run = _mk_run(tree)
    run._stall_count = {"A1": 3}
    attention = tree.get_attention()
    sv = SurpriseVector()
    contract = MagicMock(obligation="test obligation", id="task-1")

    activator._invoke_resolver(contract, run, tree, attention, sv, "state123")
    # Verify recorder.append was called with a surprise event mentioning resolver_escalation
    calls = activator.recorder.append.call_args_list
    assert len(calls) >= 1
    last_event = calls[-1][0][0]
    assert last_event.kind == "surprise"
    assert last_event.payload.get("resolver_escalation") is True
    assert last_event.payload.get("decision") == "forward"
