"""Unit tests for LLMKernel — intelligence dial, model resolution, self-heal.

No network: a FakeClient returns scripted LLMResult objects so we can exercise
the truncation→bump, validation→reask, and exhausted→escalate branches.
"""
from dataclasses import dataclass, field

import pytest
from pydantic import BaseModel

from cat.llm import LLMResult
from cat.llm.kernel import LLMKernel, KernelResult


class Hypo(BaseModel):
    location: str
    change: str


@dataclass
class FakeClient:
    """Returns pre-scripted responses; records the models/params it saw."""
    model: str
    script: list  # list of LLMResult to return in order
    calls: list = field(default_factory=list)

    def chat(self, **kw):
        self.calls.append(kw)
        i = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[i]


def _res(text="", finish="stop", tool_calls=None):
    return LLMResult(text=text, tool_calls=tool_calls or [], finish_reason=finish,
                     tokens_in=10, tokens_out=10, usd=0.0, model="fake")


def _kernel(script_by_model, **kw):
    """Build a kernel whose client_factory yields FakeClients per model."""
    clients = {}
    def factory(model):
        c = FakeClient(model=model, script=script_by_model.get(model, [_res("{}")]))
        clients[model] = c
        return c
    k = LLMKernel(client_factory=factory, **kw)
    return k, clients


ARRAY = ["flash", "pro", "kimi"]


# ── intelligence dial + model resolution ──────────────────────────────────

def test_intelligence_clamp():
    k, _ = _kernel({}, models=ARRAY, schema=Hypo)
    k.set_intelligence(10);   assert k.intelligence == 1.0
    k.set_intelligence(-5);   assert k.intelligence == 0.0
    k.set_intelligence(0.4);  assert k.intelligence == 0.4
    k.increase_intelligence(2.0); assert k.intelligence == 1.0


def test_model_resolution_bands():
    k, _ = _kernel({}, models=ARRAY, schema=Hypo)
    # round(intel*2): 0,0.2→0(flash); 0.4,0.6→1(pro); 0.8,1.0→2(kimi)
    expect = {0.0: "flash", 0.2: "flash", 0.4: "pro",
              0.6: "pro", 0.8: "kimi", 1.0: "kimi"}
    for intel, model in expect.items():
        k.set_intelligence(intel)
        assert k.current_model() == model, f"intel {intel} → {k.current_model()} != {model}"


def test_performer_band_sequence():
    """H1..H6 with +0.2 increments → flash,flash,pro,pro,kimi,kimi."""
    k, _ = _kernel({}, models=ARRAY, mode="text")
    seen = []
    for _h in range(6):
        seen.append(k.current_model())
        k.increase_intelligence(0.2)
    assert seen == ["flash", "flash", "pro", "pro", "kimi", "kimi"]


# ── schema mode happy path ─────────────────────────────────────────────────

def test_schema_valid_first_try():
    script = {"flash": [_res('{"location":"a.py","change":"fix"}')]}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo, intelligence=0.0)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok and isinstance(r.value, Hypo) and r.value.location == "a.py"
    assert r.model == "flash"


def test_schema_prose_wrapped_json():
    script = {"flash": [_res('Here you go:\n{"location":"x","change":"y"} done')]}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok and r.value.location == "x"


# ── self-heal: truncation → ×4 tokens ──────────────────────────────────────

def test_truncation_bumps_tokens_then_succeeds():
    # first call truncates (finish=length), second (after bump) returns valid JSON
    script = {"flash": [_res("partial...", finish="length"),
                        _res('{"location":"a","change":"b"}')]}
    k, clients = _kernel(script, models=ARRAY, schema=Hypo, base_max_tokens=1000)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok and r.value.change == "b"
    # second call's max_tokens must be 4× the first
    calls = clients["flash"].calls
    assert calls[0]["max_tokens"] == 1000
    assert calls[1]["max_tokens"] == 4000


# ── self-heal: invalid JSON → reask ────────────────────────────────────────

def test_reask_on_invalid_then_valid():
    script = {"flash": [_res("not json at all"),
                        _res('{"location":"a","change":"b"}')]}
    k, clients = _kernel(script, models=ARRAY, schema=Hypo, reask_max=2)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok and r.value.location == "a"
    # the reask must have appended an assistant + corrective user message
    second_msgs = clients["flash"].calls[1]["messages"]
    assert any("failed" in m["content"] for m in second_msgs if m["role"] == "user")


def test_validation_error_triggers_reask():
    # valid JSON but missing required 'change' → ValidationError → reask → fixed
    script = {"flash": [_res('{"location":"a"}'),
                        _res('{"location":"a","change":"b"}')]}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo, reask_max=2)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok and r.value.change == "b"


# ── self-heal: exhausted reask → escalate intelligence (next model) ────────

def test_escalates_model_after_reask_exhausted():
    # flash always returns junk; pro returns valid → kernel must climb to pro
    script = {
        "flash": [_res("junk"), _res("junk"), _res("junk"), _res("junk")],
        "pro":   [_res('{"location":"a","change":"b"}')],
    }
    k, clients = _kernel(script, models=ARRAY, schema=Hypo,
                         intelligence=0.0, reask_max=1)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok and r.model == "pro"
    assert k.intelligence >= 0.5  # climbed at least one model step
    assert "pro" in clients


