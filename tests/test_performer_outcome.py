"""Tests for cat/plan_schema.py::PerformerOutcome + ToolInvocation.

PerformerOutcome is the structured normalization of one Performer turn.
Closes R19: the text-only-on-mutate-step path that used to be silently
treated as DONE now returns is_terminal() == False, which lets the
activator escalate to the existing stall/Resolver/PlanPatcher pipeline
instead of exiting the loop.

Also collapses three scattered helpers (_infer_durable_change,
_infer_verifier_outcome, [ERROR] checks) into derived properties.
"""
from cat.plan_schema import PerformerOutcome, ToolInvocation


def _inv(tool, args=None, succeeded=True, output_text="", error="",
         durable_change=False, files_modified=None):
    return ToolInvocation(
        tool_name=tool, args=args or {}, succeeded=succeeded,
        output_text=output_text, error=error,
        durable_change=durable_change, files_modified=files_modified or [],
    )


# ── derived properties ────────────────────────────────────────────────────


def test_outcome_no_invocations_is_text_only():
    o = PerformerOutcome(text_response="I'll think about this.")
    assert not o.any_tool_called
    assert not o.any_durable_change
    assert not o.any_tool_errored


def test_outcome_with_successful_write_records_durable_change():
    o = PerformerOutcome(invocations=[
        _inv("str_replace", args={"path": "/x.py"}, durable_change=True,
             files_modified=["/x.py"]),
    ])
    assert o.any_tool_called
    assert o.any_durable_change
    assert not o.any_tool_errored
    assert o.files_modified == ["/x.py"]


def test_outcome_with_failed_tool_marks_errored_no_durable_change():
    o = PerformerOutcome(invocations=[
        _inv("str_replace", succeeded=False, error="old_str not found",
             durable_change=False),
    ])
    assert o.any_tool_called
    assert not o.any_durable_change  # failed → no durable change
    assert o.any_tool_errored


def test_outcome_files_modified_dedupes_and_skips_failed():
    """If two invocations touch the same file (and one fails), the file
    appears once in files_modified and only if the touching one succeeded."""
    o = PerformerOutcome(invocations=[
        _inv("str_replace", durable_change=True, files_modified=["/x.py"]),
        _inv("str_replace", succeeded=False, files_modified=["/y.py"]),
        _inv("str_replace", durable_change=True, files_modified=["/x.py"]),
    ])
    assert o.files_modified == ["/x.py"]


# ── verifier_outcome inference ───────────────────────────────────────────


def test_verifier_outcome_pytest_pass():
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "python -m pytest tests/"},
             output_text="===== 42 passed in 1.23s ====="),
    ])
    assert o.verifier_outcome == "PASS"


def test_verifier_outcome_pytest_fail():
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "python -m pytest tests/"},
             output_text="===== 3 failed, 39 passed in 1.23s ====="),
    ])
    assert o.verifier_outcome == "FAIL"


def test_verifier_outcome_django_runtests_fail():
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "python tests/runtests.py auth_tests"},
             output_text="Ran 12 tests in 0.5s\nFAILED (failures=1)"),
    ])
    assert o.verifier_outcome == "FAIL"


def test_verifier_outcome_traceback_is_fail():
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "pytest -q"},
             output_text="Traceback (most recent call last):\n  File ...\nImportError"),
    ])
    assert o.verifier_outcome == "FAIL"


def test_verifier_outcome_failed_tool_is_fail():
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "pytest tests/"},
             succeeded=False, error="timed out after 120s"),
    ])
    assert o.verifier_outcome == "FAIL"


def test_verifier_outcome_non_test_command_returns_none():
    """A bash ls call must NOT be misread as a verifier outcome."""
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "ls -la"},
             output_text="passed/  failed/  README.md"),
    ])
    assert o.verifier_outcome is None


def test_verifier_outcome_no_tests_ran_is_none():
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "pytest tests/missing/"},
             output_text="==== no tests ran in 0.01s ===="),
    ])
    assert o.verifier_outcome is None


def test_verifier_outcome_only_uses_last_invocation():
    """When multiple invocations exist, only the last one's output drives
    the verifier verdict (since that's the most recent test signal)."""
    o = PerformerOutcome(invocations=[
        _inv("bash", args={"command": "pytest a/"}, output_text="===== 1 failed ====="),
        _inv("bash", args={"command": "pytest b/"}, output_text="===== 1 passed ====="),
    ])
    assert o.verifier_outcome == "PASS"


def test_verifier_outcome_empty_invocations():
    o = PerformerOutcome()
    assert o.verifier_outcome is None


# ── is_terminal: the R19 logic ────────────────────────────────────────────


def test_is_terminal_tool_call_never_terminal():
    """If the Performer made a tool call, this iteration isn't terminal —
    we're continuing work."""
    o = PerformerOutcome(invocations=[_inv("read", args={"path": "/x"})])
    for intent in ("investigate", "design", "mutate", "verify", "other", None):
        assert not o.is_terminal(intent)


def test_is_terminal_text_only_on_investigate_is_terminal():
    """Text-only on investigate is a legit done signal."""
    o = PerformerOutcome(text_response="I've understood the file.")
    assert o.is_terminal("investigate")


def test_is_terminal_text_only_on_design_is_terminal():
    """Text-only on design is a legit done signal (no action expected)."""
    o = PerformerOutcome(text_response="I decided to use approach A.")
    assert o.is_terminal("design")


def test_is_terminal_text_only_on_mutate_is_NOT_terminal():
    """R19 leak: Performer described action without executing it on a mutate
    step. Must be flagged as not-terminal so the stall machinery engages."""
    o = PerformerOutcome(text_response="I will modify foo.py to add the space.")
    assert not o.is_terminal("mutate")


def test_is_terminal_text_only_on_verify_is_NOT_terminal():
    """Same principle: a verify step requires a tool call. Text-only means
    the agent described what would happen but didn't run pytest."""
    o = PerformerOutcome(text_response="The fix should pass the tests.")
    assert not o.is_terminal("verify")


def test_is_terminal_text_only_on_other_is_terminal():
    """Other-intent steps are generic; text-only is OK."""
    o = PerformerOutcome(text_response="...")
    assert o.is_terminal("other")


def test_is_terminal_text_only_none_intent_is_terminal():
    """When attention_intent is unavailable, fall back to terminal=True
    (the safer assumption for the legacy path)."""
    o = PerformerOutcome(text_response="...")
    assert o.is_terminal(None)
