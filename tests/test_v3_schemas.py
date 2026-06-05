"""Unit tests for CAT v3 hypothesis engine schemas."""
from cat.schemas import (
    HypothesisSchema, PerformerTaskSchema, KnowledgeSchema, AttestorV3Schema
)


def test_hypothesis_schema_basic():
    h = HypothesisSchema(
        location="django/forms/models.py::apply_limit_choices_to_to_formfield",
        change="add .distinct() after .complex_filter(limit_choices_to)",
        predicts="test_limit_choices_to_no_duplicates passes",
        basis="joins from limit_choices_to produce duplicate rows",
    )
    assert "models.py" in h.location
    assert "distinct" in h.change


def test_hypothesis_schema_basis_optional():
    h = HypothesisSchema(location="file.py", change="fix X", predicts="Y passes")
    assert h.basis == ""


def test_performer_task_schema():
    h = HypothesisSchema(location="file.py", change="do X", predicts="Y")
    task = PerformerTaskSchema(
        obligation="fix bug Z",
        hypothesis=h,
        oracle="cd /testbed && python -m pytest tests/",
    )
    assert task.max_attempts == 5
    assert task.context == ""


def test_knowledge_schema_empty():
    k = KnowledgeSchema()
    assert k.findings == {}
    assert k.failed_hypotheses == []
    assert k.oracle_failures == []


def test_knowledge_schema_accumulation():
    k = KnowledgeSchema()
    k.findings["file.py"] = "contains function foo at line 42"
    k.failed_hypotheses.append({"hypothesis": "h1", "verdict": "exhausted"})
    k.oracle_failures.append("FAILED: test_foo AssertionError")
    assert len(k.findings) == 1
    assert len(k.failed_hypotheses) == 1
    assert len(k.oracle_failures) == 1


def test_attestor_v3_schema_new_hypothesis():
    a = AttestorV3Schema(
        verdict="new_hypothesis",
        reason="location existed but change was wrong",
        diagnosis="try forms/models.py instead of related.py",
    )
    assert a.verdict == "new_hypothesis"


def test_attestor_v3_schema_give_up():
    a = AttestorV3Schema(verdict="give_up", reason="all 3 locations tried and none fixed it")
    assert a.verdict == "give_up"
    assert a.diagnosis == ""


def test_attestor_v3_schema_rejects_invalid_verdict():
    from pydantic import ValidationError
    import pytest
    with pytest.raises(ValidationError):
        AttestorV3Schema(verdict="patch_plan", reason="...")  # v2 verdict — invalid in v3
