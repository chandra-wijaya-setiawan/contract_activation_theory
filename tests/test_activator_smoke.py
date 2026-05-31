"""Smoke tests for Activator — no real LLM, just verifying wiring.

We mock the LLM and stipulator calls to confirm the v2 path:
- builds a PlanTree from a phased plan
- applies tree-edit ops
- emits expected trace events
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cat.activator import Activator
from cat.capabilities import CapabilityRegistry
from cat.contracts import Constraints, Contract, Criterion, ContractState, State, Cost
from cat.llm import LLMResult
from cat.plan_tree import PlanTree, TreeEditOp
from cat.trace import Recorder


PHASED_PLAN_JSON = json.dumps({
    "phases": [
        {"name": "MAP", "steps": [
            {"description": "read X", "intent": "investigate", "expects": {"durable_change": False}}
        ]},
        {"name": "IMPLEMENT", "steps": [
            {"description": "edit Y", "intent": "mutate",
             "expects": {"durable_change": True, "files_modified": ["y.py"]}}
        ]},
    ]
})


def test_activator_imports():
    from cat.activator import Activator
    assert Activator is not None


def test_plan_tree_built_from_phased_plan():
    """Manual: from_phased_plan + apply_op happy path."""
    plan_dict = json.loads(PHASED_PLAN_JSON)
    tree = PlanTree.from_phased_plan(plan_dict, obligation_class="abc")
    assert tree.attention_id == "A1"
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": "x"}, "ref"))
    nxt = tree.advance_attention()
    assert nxt == "B1"
    assert tree.nodes["B1"].status == "active"


def test_step_appears_done_logic_investigate_empty():
    """investigate step with empty tool output → not done."""
    from cat.activator import Activator
    from cat.activator import ActivatorRun, _PlanStep
    from cat.plan_tree import PlanTree
    from cat.surprise import SurpriseVector

    plan_dict = json.loads(PHASED_PLAN_JSON)
    tree = PlanTree.from_phased_plan(plan_dict)
    attention = tree.nodes["A1"]

    # Build a minimal Activator instance for the helper
    activator = Activator.__new__(Activator)
    # Build a run with empty tool result
    run = MagicMock()
    run.messages = [{"role": "tool", "content": ""}]
    sv = SurpriseVector()  # all default-True
    assert not activator._step_appears_done(run, attention, sv)


def test_step_appears_done_investigate_with_output():
    """investigate step with real tool output → done."""
    plan_dict = json.loads(PHASED_PLAN_JSON)
    tree = PlanTree.from_phased_plan(plan_dict)
    attention = tree.nodes["A1"]
    activator = Activator.__new__(Activator)
    run = MagicMock()
    run.messages = [{"role": "tool", "content": "/path/to/file.py\nclass Foo:\n  ..."}]
    from cat.surprise import SurpriseVector
    sv = SurpriseVector()
    assert activator._step_appears_done(run, attention, sv)


def test_step_appears_done_mutate_durable_match():
    """mutate step requires durable_match=True AND files_match=True."""
    plan_dict = json.loads(PHASED_PLAN_JSON)
    tree = PlanTree.from_phased_plan(plan_dict)
    attention = tree.nodes["B1"]
    activator = Activator.__new__(Activator)
    run = MagicMock()
    run.messages = [{"role": "tool", "content": "edited"}]
    from cat.surprise import SurpriseVector
    # durable=True, files=True → done
    sv = SurpriseVector(durable_match=True, files_match=True)
    assert activator._step_appears_done(run, attention, sv)
    # durable=False → not done
    sv2 = SurpriseVector(durable_match=False, files_match=True)
    assert not activator._step_appears_done(run, attention, sv2)


def test_step_appears_done_tool_error_blocks():
    """[ERROR] tool result → step not done regardless of intent."""
    plan_dict = json.loads(PHASED_PLAN_JSON)
    tree = PlanTree.from_phased_plan(plan_dict)
    attention = tree.nodes["A1"]
    activator = Activator.__new__(Activator)
    run = MagicMock()
    run.messages = [{"role": "tool", "content": "[ERROR] command failed"}]
    from cat.surprise import SurpriseVector
    sv = SurpriseVector()
    assert not activator._step_appears_done(run, attention, sv)


# ── R12 Bug B: verifier_outcome inference from bash/test output ────────────


def _mk_run_with_last_bash(command: str, output: str):
    """Build a minimal ActivatorRun-like object exposing run.messages so
    _infer_verifier_outcome can walk back to the assistant tool_call."""
    run = MagicMock()
    run.messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": command})},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": output},
    ]
    return run


def test_infer_verifier_outcome_pytest_pass():
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "python -m pytest tests/",
        "=========== 42 passed in 1.23s ===========",
    )
    assert activator._infer_verifier_outcome(run) == "PASS"


def test_infer_verifier_outcome_pytest_fail():
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "python -m pytest tests/",
        "=========== 3 failed, 39 passed in 1.23s ===========",
    )
    assert activator._infer_verifier_outcome(run) == "FAIL"


def test_infer_verifier_outcome_django_runtests_fail():
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "python tests/runtests.py auth_tests",
        "Ran 12 tests in 0.5s\nFAILED (failures=1)",
    )
    assert activator._infer_verifier_outcome(run) == "FAIL"


def test_infer_verifier_outcome_traceback_is_fail():
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "pytest -q",
        "Traceback (most recent call last):\n  File ...\nImportError: no such module",
    )
    assert activator._infer_verifier_outcome(run) == "FAIL"


def test_infer_verifier_outcome_tool_error_is_fail():
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "pytest tests/",
        "[ERROR] timed out after 120s",
    )
    assert activator._infer_verifier_outcome(run) == "FAIL"


def test_infer_verifier_outcome_non_test_command_returns_none():
    """A `bash` ls call must NOT be interpreted as a verifier outcome."""
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "ls -la",
        "passed/  failed/  README.md",  # words that look like markers but aren't a test run
    )
    assert activator._infer_verifier_outcome(run) is None


def test_infer_verifier_outcome_no_tests_ran_is_none():
    """`no tests ran` is too ambiguous to advance a verify step."""
    activator = Activator.__new__(Activator)
    run = _mk_run_with_last_bash(
        "pytest tests/missing_module/",
        "==== no tests ran in 0.01s ====",
    )
    assert activator._infer_verifier_outcome(run) is None


def test_infer_verifier_outcome_empty_messages():
    activator = Activator.__new__(Activator)
    run = MagicMock()
    run.messages = []
    assert activator._infer_verifier_outcome(run) is None


# ── R16: oracle command bound to verify steps ─────────────────────────────


def test_bind_oracle_to_verify_steps_injects_command():
    """Verify steps without a concrete command should get the oracle command
    from contract.representations (key='judge_strategy', 'oracle: <cmd>')."""
    from cat.contracts import Representation
    plan_dict = json.loads(PHASED_PLAN_JSON)
    # Add a verify step so the binder has something to bind
    plan_dict["phases"].append({"name": "VERIFY", "steps": [
        {"description": "Run the oracle test command",
         "intent": "verify",
         "expects": {"verifier_outcome": "PASS"}}
    ]})
    tree = PlanTree.from_phased_plan(plan_dict)
    activator = Activator.__new__(Activator)
    contract = MagicMock()
    contract.representations = [
        Representation(key="judge_strategy", value="oracle: cd /testbed && python -m pytest tests/foo.py", evidence=""),
    ]
    n_bound = activator._bind_oracle_to_verify_steps(tree, contract)
    assert n_bound == 1
    verify_nodes = [n for n in tree.nodes.values() if getattr(n, "intent", None) == "verify"]
    assert len(verify_nodes) == 1
    assert verify_nodes[0].suggested_tool == "bash"
    assert verify_nodes[0].suggested_args["command"].startswith("cd /testbed")


def test_bind_oracle_skips_when_command_already_present():
    """If the Stipulator already provided a concrete command, don't overwrite it."""
    from cat.contracts import Representation
    plan_dict = json.loads(PHASED_PLAN_JSON)
    plan_dict["phases"].append({"name": "VERIFY", "steps": [
        {"description": "Run pytest",
         "intent": "verify",
         "suggested_tool": "bash",
         "suggested_args": {"command": "pytest -x"},
         "expects": {"verifier_outcome": "PASS"}}
    ]})
    tree = PlanTree.from_phased_plan(plan_dict)
    activator = Activator.__new__(Activator)
    contract = MagicMock()
    contract.representations = [
        Representation(key="judge_strategy", value="oracle: python -m pytest different.py", evidence=""),
    ]
    n_bound = activator._bind_oracle_to_verify_steps(tree, contract)
    assert n_bound == 0
    verify_nodes = [n for n in tree.nodes.values() if getattr(n, "intent", None) == "verify"]
    assert verify_nodes[0].suggested_args["command"] == "pytest -x"  # untouched


