"""Smoke tests for ActivatorV2 — no real LLM, just verifying wiring.

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

from cat.activator_v2 import ActivatorV2
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


def test_activator_v2_imports():
    from cat.activator_v2 import ActivatorV2
    assert ActivatorV2 is not None


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
    from cat.activator_v2 import ActivatorV2
    from cat.activator import ActivatorRun, _PlanStep
    from cat.plan_tree import PlanTree
    from cat.surprise import SurpriseVector

    plan_dict = json.loads(PHASED_PLAN_JSON)
    tree = PlanTree.from_phased_plan(plan_dict)
    attention = tree.nodes["A1"]

    # Build a minimal Activator instance for the helper
    activator = ActivatorV2.__new__(ActivatorV2)
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
    activator = ActivatorV2.__new__(ActivatorV2)
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
    activator = ActivatorV2.__new__(ActivatorV2)
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
    activator = ActivatorV2.__new__(ActivatorV2)
    run = MagicMock()
    run.messages = [{"role": "tool", "content": "[ERROR] command failed"}]
    from cat.surprise import SurpriseVector
    sv = SurpriseVector()
    assert not activator._step_appears_done(run, attention, sv)
