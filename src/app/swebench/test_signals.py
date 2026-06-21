"""Extract high-signal facts from test-runner output."""
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
    __test__ = False

    test_id: str = ""
    assertion: str = ""
    top_repo_frame: str = ""
    repo_frames: list[tuple[str, int, str]] = field(default_factory=list)

    def is_useful(self) -> bool:
        return bool(self.test_id or self.assertion or self.top_repo_frame)

    def format_for_stipulator(self) -> str:
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
    """Parse a pytest run's stdout/stderr into key signals."""
    if not output:
        return TestFailureSignals()

    sig = TestFailureSignals()

    m = _TEST_NODEID_RE.search(output)
    if m:
        sig.test_id = m.group(1)

    m = _ASSERTION_RE.search(output)
    if m:
        sig.assertion = m.group(1).strip()

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
        rel = path.replace(repo_root_norm, "").lstrip("/")
        sig.repo_frames.append((rel, line, func))

    if sig.repo_frames:
        rel, line, func = sig.repo_frames[-1]
        sig.top_repo_frame = f"{func} ({rel}:{line})"

    return sig


__all__ = ["extract_pytest_signals", "TestFailureSignals"]
