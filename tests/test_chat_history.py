"""Tests for conversation history (chat REPL continuity) and KV cache tracking.

Two concerns:
  1. KV cache visibility — LLMResult tracks cached_tokens; llm_usage events expose it.
  2. Conversation history — prior turns prepended as real messages to the Performer
     so context survives across chat REPL turns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from cat.llm import LLMResult
from cat.contract.schemas import PerformerTaskSchema, PerformerExecutionStep
from cat.llm.kernel import LLMKernel, KernelResult


# ── helpers shared with test_llm_kernel ───────────────────────────────────────

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


def _done_step():
    """Serialised PerformerExecutionStep that calls done()."""
    return '{"thinking":"ok","intent":"done","tools":[],"reason":"answer","expect":null,"then":null}'


# ── KV cache: LLMResult carries tokens_cached ─────────────────────────────────

def test_llm_result_has_tokens_cached_field():
    r = LLMResult(text="hi", tokens_in=200, tokens_out=30, tokens_cached=120)
    assert r.tokens_cached == 120


def test_llm_result_tokens_cached_defaults_zero():
    r = LLMResult(text="hi", tokens_in=10, tokens_out=5)
    assert r.tokens_cached == 0


def test_kernel_records_cached_tokens_in_usage():
    """Kernel usage_records must include cached_tokens from the LLMResult."""
    script = {"flash": [_res(_done_step(), tokens_in=200, tokens_cached=150)]}

    clients: dict = {}
    def factory(model):
        c = FakeClient(model=model, script=script.get(model, [_res("{}")]))
        clients[model] = c
        return c

    k = LLMKernel(
        client_factory=factory, models=["flash"],
        mode="toolcall", schema=PerformerExecutionStep,
    )
    k.run([{"role": "user", "content": "go"}], tools=[])
    assert len(k.usage_records) == 1
    rec = k.usage_records[0]
    assert "tokens_cached" in rec, "usage_records must expose tokens_cached"
    assert rec["tokens_cached"] == 150


def test_cached_tokens_above_zero_signals_kv_hit():
    """When cached_tokens > 0, the prefix was served from KV cache — confirm it's
    being tracked and that the value flows through as a non-zero integer.
    """
    script = {"flash": [
        _res(_done_step(), tokens_in=100, tokens_cached=0),   # turn 1: cold
        _res(_done_step(), tokens_in=120, tokens_cached=80),  # turn 2: warm hit
    ]}

    clients: dict = {}
    def factory(model):
        c = FakeClient(model=model, script=script.get(model, [_res("{}")]))
        clients[model] = c
        return c

    k = LLMKernel(
        client_factory=factory, models=["flash"],
        mode="toolcall", schema=PerformerExecutionStep,
    )
    k.run([{"role": "user", "content": "turn 1"}], tools=[])
    k.run([{"role": "user", "content": "turn 2"}], tools=[])

    assert k.usage_records[0]["tokens_cached"] == 0,  "turn 1 should be a cold miss"
    assert k.usage_records[1]["tokens_cached"] == 80, "turn 2 should show a cache hit"


# ── Conversation history: PerformerTaskSchema and Contract ────────────────────

def test_performer_task_schema_has_chat_history():
    t = PerformerTaskSchema(obligation="fix the bug")
    assert hasattr(t, "chat_history"), "PerformerTaskSchema must have chat_history"
    assert t.chat_history == []


def test_chat_history_accepted_as_message_pairs():
    history = [
        {"role": "user",      "content": "what is 2+2?"},
        {"role": "assistant", "content": "4"},
    ]
    t = PerformerTaskSchema(obligation="follow up", chat_history=history)
    assert len(t.chat_history) == 2
    assert t.chat_history[0]["role"] == "user"


# ── Conversation history: Activator prepends history to inner_messages ─────────

def test_activator_prepends_chat_history_to_performer_messages():
    """The first user message the Performer sees must be the oldest history entry,
    not the current task — verifying Option B message threading.
    """
    from pathlib import Path
    from cat.agent.hypothesis import HypothesisLoopActivator
    from cat.contract.model import Contract, Constraints, State
    from cat.trace import Recorder
    from cat.capability.registry import CapabilityRegistry
    from cat.agent.definition import AgentDef, LoopConfig
    from cat.capability.git_checkpoint import NullCheckpointer
    from cat.llm.client import LLM
    from cat.llm.kernel import LLMKernel

    captured_messages: list[list[dict]] = []

    def factory(model):
        return FakeClient(model=model, script=[_res(_done_step())] * 10)

    # Wrap a real kernel with our capturing factory
    capturing_kernel = LLMKernel(
        client_factory=factory,
        models=["flash"],
        mode="toolcall",
        schema=PerformerExecutionStep,
    )
    # Monkey-patch run to capture messages
    _orig_run = capturing_kernel.run
    def _capturing_run(messages, **kw):
        captured_messages.append(list(messages))
        return _orig_run(messages, **kw)
    capturing_kernel.run = _capturing_run

    skill = AgentDef(
        name="chat",
        loop_config=LoopConfig(
            skip_representer=True,
            skip_stipulator=True,
            skip_oracle=True,
            max_hypotheses=1,
            max_inner_attempts=1,
        ),
    )

    history = [
        {"role": "user",      "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    contract = Contract.new(
        obligation="what did I say before?",
        criteria=[],
        constraints=Constraints(max_usd=1.0, max_wall_s=60.0),
    )
    contract.chat_history = history

    state = State(workdir=Path("/tmp"))
    recorder = Recorder(run_id="test", task_id="t1", agent="test", model="flash",
                        out_dir=Path("/tmp"))
    registry = CapabilityRegistry()
    llm = LLM(model="flash", recorder=recorder)

    activator = HypothesisLoopActivator(
        llm=llm,
        registry=registry,
        recorder=recorder,
        checkpointer=NullCheckpointer(),
        skill=skill,
        performer_kernel=capturing_kernel,
    )
    activator.run(contract, state)

    assert captured_messages, "Performer must have been called"
    first_call_messages = captured_messages[0]
    user_msgs = [m for m in first_call_messages if m["role"] == "user"]
    assert len(user_msgs) >= 2, "Must have history user msg + current task user msg"
    assert user_msgs[0]["content"] == "hello", (
        "First user message must be history[0], not the current task"
    )
