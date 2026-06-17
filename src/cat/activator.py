"""Backward-compat shim: Activator → HypothesisLoopActivator."""
from cat.engine.activator import HypothesisLoopActivator as Activator
from cat.engine.base import ActivatorRun

__all__ = ["Activator", "ActivatorRun"]
