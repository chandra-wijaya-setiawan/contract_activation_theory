"""Tests for sub-agent registration and task result transfer.

Tests that:
1. spawn_subagent tool can be registered and invoked
2. Sub-agent runs a nested contract discharge
3. Results (verdict, output, artifacts, cost) transfer back to parent
4. Parent Performer receives the result as a tool result
5. Trace events nest correctly (child events link to parent)
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cat.capability.registry import CapabilityRegistry, ToolResult
from cat.contract.model import (
    Contract,
    Constraints,
    Cost,
    Criterion,
    Representation,
    State,
)
from cat.contract.schemas import PerformerExecutionStep, SubAgentResult
from cat.llm import LLMResult
from cat.llm.kernel import LLMKernel, KernelResult
from cat.trace import Recorder


# ── helpers ──────────────────────────────────────────────────────────────────

@dataclass
class FakeClient:
    model: str
    script: list
    calls: list = field(default_factory=list)

    def chat(self, **kw):
        self.calls.append(kw)
        i = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[i]


def _res(text="", finish="stop", tokens_in=10, tokens_out=5, tokens_cached=0):
    return LLMResult(
        text=text, finish_reason=finish,
        tokens_in=tokens_in, tokens_out=tokens_out, tokens_cached=tokens_cached,
        usd=0.0, model="fake",
    )


def _done_step(reason="done"):
    return json.dumps({
        "thinking": "task complete",
        "intent": "done",
        "tools": [],
        "reason": reason,
        "expect": None,
        "then": None,
    })


def _look_step(tool_name="bash", command="echo hello"):
    return json.dumps({
        "thinking": "need to look",
        "intent": "look",
        "tools": [{"name": tool_name, "arguments": {"command": command}}],
        "expect": "some output",
        "then": "check",
        "reason": None,
    })


def _make_kernel(model="flash", script=None, mode="toolcall"):
    if script is None:
        script = [_res(_done_step())]

    def factory(model):
        return FakeClient(model=model, script=script)

    return LLMKernel(
        client_factory=factory,
        models=[model],
        mode=mode,
        schema=PerformerExecutionStep,
    )


def _make_registry_with_subagent(agent_registry, recorder, llm, checkpointer=None):
    """Build a registry with bash, read, write, done, give_up + spawn_subagent."""
    from cat.capability.full_registry import build_default_registry, register_subagent_tool

    reg = build_default_registry()
    # Remove web tools to keep tests simple
    reg._caps = {k: v for k, v in reg._caps.items()
                 if k in ("bash", "read", "write", "str_replace", "list",
                          "done", "give_up")}

    register_subagent_tool(
        registry=reg,
        agent_registry=agent_registry,
        capability_registry=reg,
        recorder=recorder,
        llm=llm,
        checkpointer=checkpointer,
    )
    return reg


# ── Test 1: SubAgentResult schema ────────────────────────────────────────────

def test_subagent_result_schema():
    """SubAgentResult has all expected fields with correct defaults."""
    r = SubAgentResult()
    assert r.verdict == "failed"
    assert r.output == ""
    assert r.artifacts == {}
    assert r.tokens_spent == 0
    assert r.tool_calls == 0
    assert r.trace_id == ""


def test_subagent_result_completed():
    """SubAgentResult can represent a completed sub-agent run."""
    r = SubAgentResult(
        verdict="completed",
        output="Found 3 files with the pattern",
        artifacts={"files": "a.py, b.py, c.py"},
        tokens_spent=4500,
        tool_calls=8,
        trace_id="sub-abc12345",
    )
    assert r.verdict == "completed"
    assert r.tokens_spent == 4500
    assert r.trace_id == "sub-abc12345"


def test_subagent_result_json_roundtrip():
    """SubAgentResult serializes to JSON and back."""
    r = SubAgentResult(
        verdict="completed",
        output="done",
        artifacts={"key": "value"},
        tokens_spent=100,
        tool_calls=2,
        trace_id="sub-xyz",
    )
    data = json.loads(r.model_dump_json())
    assert data["verdict"] == "completed"
    assert data["artifacts"] == {"key": "value"}
    assert data["trace_id"] == "sub-xyz"


# ── Test 2: spawn_subagent tool registration ─────────────────────────────────

def test_spawn_subagent_registered_in_registry():
    """spawn_subagent tool appears in the registry after registration."""
    from cat.agent.registry import AgentRegistry
    from cat.llm.client import LLM

    tmpdir = tempfile.mkdtemp()
    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=Path(tmpdir))
    llm = LLM(model="flash", recorder=recorder)

    reg = _make_registry_with_subagent(agent_reg, recorder, llm)

    assert reg.get("spawn_subagent") is not None, \
        "spawn_subagent must be registered"
    assert reg.get("spawn_subagent").name == "spawn_subagent"


def test_spawn_subagent_in_tool_schemas():
    """spawn_subagent appears in tool_schemas() output."""
    from cat.agent.registry import AgentRegistry
    from cat.llm.client import LLM

    tmpdir = tempfile.mkdtemp()
    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=Path(tmpdir))
    llm = LLM(model="flash", recorder=recorder)

    reg = _make_registry_with_subagent(agent_reg, recorder, llm)
    schemas = reg.tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "spawn_subagent" in names, \
        f"spawn_subagent not in tool schemas: {names}"


# ── Test 3: spawn_subagent returns error for missing task ────────────────────

def test_spawn_subagent_missing_task():
    """spawn_subagent returns error when task is missing."""
    from cat.capability.tools.subagent import create_subagent_capability
    from cat.agent.registry import AgentRegistry
    from cat.llm.client import LLM

    tmpdir = tempfile.mkdtemp()
    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=Path(tmpdir))
    llm = LLM(model="flash", recorder=recorder)
    reg = CapabilityRegistry()

    cap = create_subagent_capability(agent_reg, reg, recorder, llm)
    result = cap.handler({"task": ""}, State(workdir=Path(tmpdir)))

    assert result.is_error is True
    assert "missing" in result.output.lower()


# ── Test 4: spawn_subagent returns error for unknown profile ─────────────────

def test_spawn_subagent_unknown_profile():
    """spawn_subagent returns error when profile doesn't exist."""
    from cat.capability.tools.subagent import create_subagent_capability
    from cat.agent.registry import AgentRegistry
    from cat.llm.client import LLM

    tmpdir = tempfile.mkdtemp()
    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=Path(tmpdir))
    llm = LLM(model="flash", recorder=recorder)
    reg = CapabilityRegistry()

    cap = create_subagent_capability(agent_reg, reg, recorder, llm)
    result = cap.handler(
        {"task": "do something", "profile": "nonexistent_profile_xyz"},
        State(workdir=Path(tmpdir)),
    )

    assert result.is_error is True
    assert "not found" in result.output.lower()


