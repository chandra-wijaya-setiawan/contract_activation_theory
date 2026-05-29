"""Tests for run_report tool-economy extraction (1.8.0)."""
from __future__ import annotations

from cat.run_report import _tool_usage_from_events


def _ev(kind, **payload):
    return {"kind": kind, "payload": payload}


def _tc(tool_call_id, tool, **args):
    return _ev("tool_call", tool_call_id=tool_call_id, tool=tool, args=args)


def test_tool_usage_counts_perform_layer_only():
    """rep-* and oracle-* tool calls don't count toward tool_usage."""
    events = [
        _tc("rep-001", "git", command="log"),         # REPRESENT probe — skipped
        _tc("oracle-x", "bash", command="pytest"),    # VERIFY — skipped
        _tc("tc-abc", "read", path="foo.py"),         # PERFORM ✓
        _tc("tc-def", "bash", command="ls"),          # PERFORM ✓
        _tc("tc-ghi", "read", path="bar.py"),         # PERFORM ✓
    ]
    counts, flags = _tool_usage_from_events(events)
    assert counts == {"read": 2, "bash": 1}


def test_tool_usage_detects_back_to_back_repetition():
    """Three consecutive identical-arg calls flagged as back-to-back waste."""
    events = [
        _tc("tc-1", "read", path="big.py"),
        _tc("tc-2", "read", path="big.py"),
        _tc("tc-3", "read", path="big.py"),
        _tc("tc-4", "bash", command="ls"),
    ]
    counts, flags = _tool_usage_from_events(events)
    assert counts == {"read": 3, "bash": 1}
    assert any("back-to-back" in f and "big.py" in f for f in flags)


def test_tool_usage_detects_4plus_reads_on_same_file():
    """Even non-consecutive: 4+ reads on the same file → suggest outline."""
    events = [
        _tc("tc-1", "read", path="hot.py"),
        _tc("tc-2", "bash", command="grep x"),
        _tc("tc-3", "read", path="hot.py"),
        _tc("tc-4", "bash", command="ls"),
        _tc("tc-5", "read", path="hot.py"),
        _tc("tc-6", "bash", command="pytest"),
        _tc("tc-7", "read", path="hot.py"),
    ]
    counts, flags = _tool_usage_from_events(events)
    assert counts["read"] == 4
    assert any("outline" in f and "hot.py" in f for f in flags)


def test_tool_usage_no_flags_for_efficient_run():
    """A run that hits each file once should produce zero flags."""
    events = [
        _tc("tc-1", "outline", path="a.py"),
        _tc("tc-2", "read", path="a.py"),
        _tc("tc-3", "str_replace", path="a.py"),
        _tc("tc-4", "bash", command="pytest"),
    ]
    counts, flags = _tool_usage_from_events(events)
    assert counts == {"outline": 1, "read": 1, "str_replace": 1, "bash": 1}
    assert flags == []
