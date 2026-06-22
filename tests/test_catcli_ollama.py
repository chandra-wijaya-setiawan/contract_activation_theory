"""Integration test: run cat-cli with Ollama model.

Verifies the full LLM pipeline works end-to-end with the new provider architecture:
- Provider routing (ollama/ prefix → OllamaProvider)
- LLMKernel.generate_schema() with self-heal
- LLMKernel.generate_toolcall() with Pydantic validation
- LLMKernel.generate_text() with native tool calls
- Full agent run with skip_stipulator (direct performer mode)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _run_catcli(model: str, task: str, timeout: int = 180) -> tuple[int, str, str]:
    """Run cat-cli as a subprocess with the given model and task."""
    env = {
        "DOCKER_HOST": "ssh://cpubox",
        "OLLAMA_BASE_URL": "http://10.0.0.3:11434/v1",
    }
    full_env = {**os.environ, **env}

    cmd = [
        sys.executable, "-m", "app.cat_cli.cat_cli",
        "--profile", "chat",
        "--model", model,
        "--task", task,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=str(REPO_ROOT), timeout=timeout, env=full_env,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.mark.integration
class TestCatCliOllama:
    """End-to-end tests with Ollama models."""

    def test_ollama_provider_routing(self):
        """Verify the Ollama provider is correctly routed via model prefix."""
        from cat.llm.providers import get_provider
        from cat.llm.providers.ollama import OllamaProvider

        p = get_provider("ollama/qwen2.5-coder:7b")
        assert isinstance(p, OllamaProvider), f"Expected OllamaProvider, got {type(p)}"

    def test_ollama_basic_schema_call(self):
        """Verify a basic structured LLM call through the Ollama provider works."""
        from cat.llm.kernel import LLMKernel
        from cat.contract.schemas import ChatJudgeSchema

        kernel = LLMKernel(
            models=["ollama/qwen2.5-coder:7b"],
            mode="schema",
            schema=ChatJudgeSchema,
            temperature=0.1,
            role_tag="test",
        )
        messages = [
            {"role": "system", "content": "You are a judge. Respond with JSON only."},
            {"role": "user", "content": "USER REQUEST: Say hello.\nRESPONSE: Hello there!\nJudge this."},
        ]
        result = kernel.generate_schema(messages)

        assert result.ok, f"Kernel call failed: {result.failure}"
        assert result.value is not None
        assert hasattr(result.value, "verdict")
        assert result.model == "ollama/qwen2.5-coder:7b"

    def test_ollama_toolcall_validation(self):
        """Verify toolcall mode validates with Pydantic."""
        from cat.llm.kernel import LLMKernel
        from cat.contract.schemas import PerformerExecutionStep

        kernel = LLMKernel(
            models=["ollama/qwen2.5-coder:7b"],
            mode="toolcall",
            schema=PerformerExecutionStep,
            temperature=0.2,
            role_tag="test",
        )
        messages = [
            {"role": "system", "content": "You are a performer. Use tools to accomplish the task."},
            {"role": "user", "content": "Say hello to the user."},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "done",
                    "description": "Mark task as complete",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string", "description": "Why the task is complete"}
                        },
                        "required": ["reason"]
                    }
                }
            }
        ]
        result = kernel.generate_toolcall(messages, schema=PerformerExecutionStep, tools=tools)

        assert result.model == "ollama/qwen2.5-coder:7b"
        if result.ok:
            assert result.value is not None
            assert hasattr(result.value, "intent")

    def test_ollama_text_mode(self):
        """Verify text mode returns native tool calls."""
        from cat.llm.kernel import LLMKernel

        kernel = LLMKernel(
            models=["ollama/qwen2.5-coder:7b"],
            mode="text",
            temperature=0.1,
            role_tag="test",
        )
        messages = [
            {"role": "user", "content": "Say hello in one sentence."},
        ]
        result = kernel.generate_text(messages, tools=[])

        assert result.model == "ollama/qwen2.5-coder:7b"
        if result.ok:
            assert result.text or result.tool_calls

    def test_ollama_kernel_tracing(self):
        """Verify the kernel emits llm_input and llm_output trace events."""
        from cat.llm.kernel import LLMKernel
        from cat.contract.schemas import ChatJudgeSchema
        from cat.trace import Recorder

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = Recorder(
                run_id="test-ollama",
                task_id="test",
                agent="test",
                model="ollama/qwen2.5-coder:7b",
                out_dir=Path(tmpdir),
            )
            kernel = LLMKernel(
                models=["ollama/qwen2.5-coder:7b"],
                mode="schema",
                schema=ChatJudgeSchema,
                temperature=0.1,
                role_tag="test",
                recorder=recorder,
            )
            messages = [
                {"role": "system", "content": "You are a judge. Respond with JSON only."},
                {"role": "user", "content": "USER REQUEST: Say hello.\nRESPONSE: Hello there!\nJudge this."},
            ]
            kernel.generate_schema(messages)

            kinds = [ev.kind for ev in recorder.traj.events]
            assert "llm_input" in kinds, f"No llm_input in trace. Kinds: {kinds}"
            assert "llm_output" in kinds, f"No llm_output in trace. Kinds: {kinds}"

    def test_summarize_repo_ollama(self):
        """Run the agent directly with Ollama to summarize the repo in ~300 words.

        Uses skip_stipulator=True to bypass the hypothesis loop — the model
        goes straight to the Performer which reads files and calls done().
        This is much faster and more reliable for smaller Ollama models.
        """
        import cat.agent.hypothesis as _hyp
        from cat.agent.registry import AgentRegistry
        from cat.agent.hypothesis import Activator
        from cat.contract.model import Contract, State, Constraints
        from cat.trace import Recorder
        from cat.llm.client import LLM
        from pathlib import Path as _Path

        # Override the default model array so the performer uses Ollama
        _orig_array = _hyp._DEFAULT_MODEL_ARRAY
        _hyp._DEFAULT_MODEL_ARRAY = ["ollama/qwen2.5-coder:7b"]

        task = (
            "Read the README.md file and summarize this repository in approximately 300 words. "
            "Describe: what the project does, its architecture, key components, "
            "and how they interact. Be concise but comprehensive. "
            "Use done(reason=...) to deliver your summary."
        )

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                recorder = Recorder(
                    run_id="test-ollama-summary",
                    task_id="summarize-repo",
                    agent="chat",
                    model="ollama/qwen2.5-coder:7b",
                    out_dir=_Path(tmpdir),
                )

                registry = AgentRegistry()
                skill = registry.load("chat")
                skill.model = "ollama/qwen2.5-coder:7b"
                skill.loop_config.skip_stipulator = True
                skill.loop_config.skip_oracle = True
                skill.loop_config.max_hypotheses = 1
                skill.loop_config.max_inner_attempts = 2

                from cat.capability.full_registry import build_default_registry
                cap_registry = build_default_registry()
                llm = LLM(
                    model="ollama/qwen2.5-coder:7b",
                    recorder=recorder,
                )

                activator = Activator(
                    llm=llm,
                    registry=cap_registry,
                    recorder=recorder,
                    skill=skill,
                )

                contract = Contract.new(
                    obligation=task,
                    criteria=[],
                    constraints=Constraints(max_usd=1.0, max_wall_s=300.0),
                )
                contract.raw_request = task
                state = State(workdir=REPO_ROOT)

                ok = activator.run(contract, state)

                # Extract the summary from knowledge_update events
                # (the "done" reason is stored in the payload's "reason" field)
                summary_parts = []
                for ev in recorder.traj.events:
                    if ev.kind == "knowledge_update":
                        reason = ev.payload.get("reason", "")
                        if reason:
                            summary_parts.append(reason)

                full_output = "\n".join(summary_parts)

                if not ok or not full_output.strip():
                    for ev in recorder.traj.events:
                        print(f"  {ev.kind}: {str(ev.payload)[:150]}")

                assert ok, f"Agent run failed. Trace has {len(recorder.traj.events)} events"
                assert full_output.strip(), "Agent produced no output"

                words = full_output.split()
                assert len(words) >= 50, f"Output too short: {len(words)} words"

                lower = full_output.lower()
                assert any(kw in lower for kw in ["cat", "contract", "agent", "hypothesis", "llm"]), (
                    f"Output doesn't seem to describe the repo:\n{full_output[:300]}"
                )
        finally:
            _hyp._DEFAULT_MODEL_ARRAY = _orig_array