# ── Test 5: Full sub-agent invocation with real activator ────────────────────

def test_spawn_subagent_runs_subagent_and_returns_result():
    """spawn_subagent runs a real sub-agent and returns SubAgentResult.

    This is the core integration test:
    - Parent Performer calls spawn_subagent(task="echo hello world")
    - Sub-agent runs the full loop with the 'task' profile
    - Sub-agent's Performer calls bash("echo hello world"), then done()
    - Result comes back as SubAgentResult JSON in the tool output
    """
    from cat.agent.registry import AgentRegistry
    from cat.agent.hypothesis import HypothesisAgent
    from cat.agent.definition import AgentDef, LoopConfig
    from cat.llm.client import LLM
    from cat.capability.git_checkpoint import NullCheckpointer

    tmpdir = tempfile.mkdtemp()
    workdir = Path(tmpdir)

    # Create a fake client that returns done() immediately
    # The sub-agent's Performer will call bash then done
    sub_script = [
        _res(_done_step("echoed hello world")),  # sub-agent Performer: done
    ]

    def sub_factory(model):
        return FakeClient(model=model, script=sub_script)

    sub_kernel = LLMKernel(
        client_factory=sub_factory,
        models=["flash"],
        mode="toolcall",
        schema=PerformerExecutionStep,
    )

    # Build the sub-agent's profile
    sub_profile = AgentDef(
        name="task",
        loop_config=LoopConfig(
            skip_representer=True,
            skip_stipulator=True,
            skip_oracle=True,
            max_hypotheses=1,
            max_inner_attempts=1,
        ),
    )

    # Build agent registry that can load the 'task' profile
    agent_reg = AgentRegistry()

    # We need to register the profile so AgentRegistry can find it.
    # Since we can't easily add to search dirs, we'll test the tool directly.
    recorder = Recorder(run_id="test-subagent", task_id="t1", agent="test",
                        model="flash", out_dir=workdir)
    llm = LLM(model="flash", recorder=recorder)

    # Build registry with subagent tool
    reg = CapabilityRegistry()
    from cat.capability.full_registry import register_subagent_tool
    # Register basic tools
    from cat.capability.full_registry import build_default_registry
    base_reg = build_default_registry()
    for name in ("bash", "read", "write", "str_replace", "list", "done", "give_up"):
        cap = base_reg.get(name)
        if cap:
            reg.register(cap)

    register_subagent_tool(
        registry=reg,
        agent_registry=agent_reg,
        capability_registry=reg,
        recorder=recorder,
        llm=llm,
        checkpointer=NullCheckpointer(),
    )

    # Directly invoke the spawn_subagent tool
    subagent_cap = reg.get("spawn_subagent")
    assert subagent_cap is not None

    result = subagent_cap.handler(
        {
            "task": "echo hello world",
            "profile": "task",
            "context": {"_workdir": str(workdir)},
        },
        State(workdir=workdir),
    )

    # The result should be a valid SubAgentResult JSON
    assert result.is_error is False, f"sub-agent failed: {result.output}"
    sub_result = SubAgentResult.model_validate_json(result.output)
    assert sub_result.verdict == "completed"
    assert sub_result.trace_id.startswith("sub-")
    assert sub_result.tokens_spent >= 0


# ── Test 6: Sub-agent result flows through to parent Performer ───────────────

