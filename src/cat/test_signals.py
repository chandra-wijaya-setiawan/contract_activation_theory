"""Backward-compat shim — import from legacy location."""
from cat._legacy.test_signals import extract_pytest_signals, TestFailureSignals

__all__ = ["extract_pytest_signals", "TestFailureSignals"]
