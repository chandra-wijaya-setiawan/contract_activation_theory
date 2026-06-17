"""Backward-compat shim: re-exports TbCatAgent and TbReactAgent from _legacy."""
from cat._legacy.tb_agents import TbCatAgent, TbReactAgent

__all__ = ["TbCatAgent", "TbReactAgent"]
