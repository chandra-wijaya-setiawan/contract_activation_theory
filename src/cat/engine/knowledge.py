"""Knowledge accumulation across hypothesis attempts.

Extracts and structures findings from Performer tool results
so the Stipulator can generate better-informed hypotheses.

See design.v3.md §"KnowledgeSchema".
"""
from __future__ import annotations

import re
from typing import Any

from cat.contract.schemas import (
    KnowledgeSchema, PerformerOutcome, InvocationSchema as ToolInvocation,
)


class KnowledgeAccumulator:
    """Accumulates knowledge across hypothesis attempts."""

    def __init__(self):
        self.knowledge = KnowledgeSchema()

    def update(self, result: PerformerOutcome, oracle_cmd: str) -> None:
        """Extract key findings from the Performer's tool results."""
        for inv in result.invocations:
            if inv.tool_name in ("read", "rg", "bash") and inv.succeeded:
                key = inv.args.get("path") or inv.args.get("pattern") or inv.tool_name
                out = inv.output_text
                if out and len(out) > 20:
                    self.knowledge.findings[str(key)[:60]] = out[:600]

                    # Extract actual method/function/class names from file reads
                    if inv.tool_name == "read":
                        names = re.findall(r'\bdef (\w+)\b', out)
                        classes = re.findall(r'\bclass (\w+)\b', out)
                        if names or classes:
                            found_str = ", ".join(
                                [f"class {c}" for c in classes[:3]] +
                                [f"def {n}" for n in names[:5]]
                            )
                            file_key = str(key)[:40]
                            self.knowledge.findings[f"FOUND_IN:{file_key}"] = (
                                f"Actual names in file: {found_str}. "
                                f"Use THESE names in your hypothesis, not others."
                            )

                    # Explicit negative: rg 0 matches
                    if inv.tool_name == "rg" and "0 matches" in out:
                        pattern = inv.args.get("pattern", "?")
                        self.knowledge.findings[f"NOT_FOUND:{pattern}"] = (
                            f"'{pattern}' does NOT exist anywhere. "
                            f"Do NOT generate hypotheses using this name."
                        )

            if inv.durable_change:
                for f in inv.files_modified:
                    self.knowledge.findings[f"modified:{f}"] = f"changed by: {inv.tool_name}"

    def build_context(self) -> str:
        """Compress knowledge into context string for the Stipulator."""
        if not self.knowledge.findings:
            return ""
        lines = []
        for loc, finding in self.knowledge.findings.items():
            if loc.startswith("NOT_FOUND:"):
                lines.append(f"  {finding}")
        for loc, finding in self.knowledge.findings.items():
            if not loc.startswith("NOT_FOUND:") and ("client.py" in loc or "read" == loc):
                lines.append(f"FILE CONTENT of {loc}:\n{str(finding)[:500]}")
                break
        return "\n".join(lines[:8])

    def build_prior_results(self, prior_attempts: list) -> str:
        """Build a prior_results string for the Performer planner."""
        if not prior_attempts:
            return ""
        lines = []
        for i, attempt in enumerate(prior_attempts, 1):
            h = attempt.get("hypothesis")
            verdict = attempt.get("verdict", "?")
            reason = attempt.get("reason", "")[:150]
            loc = getattr(h, "location", "?") if h else "?"
            change = getattr(h, "change", "?")[:80] if h else "?"
            lines.append(f"Attempt {i}: targeted '{loc}' — {change}")
            lines.append(f"  Result: {verdict} — {reason}")
        for key, finding in self.knowledge.findings.items():
            if key.startswith("modified:"):
                lines.append(f"Already applied: {key.replace('modified:', '')} was changed")
        for f in self.knowledge.oracle_failures[-1:]:
            lines.append(f"Last oracle failure: {f[:200]}")
        return "\n".join(lines)

    def to_schema(self) -> KnowledgeSchema:
        return self.knowledge
