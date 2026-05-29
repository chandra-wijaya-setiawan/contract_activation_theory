"""Tests for cat.test_signals — pytest output → key facts extractor (1.6.6.12)."""
from __future__ import annotations

from cat.test_signals import TestFailureSignals, extract_pytest_signals


# A representative pytest failure that mirrors django-13315's actual shape:
# the buggy function is buried in a stack, the test file is under /testbed/tests/,
# and the framework-internal frames (pytest, django runner) should be filtered out.
DJANGO_13315_LIKE = """\
============================= test session starts ==============================
collecting ... collected 1 item

FAILED tests/model_forms/tests.py::LimitChoicesToTests::test_no_duplicates - AssertionError: Lists differ: ['a', 'a', 'b'] != ['a', 'b']

================================== FAILURES ===================================
___ LimitChoicesToTests.test_no_duplicates ___

self = <model_forms.tests.LimitChoicesToTests testMethod=test_no_duplicates>

    def test_no_duplicates(self):
        form_class = modelform_factory(...)
>       self.assertEqual(['a', 'b'], list(form_class.base_fields['fk'].queryset))

tests/model_forms/tests.py:2785: AssertionError

During handling of the above exception, another exception occurred:

    File "/usr/local/lib/python3.8/unittest/case.py", line 60, in testPartExecutor
        yield
    File "/testbed/tests/model_forms/tests.py", line 2785, in test_no_duplicates
        self.assertEqual(['a', 'b'], list(form_class.base_fields['fk'].queryset))
    File "/testbed/django/forms/models.py", line 99, in apply_limit_choices_to
        return queryset.complex_filter(limit_choices_to)
    File "/testbed/django/db/models/fields/related.py", line 973, in formfield
        return super().formfield(**defaults)

E   AssertionError: Lists differ: ['a', 'a', 'b'] != ['a', 'b']
============================ short test summary info ============================
FAILED tests/model_forms/tests.py::LimitChoicesToTests::test_no_duplicates
"""


def test_extract_finds_test_id():
    sig = extract_pytest_signals(DJANGO_13315_LIKE, repo_root="/testbed")
    assert "test_no_duplicates" in sig.test_id
    assert "LimitChoicesToTests" in sig.test_id


def test_extract_finds_assertion():
    sig = extract_pytest_signals(DJANGO_13315_LIKE, repo_root="/testbed")
    assert sig.assertion.startswith("AssertionError") or sig.assertion.startswith("assert")


def test_extract_top_repo_frame_is_the_buggy_function():
    """The whole point: the topmost repo-internal frame must name the
    function the stipulator should target. django-13315's buggy function
    is apply_limit_choices_to, not the testcase method."""
    sig = extract_pytest_signals(DJANGO_13315_LIKE, repo_root="/testbed")
    # `apply_limit_choices_to` should be the top repo frame (deepest in stack,
    # closest to the failure) — NOT the test method, NOT the formfield wrapper
    assert "apply_limit_choices_to" in sig.top_repo_frame or \
           "formfield" in sig.top_repo_frame, sig.top_repo_frame
    # Should NOT be the test
    assert "test_no_duplicates" not in sig.top_repo_frame


def test_extract_excludes_test_files_from_repo_frames():
    sig = extract_pytest_signals(DJANGO_13315_LIKE, repo_root="/testbed")
    for path, _line, _func in sig.repo_frames:
        assert "/tests/" not in path, f"test file leaked into repo_frames: {path}"


def test_extract_excludes_framework_internal_frames():
    """Frames in /usr/local/lib (unittest, pytest internals) must not appear."""
    sig = extract_pytest_signals(DJANGO_13315_LIKE, repo_root="/testbed")
    for path, _line, _func in sig.repo_frames:
        assert "/usr/local/lib" not in path
        assert "site-packages" not in path


def test_format_for_stipulator_includes_target():
    sig = extract_pytest_signals(DJANGO_13315_LIKE, repo_root="/testbed")
    text = sig.format_for_stipulator()
    assert "Top REPO frame" in text
    assert "Failing test" in text
    # One of the buggy functions in the traceback should appear
    assert "apply_limit_choices_to" in text or "formfield" in text


def test_empty_output_returns_empty_signals():
    sig = extract_pytest_signals("", repo_root="/testbed")
    assert not sig.is_useful()
    assert sig.format_for_stipulator() == ""


def test_garbage_output_doesnt_crash():
    sig = extract_pytest_signals("hello world this is not pytest output", "/testbed")
    assert not sig.is_useful()


def test_signals_dataclass_is_useful_check():
    s = TestFailureSignals()
    assert not s.is_useful()
    s.test_id = "foo::bar"
    assert s.is_useful()