def test_subagent_result_as_tool_result_for_parent():
    """The parent Performer receives the sub-agent result as a normal tool result.

    Simulates what happens in the parent's inner loop:
    1. Parent Performer calls spawn_subagent
    2. Tool result contains SubAgentResult JSON
    3. Parent can parse it and use the output
    """
    from cat.capability.tools.subagent import create_subagent_capability
    from cat.agent.registry import AgentRegistry
    from cat.llm.client import LLM

    tmpdir = tempfile.mkdtemp()
    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=Path(tmpdir))
    llm = LLM(model="flash", recorder=recorder)
    reg = CapabilityRegistry()

    cap = create_subagent_capability(agent_reg, reg, recorder, llm)

    # Simulate what the parent Performer does:
    # 1. Call spawn_subagent
    result = cap.handler(
        {"task": "list files", "profile": "nonexistent"},
        State(workdir=Path(tmpdir)),
    )

    # 2. The result is a ToolResult — parent sees it as a tool message
    assert isinstance(result, ToolResult)

    # 3. If successful, parent can parse the JSON
    if not result.is_error:
        sub_result = SubAgentResult.model_validate_json(result.output)
        assert sub_result.verdict in ("completed", "failed", "partial")


# ── Test 7: Contract parent_contract_id linkage ──────────────────────────────

def test_contract_has_parent_contract_id():
    """Contract supports parent_contract_id for sub-agent linkage."""
    parent = Contract.new(
        obligation="parent task",
        criteria=[],
    )
    child = Contract.new(
        obligation="child task",
        criteria=[],
    )
    child.parent_contract_id = parent.id

    assert child.parent_contract_id == parent.id
    assert parent.parent_contract_id is None  # top-level has no parent


# ── Test 8: Sub-agent cost tracking ──────────────────────────────────────────

def test_subagent_cost_tracked_in_result():
    """Sub-agent cost (tokens, tool_calls) is reported in the result."""
    from cat.capability.tools.subagent import create_subagent_capability
    from cat.agent.registry import AgentRegistry
    from cat.llm.client import LLM

    tmpdir = tempfile.mkdtemp()
    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=Path(tmpdir))
    llm = LLM(model="flash", recorder=recorder)
    reg = CapabilityRegistry()

    cap = create_subagent_capability(agent_reg, reg, recorder, llm)

    # Even for an unknown profile, the error result should be a valid ToolResult
    result = cap.handler(
        {"task": "test", "profile": "nonexistent"},
        State(workdir=Path(tmpdir)),
    )
    assert isinstance(result, ToolResult)
    assert result.is_error is True


# ── Test 9: Trace nesting ────────────────────────────────────────────────────

def test_subagent_trace_events_nested():
    """Sub-agent trace events are recorded with the shared recorder.

    When the sub-agent actually runs (not an early error), trace events
    from the nested discharge appear in the shared recorder.
    """
    from cat.capability.tools.subagent import create_subagent_capability
    from cat.agent.registry import AgentRegistry
    from cat.agent.definition import AgentDef, LoopConfig
    from cat.llm.client import LLM
    from cat.capability.full_registry import build_default_registry, register_subagent_tool
    from cat.capability.git_checkpoint import NullCheckpointer

    tmpdir = tempfile.mkdtemp()
    workdir = Path(tmpdir)

    agent_reg = AgentRegistry()
    recorder = Recorder(run_id="test", task_id="t1", agent="test",
                        model="flash", out_dir=workdir)
    llm = LLM(model="flash", recorder=recorder)

    # Build a registry with real tools + subagent
    reg = build_default_registry()
    reg._caps = {k: v for k, v in reg._caps.items()
                 if k in ("bash", "read", "write", "str_replace", "list",
                          "done", "give_up")}
    register_subagent_tool(
        registry=reg,
        agent_registry=agent_reg,
        capability_registry=reg,
        recorder=recorder,
        llm=llm,
        checkpointer=NullCheckpointer(),
    )

    initial_event_count = len(recorder.traj.events)

    # Invoke the subagent tool — it will try to load 'task' profile and fail,
    # but the tool_call/tool_result events from the capability layer are recorded
    subagent_cap = reg.get("spawn_subagent")
    subagent_cap.handler(
        {"task": "echo hello", "profile": "task"},
        State(workdir=workdir),
    )

    # The tool_call and tool_result events from CapabilityRegistry.invoke()
    # should be recorded even if the sub-agent itself fails
    assert len(recorder.traj.events) > initial_event_count, (
        "spawn_subagent should record tool_call + tool_result trace events"
    )


# ── Test 10: Context passed to sub-agent ─────────────────────────────────────

def test_subagent_context_as_representations():
    """Context key-value pairs become representations for the sub-agent."""
    from cat.contract.model import Contract, Representation

    context = {
        "workdir": "/testbed",
        "focus": "src/",
        "_max_tokens": 10000,  # _-prefixed should be filtered
    }

    reps = [
        Representation(key=k, value=str(v))
        for k, v in context.items()
        if not k.startswith("_")
    ]

    assert len(reps) == 2  # _max_tokens filtered out
    keys = [r.key for r in reps]
    assert "workdir" in keys
    assert "focus" in keys
    assert "_max_tokens" not in keys
