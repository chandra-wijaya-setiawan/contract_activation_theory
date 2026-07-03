"""Unit tests for LLMKernel — intelligence dial, model resolution, self-heal.

No network: a FakeClient returns scripted LLMResult objects so we can exercise
the truncation→bump, validation→reask, and exhausted→escalate branches.
"""
from dataclasses import dataclass, field
from typing import Any

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
    model: str = ""
    script: list = field(default_factory=list)  # list of LLMResult to return in order
    calls: list = field(default_factory=list)
    recorder: Any = None

    def chat(self, **kw):
        self.calls.append(kw)
        i = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[i]


def _res(text="", finish="stop", tool_calls=None):
    return LLMResult(text=text, tool_calls=tool_calls or [], finish_reason=finish,
                     tokens_in=10, tokens_out=10, usd=0.0, model="fake")


def _kernel(script_by_model, **kw):
    """Build a kernel whose llm_factory yields FakeClients per model."""
    clients = {}
    def factory(model):
        c = FakeClient(model=model, script=script_by_model.get(model, [_res("{}")]))
        clients[model] = c
        return c
    k = LLMKernel(llm_factory=factory, **kw)
    return k, clients


ARRAY = [
    "openrouter/deepseek/deepseek-v4-flash",
    "openrouter/deepseek/deepseek-v4-pro",
    "openrouter/owl-alpha",
]


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
    expect = {0.0: "openrouter/deepseek/deepseek-v4-flash", 0.2: "openrouter/deepseek/deepseek-v4-flash", 0.4: "openrouter/deepseek/deepseek-v4-pro",
              0.6: "openrouter/deepseek/deepseek-v4-pro", 0.8: "openrouter/owl-alpha", 1.0: "openrouter/owl-alpha"}
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
    assert seen == ["openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro", "openrouter/owl-alpha", "openrouter/owl-alpha"]


# ── schema mode happy path ─────────────────────────────────────────────────

def test_schema_valid_first_try():
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res('{"location":"a.py","change":"fix"}')]}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo, intelligence=0.0)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok and isinstance(r.value, Hypo) and r.value.location == "a.py"
    assert r.model == "openrouter/deepseek/deepseek-v4-flash"


def test_schema_prose_wrapped_json():
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res('Here you go:\n{"location":"x","change":"y"} done')]}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok and r.value.location == "x"


# ── self-heal: truncation → ×4 tokens ──────────────────────────────────────

def test_truncation_bumps_tokens_then_succeeds():
    # Both json_schema and json_object return length on first kernel call;
    # after kernel bumps max_tokens, json_schema succeeds on second kernel call.
    script = {"openrouter/deepseek/deepseek-v4-flash": [
        _res("partial...", finish="length"),   # json_schema attempt
        _res("partial...", finish="length"),   # json_object fallback
        _res('{"location":"a","change":"b"}'),  # json_schema after bump
    ]}
    k, clients = _kernel(script, models=ARRAY, schema=Hypo, base_max_tokens=1000)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok and r.value.change == "b"
    calls = clients["openrouter/deepseek/deepseek-v4-flash"].calls
    assert calls[0]["max_tokens"] == 1000
    assert calls[2]["max_tokens"] == 4000


# ── self-heal: invalid JSON → reask ────────────────────────────────────────

def test_reask_on_invalid_then_valid():
    # First call returns invalid JSON (json_schema fails, json_object also fails);
    # reask appends error message; second kernel call succeeds.
    script = {"openrouter/deepseek/deepseek-v4-flash": [
        _res("not json at all"),           # json_schema attempt
        _res("still not json"),             # json_object fallback -> triggers reask
        _res('{"location":"a","change":"b"}'),  # reask succeeds
    ]}
    k, clients = _kernel(script, models=ARRAY, schema=Hypo, reask_max=2)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok and r.value.location == "a"
    # the reask must have appended an assistant + corrective user message
    second_msgs = clients["openrouter/deepseek/deepseek-v4-flash"].calls[2]["messages"]
    assert any("failed" in m["content"] for m in second_msgs if m["role"] == "user")


def test_validation_error_triggers_reask():
    # valid JSON but missing required 'change' → ValidationError → reask → fixed
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res('{"location":"a"}'),
                        _res('{"location":"a","change":"b"}')]}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo, reask_max=2)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok and r.value.change == "b"


# ── self-heal: exhausted reask → escalate intelligence (next model) ────────