def test_total_failure_returns_not_ok():
    # every model returns junk → never validates → ok=False, no exception
    junk = [_res("junk")] * 6
    script = {m: list(junk) for m in ARRAY}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo, reask_max=1)
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok is False and r.value is None and r.failure


# ── text mode ──────────────────────────────────────────────────────────────

def test_text_mode_returns_tool_calls():
    tc = [{"id": "1", "name": "read", "arguments": {"path": "x"}}]
    script = {"flash": [_res("thinking...", tool_calls=tc)]}
    k, _ = _kernel(script, models=ARRAY, mode="text")
    r = k.run([{"role": "user", "content": "go"}], tools=[{"x": 1}])
    assert r.ok and r.tool_calls == tc and r.text == "thinking..."


# ── api error → escalate, then fail cleanly ────────────────────────────────

def test_api_error_escalates_then_fails():
    class BoomClient:
        def __init__(self, model): self.model = model
        def chat(self, **kw): raise RuntimeError("429 rate limited")
    k = LLMKernel(models=ARRAY, schema=Hypo, intelligence=0.0,
                  client_factory=lambda m: BoomClient(m))
    r = k.run([{"role": "user", "content": "go"}])
    assert r.ok is False and "api_error" in r.failure
    assert k.intelligence == 1.0  # climbed all the way trying


# ── adapter layer: registry resolution + Kimi behavior ─────────────────────
from cat.llm.adapters import (
    adapter_for, LLMModelAdapter, LLMModelAdapterKimiBase, LLMModelAdapterKimi26,
)
from cat.llm.card import card_for


def test_adapter_registry_resolution():
    assert isinstance(adapter_for("openrouter/deepseek/deepseek-v4-flash"), LLMModelAdapter)
    assert type(adapter_for("openrouter/deepseek/deepseek-v4-flash")) is LLMModelAdapter
    assert isinstance(adapter_for("openrouter/moonshotai/kimi-k2.6"), LLMModelAdapterKimi26)
    # unknown kimi version → family base (untuned but correct), not universal
    a = adapter_for("openrouter/moonshotai/kimi-k2.9")
    assert isinstance(a, LLMModelAdapterKimiBase) and not isinstance(a, LLMModelAdapterKimi26)
    # totally unknown → universal fallback
    assert type(adapter_for("acme/whatever-1")) is LLMModelAdapter


def test_model_card_lookup():
    c = card_for("openrouter/moonshotai/kimi-k2.6")
    assert c.producer == "Moonshot AI" and c.family == "Kimi K2"
    c2 = card_for("acme/unknown-model")
    assert c2.family == "unknown"  # derived fallback


class _RouterStub:
    """Captures dispatch kwargs; returns a scripted LLMResult."""
    def __init__(self, script): self.script = script; self.calls = []
    def dispatch(self, model_id, **kw):
        self.calls.append((model_id, kw))
        i = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[i]


def test_kimi_adapter_prefills_and_disables_reasoning():
    # Kimi returns body WITHOUT leading "{" (prefill not echoed) → adapter prepends it
    r = _RouterStub([_res('"location":"a","change":"b"}')])
    a = LLMModelAdapterKimi26()
    out = a.generate_schema(r, "openrouter/moonshotai/kimi-k2.6",
                            [{"role": "user", "content": "go"}],
                            schema=Hypo, max_tokens=1500, temperature=0.1)
    assert out.parsed == {"location": "a", "change": "b"}
    _, kw = r.calls[0]
    assert kw["reasoning"] == {"enabled": False}      # reasoning disabled
    assert kw["max_tokens"] >= 16000                  # k2.6 floor applied
    assert kw["temperature"] == 1.0                   # k2.6 fixed temp
    assert kw["messages"][-1] == {"role": "assistant", "content": "{"}  # prefill


def test_kimi_adapter_parses_tool_markers():
    marker = ('thinking <|tool_calls_section_begin|><|tool_call_begin|>'
              'functions.read:0<|tool_call_argument_begin|>{"path":"x.py"}'
              '<|tool_call_end|><|tool_calls_section_end|>')
    r = _RouterStub([_res(marker, tool_calls=[])])
    a = LLMModelAdapterKimiBase()
    out = a.generate_text(r, "openrouter/moonshotai/kimi-k2", [{"role": "user", "content": "go"}],
                          tools=[{"x": 1}], max_tokens=1500, temperature=0.2)
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0]["name"] == "read"
    assert '"path"' in out.tool_calls[0]["arguments"]


def test_kernel_records_usage_with_producer():
    script = {"flash": [_res('{"location":"a","change":"b"}')]}
    k, _ = _kernel(script, models=["flash"], schema=Hypo)
    k.run([{"role": "user", "content": "go"}])
    assert len(k.usage_records) == 1
    rec = k.usage_records[0]
    assert rec["ok"] and "producer" in rec and "tokens_in" in rec
