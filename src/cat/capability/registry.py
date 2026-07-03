"""Capability registry + tool dispatch.

CapabilityRegistry owns the tool dispatch loop: it holds named Capability
objects, exposes OpenAI-compatible schemas (with injected `reason` param),
and handles invoke() with tracing, reason-stripping, and error containment.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cat.contract.model import State
from cat.trace import Recorder, TraceEvent
from cat.utils.time import utcnow_iso


OUTPUT_CAP_BYTES = 8_000
READ_CAP_BYTES = 64_000


@dataclass
class ToolResult:
    tool: str
    is_error: bool
    output: str
    detail: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0


@dataclass
class Capability:
    name: str
    description: str
    json_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], State], ToolResult]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if cap.name in self._caps:
            raise ValueError(f"capability {cap.name!r} already registered")
        self._caps[cap.name] = cap

    def get(self, name: str) -> Optional[Capability]:
        return self._caps.get(name)

    def names(self) -> list[str]:
        return list(self._caps)

    def tool_schemas(self, only: Optional[set[str]] = None) -> list[dict[str, Any]]:
        """OpenAI-compatible tool schemas. If `only` is given, restrict to that grant.

        R27: every tool gets an optional top-level `reason` parameter injected
        here. The Performer fills it (one sentence) when its tool call deviates
        from the planner's suggested_tool, so the planner/attestor can see why.
        The dispatch path strips `reason` before invoking the handler and
        stashes it on the InvocationSchema record.
        """
        out: list[dict[str, Any]] = []
        for c in self._caps.values():
            if only is not None and c.name not in only:
                continue
            params = dict(c.json_schema or {})
            props = dict(params.get("properties") or {})
            if "reason" not in props:
                props["reason"] = {
                    "type": "string",
                    "description": (
                        "OPTIONAL one-sentence explanation of why you called "
                        "this tool. REQUIRED when you deviate from the "
                        "planner's suggested_tool (different tool, or refined "
                        "args). Example: 'suggested str_replace old_str did "
                        "not match prior read; reading actual file to get "
                        "correct content.'"
                    ),
                }
            params["properties"] = props
            out.append({
                "type": "function",
                "function": {
                    "name": c.name,
                    "description": c.description,
                    "parameters": params,
                },
            })
        return out

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        state: State,
        recorder: Optional[Recorder] = None,
        contract_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
    ) -> ToolResult:
        cap = self._caps.get(name)
        if cap is None:
            return ToolResult(tool=name, is_error=True, output=f"unknown tool: {name}")

        call_id = tool_call_id or f"tc-{uuid.uuid4().hex[:8]}"

        # R27: strip the execution-metadata `reason` arg out before the handler
        # sees it. The reason is captured in the tool_call trace event (under
        # its own key) and re-attached at the InvocationSchema layer; it is
        # never part of the underlying tool's contract args.
        reason_arg = ""
        if isinstance(args, dict) and "reason" in args:
            raw_reason = args.pop("reason", "") or ""
            reason_arg = str(raw_reason)[:500]

        # parent_event_id chains the tool_call to its triggering event (the
        # capability_invoke that issued it, or the REPRESENT probe write).
        # Falls back to contract_id only for legacy / test callers; new code
        # should always pass parent_event_id.
        tc_parent = parent_event_id if parent_event_id is not None else contract_id

        if recorder is not None:
            recorder.append(TraceEvent(
                event_id=call_id,
                parent_id=tc_parent,
                ts=utcnow_iso(),
                kind="tool_call",
                payload={
                    "tool": name, "args": args, "tool_call_id": call_id,
                    "reason": reason_arg,  # R27: execution-side metadata
                },
            ))

        t0 = time.time()
        try:
            result = cap.handler(args, state)
        except Exception as exc:  # noqa: BLE001 — capability errors must not crash the run
            result = ToolResult(
                tool=name, is_error=True,
                output=f"{type(exc).__name__}: {exc}"[:OUTPUT_CAP_BYTES],
            )
        result.wall_s = time.time() - t0
        state.last_tool_result = {
            "tool": name,
            "args": args,
            "is_error": result.is_error,
            "output": result.output,
            "detail": result.detail,
        }

        if recorder is not None:
            recorder.append(TraceEvent(
                event_id=f"{call_id}-r",
                parent_id=call_id,
                ts=utcnow_iso(),
                kind="tool_result",
                payload={
                    "tool": name,
                    "tool_call_id": call_id,
                    "output": result.output,
                    "is_error": result.is_error,
                    "detail": result.detail,
                    "wall_s": round(result.wall_s, 3),
                },
            ))
        return result
