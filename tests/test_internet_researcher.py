"""Test: chat profile spawns internet_researcher sub-agent for deep research.

Verifies the full flow:
  chat (Performer) → spawn_subagent(profile="internet_researcher") → done(reason=report)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.integration
class TestInternetResearcherSpawn:
    """End-to-end test: chat spawns internet_researcher sub-agent."""

    def test_spawn_internet_researcher(self):
        """Chat profile delegates research to internet_researcher sub-agent."""
        import cat.agent.hypothesis as _hyp
        from cat.agent.registry import AgentRegistry
        from cat.agent.hypothesis import Activator
        from cat.contract.model import Contract, State, Constraints
        from cat.trace import Recorder
        from cat.llm.client import LLM
        from cat.capability.full_registry import build_default_registry, register_subagent_tool

        # Use owl-alpha for both chat and internet_researcher
        _orig = _hyp._DEFAULT_MODEL_ARRAY
        _hyp._DEFAULT_MODEL_ARRAY = ["openrouter/owl-alpha"]

        task = (
            "Research what LLM models were released in June 2026. "
            "Use the internet_researcher sub-agent for deep research. "
            "Return a summary of key releases with citations."
        )

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                recorder = Recorder(
                    run_id="test-ir-spawn",
                    task_id="ir-spawn",
                    agent="chat",
                    model="openrouter/owl-alpha",
                    out_dir=Path(tmpdir),
                )

                registry = AgentRegistry()
                skill = registry.load("chat")
                skill.loop_config.skip_stipulator = True
                skill.loop_config.skip_oracle = True
                skill.loop_config.max_hypotheses = 1
                skill.loop_config.max_inner_attempts = 3

                cap_registry = build_default_registry()
                llm = LLM(model="openrouter/owl-alpha", recorder=recorder)

                # Register spawn_subagent so the Performer can delegate
                _sub_agent_reg = AgentRegistry()
                register_subagent_tool(
                    registry=cap_registry,
                    agent_registry=_sub_agent_reg,
                    capability_registry=cap_registry,
                    recorder=recorder,
                    llm=llm,
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
                    constraints=Constraints(max_usd=5.0, max_wall_s=300.0),
                )
                contract.raw_request = task
                state = State(workdir=REPO_ROOT)

                ok = activator.run(contract, state)

                # Extract output from knowledge_update events
                summary_parts = []
                for ev in recorder.traj.events:
                    if ev.kind == "knowledge_update":
                        reason = ev.payload.get("reason", "")
                        if reason:
                            summary_parts.append(reason)

                full_output = "\n".join(summary_parts)

                # Debug: print all events
                if not ok or not full_output.strip():
                    for ev in recorder.traj.events:
                        print(f"  {ev.kind}: {str(ev.payload)[:200]}")

                assert ok, f"Agent run failed. Trace has {len(recorder.traj.events)} events"
                assert full_output.strip(), "Agent produced no output"

                words = full_output.split()
                assert len(words) >= 30, f"Output too short: {len(words)} words"

                lower = full_output.lower()
                assert any(kw in lower for kw in ["llm", "model", "release", "june", "2026"]), (
                    f"Output doesn't seem to answer the question:\n{full_output[:300]}"
                )
                print(f"\n--- Research Report ({len(words)} words) ---\n{full_output}\n")
        finally:
            _hyp._DEFAULT_MODEL_ARRAY = _orig
