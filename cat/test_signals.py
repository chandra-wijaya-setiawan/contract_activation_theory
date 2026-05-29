"""Extract high-signal facts from test-runner output.

DOMAIN NOTE: this module is **Python-pytest domain infra**. It belongs under
`cat/domains/code_python/` when v2.x splits domain-specific concerns out of
the framework core. Kept at `cat/test_signals.py` today for minimum change;
breadcrumb left in the docstring so the 2.x extraction knows where to look.

Purpose
-------
The stipulator plans poorly when its only evidence is a 2.5KB raw pytest
traceback — the function that actually needs to change is buried in stack
frames. By extracting a few high-signal facts up front (which test failed,
what it asserted, where the failure surfaces in the repo), the stipulator
sees the target function explicitly and is far more likely to plan a fix at
the right location.

Concrete failure this addresses: django-13315 had `apply_limit_choices_to`
in the traceback, but the stipulator planned to fix `ForeignKey.formfield`
instead — a function that *calls* the buggy one, on the wrong file.

Output shape
------------
TestFailureSignals(
    test_id="tests/model_forms/tests.py::FooTests::test_limit_choices_to",
    assertion="AssertionError: Lists differ: [...] != [...]",
    top_repo_frame="apply_limit_choices_to (django/forms/models.py:99)",
    repo_frames=[...],  # all repo-internal frames, top-to-bottom
)

When fields can't be extracted, they fall back to empty strings/lists — the
caller checks truthiness rather than asking for try/except.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_TEST_NODEID_RE = re.compile(r"FAILED\s+([^\s:]+(?:::[^\s:]+)+)")
_ASSERTION_RE = re.compile(
    r"(AssertionError:[^\n]+|assert [^\n]+|E\s+\S[^\n]+)", re.MULTILINE
)
_FRAME_RE = re.compile(
    r'File "([^"]+)", line (\d+), in (\S+)'
)


@dataclass
class TestFailureSignals:
    """High-signal facts extracted from a pytest failure output."""
    # Tell pytest's collector this is NOT a test class despite the name
    __test__ = False

    test_id: str = ""
    assertion: str = ""
    top_repo_frame: str = ""
    repo_frames: list[tuple[str, int, str]] = field(default_factory=list)

    def is_useful(self) -> bool:
        """True iff at least one signal was extracted."""
        return bool(self.test_id or self.assertion or self.top_repo_frame)

    def format_for_stipulator(self) -> str:
        """Render as a short prefix to prepend to the raw pytest output."""
        if not self.is_useful():
            return ""
        lines = ["KEY SIGNALS extracted from the test failure:"]
        if self.test_id:
            lines.append(f"  Failing test: {self.test_id}")
        if self.assertion:
            assertion = self.assertion[:200]
            lines.append(f"  Assertion: {assertion}")
        if self.top_repo_frame:
            lines.append(
                f"  Top REPO frame (likely fix target): {self.top_repo_frame}"
            )
        if len(self.repo_frames) > 1:
            others = "; ".join(
                f"{func} ({path}:{line})"
                for path, line, func in self.repo_frames[1:5]
            )
            lines.append(f"  Other repo frames in stack: {others}")
        return "\n".join(lines) + "\n"


def extract_pytest_signals(
    output: str,
    repo_root: str,
) -> TestFailureSignals:
    """Parse a pytest run's stdout/stderr into key signals.

    `repo_root` is the path prefix used to distinguish repo-internal frames
    from framework/library frames. Frames inside `repo_root` AND not under
    a `/tests/` subdirectory are the candidates for "fix target."

    Empty/unparseable output returns an empty TestFailureSignals (use
    .is_useful() to check).
    """
    if not output:
        return TestFailureSignals()

    sig = TestFailureSignals()

    # Test node id — pytest's "FAILED path::Class::method"
    m = _TEST_NODEID_RE.search(output)
    if m:
        sig.test_id = m.group(1)

    # Assertion line — first match is usually the actual assert that failed
    m = _ASSERTION_RE.search(output)
    if m:
        sig.assertion = m.group(1).strip()

    # Stack frames — keep only repo-internal, non-test ones
    repo_root_norm = repo_root.rstrip("/")
    for path, line_str, func in _FRAME_RE.findall(output):
        if repo_root_norm not in path:
            continue
        if "/tests/" in path or path.endswith("_test.py") or "test_" in path.split("/")[-1]:
            continue
        try:
            line = int(line_str)
        except ValueError:
            continue
        # Store as path RELATIVE to repo_root for readability
        rel = path.replace(repo_root_norm, "").lstrip("/")
        sig.repo_frames.append((rel, line, func))

    # In pytest tracebacks the failure surfaces are listed top-down; the
    # LAST repo frame is the one closest to the actual exception. That's
    # typically the right "fix target" (the function where things broke).
    if sig.repo_frames:
        rel, line, func = sig.repo_frames[-1]
        sig.top_repo_frame = f"{func} ({rel}:{line})"

    return sig