def test_escalates_model_after_reask_exhausted():
    # flash always returns junk; pro returns valid → kernel must climb to pro
    script = {
        "openrouter/deepseek/deepseek-v4-flash": [_res("junk"), _res("junk"), _res("junk"), _res("junk")],
        "openrouter/deepseek/deepseek-v4-pro":   [_res('{"location":"a","change":"b"}')],
    }
    k, clients = _kernel(script, models=ARRAY, schema=Hypo,
                         intelligence=0.0, reask_max=1)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok and r.model == "openrouter/deepseek/deepseek-v4-pro"
    assert k.intelligence >= 0.5  # climbed at least one model step
    assert "openrouter/deepseek/deepseek-v4-pro" in clients


def test_total_failure_returns_not_ok():
    # every model returns junk → never validates → ok=False, no exception
    junk = [_res("junk")] * 6
    script = {m: list(junk) for m in ARRAY}
    k, _ = _kernel(script, models=ARRAY, schema=Hypo, reask_max=1)
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok is False and r.value is None and r.failure


# ── text mode ──────────────────────────────────────────────────────────────

def test_text_mode_returns_tool_calls():
    tc = [{"id": "1", "name": "read", "arguments": {"path": "x"}}]
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res("thinking...", tool_calls=tc)]}
    k, _ = _kernel(script, models=ARRAY, mode="text")
    r = k.generate_text([{"role": "user", "content": "go"}], tools=[{"x": 1}])
    assert r.ok and r.tool_calls == tc and r.text == "thinking..."


# ── api error → escalate, then fail cleanly ────────────────────────────────

def test_api_error_escalates_then_fails():
    class BoomClient:
        model: str = ""
        recorder: Any = None
        def __init__(self, model): self.model = model
        def chat(self, **kw): raise RuntimeError("429 rate limited")
    k = LLMKernel(models=ARRAY, schema=Hypo, intelligence=0.0,
                  llm_factory=lambda m: BoomClient(m))
    r = k.generate_schema([{"role": "user", "content": "go"}])
    assert r.ok is False and "api_error" in r.failure
    assert k.intelligence == 1.0  # climbed all the way trying


# ── provider layer: registry resolution ──────────────────────
from cat.llm.providers import get_provider
from cat.llm.providers.base import ProviderABC
from cat.llm.card import card_for


def test_provider_registry_resolution():
    """get_provider routes model_id prefix to the right provider class."""
    from cat.llm.providers.openrouter import OpenRouterProvider
    from cat.llm.providers.ollama import OllamaProvider

    p = get_provider("ollama/qwen2.5-coder:7b")
    assert isinstance(p, OllamaProvider)

    p = get_provider("openrouter/deepseek/deepseek-v4-flash")
    assert isinstance(p, OpenRouterProvider)

    # Unknown prefix -> explicit ValueError (no fallback)
    with pytest.raises(ValueError, match="No provider registered"):
        get_provider("acme/unknown-model")


def test_provider_is_abc():
    """All providers implement ProviderABC."""
    from cat.llm.providers.openrouter import OpenRouterProvider
    from cat.llm.providers.ollama import OllamaProvider

    assert issubclass(OpenRouterProvider, ProviderABC)
    assert issubclass(OllamaProvider, ProviderABC)


def test_model_card_lookup():
    c = card_for("openrouter/moonshotai/kimi-k2.6")
    assert c.producer == "Moonshot AI" and c.family == "Kimi K2"
    c2 = card_for("acme/unknown-model")
    assert c2.family == "unknown"  # derived fallback


def test_kernel_records_usage_with_producer():
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res('{"location":"a","change":"b"}')]}
    k, _ = _kernel(script, models=["openrouter/deepseek/deepseek-v4-flash"], schema=Hypo)
    k.generate_schema([{"role": "user", "content": "go"}])
    assert len(k.usage_records) == 1
    rec = k.usage_records[0]
    assert rec["ok"] and "producer" in rec and "tokens_in" in rec


def test_kernel_generate_text_mode():
    tc = [{"id": "1", "name": "read", "arguments": {"path": "x"}}]
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res("thinking...", tool_calls=tc)]}
    k, _ = _kernel(script, models=["openrouter/deepseek/deepseek-v4-flash"], mode="text")
    r = k.generate_text([{"role": "user", "content": "go"}], tools=[{"x": 1}])
    assert r.ok and r.tool_calls == tc and r.text == "thinking..."


def test_kernel_per_call_overrides():
    """Per-call intelligence override works."""
    script = {"openrouter/deepseek/deepseek-v4-flash": [_res('{"location":"a","change":"b"}')]}
    k, _ = _kernel(script, models=["openrouter/deepseek/deepseek-v4-flash"], schema=Hypo, intelligence=0.0)
    r = k.generate_schema([{"role": "user", "content": "go"}], intelligence=0.5)
    assert r.ok and r.intelligence == 0.5
    assert k.intelligence == 0.5