def test_bind_oracle_no_op_without_oracle_strategy():
    """When representations don't include oracle judge_strategy, return 0."""
    plan_dict = json.loads(PHASED_PLAN_JSON)
    plan_dict["phases"].append({"name": "VERIFY", "steps": [
        {"description": "Run pytest", "intent": "verify"}
    ]})
    tree = PlanTree.from_phased_plan(plan_dict)
    activator = Activator.__new__(Activator)
    contract = MagicMock()
    contract.representations = []  # no judge_strategy
    n_bound = activator._bind_oracle_to_verify_steps(tree, contract)
    assert n_bound == 0


# ── R12 Bug A: _observe_phase must no-op on attention_node=None ────────────


def test_observe_phase_noops_when_attention_none():
    """When stipulator falls back to flat plan (plan_tree=None), the loop
    sets attention_node=None. _observe_phase must not dereference .intent."""
    activator = Activator.__new__(Activator)
    activator.recorder = MagicMock()
    activator.librarian_writer = None
    contract = MagicMock()
    run = MagicMock()
    # Would crash on attention_node.intent if not guarded
    activator._observe_phase(
        contract, run,
        plan_tree=None, attention_node=None,
        actual_cost=100.0, state_now_class="", oc="abc",
    )
    # No surprise event should have been recorded
    activator.recorder.append.assert_not_called()
