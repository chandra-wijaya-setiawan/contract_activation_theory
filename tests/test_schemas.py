"""Unit tests for CAT v3 hypothesis engine schemas."""
import pytest
from pydantic import ValidationError

from cat.contract.schemas import (
    AttestorV3Schema,
    ExecutionSchema,
    ExpectsSchema,
    HypothesisSchema,
    InvocationSchema,
    KnowledgeSchema,
    OutcomeSchema,
    PerformerExecutionStep,
    PerformerPlanSchema,
    PerformerTaskSchema,
    ToolCall,
)


# ── HypothesisSchema ─────────────────────────────────────────────────────────

def test_hypothesis_schema_basic():
    h = HypothesisSchema(location="file.py::fn", change="add .distinct()", predicts="test passes")
    assert h.location == "file.py::fn"
    assert h.basis == ""
    assert h.start_from == "base"


def test_hypothesis_schema_basis_optional():
    h = HypothesisSchema(location="file.py", change="fix X", predicts="Y passes")
    assert h.basis == ""


# ── PerformerTaskSchema ───────────────────────────────────────────────────────

def test_performer_task_schema_defaults():
    h = HypothesisSchema(location="file.py", change="do X", predicts="Y")
    task = PerformerTaskSchema(obligation="fix bug Z", hypothesis=h, oracle="pytest tests/")
    assert task.max_attempts == 5
    assert task.context == ""


# ── KnowledgeSchema ───────────────────────────────────────────────────────────

def test_knowledge_schema_empty():
    k = KnowledgeSchema()
    assert k.findings == {}
    assert k.failed_hypotheses == []
    assert k.oracle_failures == []


def test_knowledge_schema_accumulation():
    k = KnowledgeSchema()
    k.findings["file.py"] = "contains function foo at line 42"
    k.failed_hypotheses.append("h1: change was wrong")
    k.oracle_failures.append("FAILED: test_foo AssertionError")
    assert len(k.findings) == 1
    assert len(k.failed_hypotheses) == 1
    assert len(k.oracle_failures) == 1


# ── AttestorV3Schema ──────────────────────────────────────────────────────────

def test_attestor_v3_schema_new_hypothesis():
    a = AttestorV3Schema(verdict="new_hypothesis", reason="change was wrong")
    assert a.verdict == "new_hypothesis"
    assert a.diagnosis == ""


def test_attestor_v3_schema_give_up():
    a = AttestorV3Schema(verdict="give_up", reason="all locations tried")
    assert a.verdict == "give_up"


def test_attestor_v3_schema_rejects_invalid_verdict():
    with pytest.raises(ValidationError):
        AttestorV3Schema(verdict="patch_plan", reason="...")  # v2 verdict — invalid in v3


# ── ExpectsSchema + ExecutionSchema ──────────────────────────────────────────

def test_expects_schema_defaults():
    e = ExpectsSchema()
    assert e.durable_change is False
    assert e.files_modified == []
    assert e.verifier_outcome == "N/A"


def test_execution_schema_defaults():
    ex = ExecutionSchema()
    assert ex.intent == "other"
    assert ex.suggested_tool is None
    assert ex.suggested_args == {}


# ── PerformerExecutionStep validator ─────────────────────────────────────────

def test_performer_step_look_requires_tools():
    with pytest.raises(ValidationError, match="requires at least one tool call"):
        PerformerExecutionStep(intent="look", tools=[], expect="something", then="look")


def test_performer_step_look_requires_expect():
    with pytest.raises(ValidationError, match="requires expect"):
        PerformerExecutionStep(
            intent="look",
            tools=[ToolCall(name="read", arguments={"path": "f.py"})],
            expect=None,
            then="look",
        )


def test_performer_step_look_requires_then():
    with pytest.raises(ValidationError, match="requires then"):
        PerformerExecutionStep(
            intent="look",
            tools=[ToolCall(name="read", arguments={"path": "f.py"})],
            expect="line 42",
            then=None,
        )


def test_performer_step_done_requires_reason():
    with pytest.raises(ValidationError, match="requires a reason"):
        PerformerExecutionStep(intent="done", reason=None)


def test_performer_step_done_must_not_have_tools():
    with pytest.raises(ValidationError, match="must not have tools"):
        PerformerExecutionStep(
            intent="done",
            reason="task complete",
            tools=[ToolCall(name="bash", arguments={"command": "echo hi"})],
        )


def test_performer_step_give_up_valid():
    step = PerformerExecutionStep(intent="give_up", reason="all attempts failed")
    assert step.intent == "give_up"
    assert step.tools == []


def test_performer_step_change_valid():
    step = PerformerExecutionStep(
        intent="change",
        tools=[ToolCall(name="str_replace", arguments={"path": "f.py", "old_str": "x", "new_str": "y"})],
        expect="file updated",
        then="check",
    )
    assert step.intent == "change"
    assert step.then == "check"


# ── InvocationSchema + OutcomeSchema ─────────────────────────────────────────

def test_invocation_schema_succeeded_property():
    inv = InvocationSchema(tool_name="bash", execution_status="success")
    assert inv.succeeded is True
    inv_err = InvocationSchema(tool_name="bash", execution_status="error", error="cmd not found")
    assert inv_err.succeeded is False


def test_outcome_schema_defaults():
    o = OutcomeSchema()
    assert o.verdict == "exhausted"
    assert o.any_durable_change is False
    assert o.invocations == []


# ── PerformerPlanSchema ───────────────────────────────────────────────────────

def test_performer_plan_schema_defaults():
    p = PerformerPlanSchema()
    assert p.steps == []
    assert p.next_step is None
    assert p.confidence == "medium"
