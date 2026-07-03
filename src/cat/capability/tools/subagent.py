"""spawn_subagent — tool capability for delegating tasks to nested sub-agents.

A sub-agent is a nested contract discharge: the Performer decides to delegate
a subtask, calls spawn_subagent(task=..., profile=..., context={...}), and
receives a SubAgentResult with the outcome.

The sub-agent runs the full hypothesis loop (Representer → Stipulator →
Performer → Oracle) with its own profile, isolated message thread, and
nested trace. Cost is tracked separately and reported back to the parent.

Usage from the Performer's perspective:
    spawn_subagent(
        task="Find all usages of deprecated_api in the codebase",
        profile="task",
        context={"workdir": "/testbed", "focus": "src/"}
    )
    → {verdict: "completed", output: "Found 12 usages in 4 files",
        artifacts: {"files": "a.py, b.py, c.py, d.py"}, tokens_spent: 4500, ...}
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import (
    Contract,
    Constraints,
    Cost,
    Criterion,
    Representation,
    State,
)
from cat.contract.schemas import SubAgentResult


def create_subagent_capability(
    agent_registry: Any,       # cat.agent.registry.AgentRegistry
    capability_registry: Any,  # cat.capability.registry.CapabilityRegistry
    recorder: Any,             # cat.trace.Recorder
    llm: Any,                  # cat.llm.client.LLM
    checkpointer: Any = None,  # optional checkpointer for sub-agents
) -> Capability:
    """Build the spawn_subagent tool capability.

    Args:
        agent_registry: used to load the sub-agent's profile (skill.yaml)
        capability_registry: the tool registry the sub-agent will use
        recorder: shared recorder — sub-agent trace events nest under parent
        llm: base LLM client (sub-agent may escalate to other models)
        checkpointer: optional checkpointer passed to sub-agents
    """

    def _handle(args: dict, state: State) -> ToolResult:
        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult(
                tool="spawn_subagent",
                is_error=True,
                output="missing 'task' argument",
            )

        profile_name = str(args.get("profile", "task")).strip()
        context = args.get("context", {})
        if not isinstance(context, dict):
            context = {}

        # Load the sub-agent's profile
        try:
            agent_def = agent_registry.load(profile_name)
        except FileNotFoundError:
            return ToolResult(
                tool="spawn_subagent",
                is_error=True,
                output=f"profile '{profile_name}' not found",
            )
        except Exception as exc:
            return ToolResult(
                tool="spawn_subagent",
                is_error=True,
                output=f"failed to load profile '{profile_name}': {exc}",
            )

        # Build the child contract
        child_id = f"sub-{uuid.uuid4().hex[:8]}"
        child_constraints = Constraints(
            max_tokens_total=int(context.get("_max_tokens", 50_000)),
            max_tool_calls=int(context.get("_max_tool_calls", 50)),
            max_llm_calls=int(context.get("_max_llm_calls", 25)),
            max_usd=float(context.get("_max_usd", 5.0)),
            max_wall_s=float(context.get("_max_wall_s", 300.0)),
        )

        # Context key-value pairs become representations for the sub-agent
        context_reps = [
            Representation(key=k, value=str(v))
            for k, v in context.items()
            if not k.startswith("_")  # _-prefixed keys are meta-params, not context
        ]

        child_contract = Contract(
            id=child_id,
            obligation=task,
            criteria=[],  # sub-agent uses skip_oracle; Performer's done() is the verdict
            representations=context_reps,
            constraints=child_constraints,
            depth=1,  # sub-agents are depth 1 (parent is depth 0)
            parent_contract_id=None,  # set below after we know parent id
            capabilities=list(capability_registry.names()) if capability_registry else [],
        )

        # Sub-agent state: use workdir from context or inherit parent's
        sub_workdir = Path(str(context.get("_workdir", state.workdir)))
        sub_state = State(
            workdir=sub_workdir,
            kv=dict(state.kv),  # inherit parent's key-value store
        )

        # Run the sub-agent
        try:
            from cat.agent.hypothesis import HypothesisAgent
            from cat.agent.definition import LoopConfig

            sub_agent = HypothesisAgent(
                llm=llm,
                registry=capability_registry,
                recorder=recorder,
                checkpointer=checkpointer,
                skill=agent_def,
            )

            # Sub-agents always skip oracle — the parent Performer is the oracle
            # Override the profile's loop config to skip oracle
            original_skip_oracle = agent_def.loop_config.skip_oracle
            agent_def.loop_config.skip_oracle = True
            agent_def.loop_config.max_hypotheses = int(
                context.get("_max_hypotheses", agent_def.loop_config.max_hypotheses)
            )
            agent_def.loop_config.max_inner_attempts = int(
                context.get("_max_inner_attempts", agent_def.loop_config.max_inner_attempts)
            )

            run_result = sub_agent.run(child_contract, sub_state)

            # Restore original config (don't mutate the profile object)
            agent_def.loop_config.skip_oracle = original_skip_oracle

            # Build the result
            sub_result = SubAgentResult(
                verdict=_verdict_from_state(child_contract.state),
                output=_extract_output(run_result),
                artifacts=_extract_artifacts(run_result, child_contract),
                tokens_spent=child_contract.cost.tokens_total,
                tool_calls=child_contract.cost.tool_calls,
                trace_id=child_id,
            )

            result_json = sub_result.model_dump_json()
            return ToolResult(
                tool="spawn_subagent",
                is_error=False,
                output=result_json,
                detail={
                    "contract_id": child_id,
                    "profile": profile_name,
                    "verdict": sub_result.verdict,
                    "tokens_spent": sub_result.tokens_spent,
                    "tool_calls": sub_result.tool_calls,
                },
            )

        except Exception as exc:
            return ToolResult(
                tool="spawn_subagent",
                is_error=True,
                output=f"sub-agent failed: {type(exc).__name__}: {exc}",
                detail={"contract_id": child_id, "profile": profile_name},
            )

    return Capability(
        name="spawn_subagent",
        description=(
            "Delegate a task to a sub-agent. The sub-agent runs the full "
            "hypothesis loop with the specified profile and returns its result.\n\n"
            "Args:\n"
            "  task (str): What the sub-agent should do.\n"
            "  profile (str): Which profile to use (default: 'task').\n"
            "  context (dict): Key-value facts to pass to the sub-agent.\n"
            "    _workdir (str): working directory (default: parent's workdir)\n"
            "    _max_tokens (int): token budget (default: 50000)\n"
            "    _max_tool_calls (int): tool call budget (default: 50)\n"
            "    _max_hypotheses (int): max hypotheses (default: from profile)\n"
            "    _max_inner_attempts (int): max inner attempts (default: from profile)\n\n"
            "Returns JSON: {verdict, output, artifacts, tokens_spent, tool_calls, trace_id}"
        ),
        json_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What the sub-agent should accomplish",
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name for the sub-agent (default: 'task')",
                },
                "context": {
                    "type": "object",
                    "description": (
                        "Key-value context for the sub-agent. "
                        "Use _-prefixed keys for meta-params "
                        "(_workdir, _max_tokens, _max_tool_calls, etc.)."
                    ),
                },
            },
            "required": ["task"],
        },
        handler=_handle,
    )


def _verdict_from_state(state: Any) -> str:
    """Map ContractState to SubAgentResult verdict."""
    from cat.contract.model import ContractState

    if state == ContractState.FULFILLED:
        return "completed"
    elif state == ContractState.ACTIVE:
        return "partial"
    else:
        return "failed"


def _extract_output(run_result: Any) -> str:
    """Extract a human-readable summary from the sub-agent's run result."""
    try:
        # Try to get the final performer reason from the last hypothesis
        contract = run_result.contract
        if contract.state_reason:
            return contract.state_reason[:2000]
        return f"Sub-agent finished in state: {contract.state.value}"
    except Exception:
        return "Sub-agent completed (no summary available)"


def _extract_artifacts(run_result: Any, contract: Contract) -> dict[str, str]:
    """Extract key artifacts from the sub-agent's run for the parent."""
    artifacts: dict[str, str] = {}
    try:
        # Include any modified files from representations
        for rep in contract.representations:
            if rep.key.startswith("modified:"):
                artifacts[rep.key] = rep.value

        # Include cost summary
        artifacts["cost_summary"] = (
            f"tokens={contract.cost.tokens_total}, "
            f"tool_calls={contract.cost.tool_calls}, "
            f"llm_calls={contract.cost.llm_calls}, "
            f"usd={contract.cost.usd:.4f}"
        )
    except Exception:
        pass
    return artifacts
