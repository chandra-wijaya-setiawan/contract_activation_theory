"""Essay experiment — run CAT on a writing task with and without structural constraints.

Usage:
    python scripts/essay_experiment.py unconstrained
    python scripts/essay_experiment.py constrained
    python scripts/essay_experiment.py both
"""
from __future__ import annotations

import os
import sys
import textwrap
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

# ── minimal in-process State + Contract setup ─────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from cat.contracts import Contract, Criterion, Evidence, State, Representation
from cat.activator import Activator
from cat.trace import Recorder


# ── output buffer: criteria read essay from state.kv["essay"] ──────────────────
ESSAY_KEY = "essay"


def _make_essay_criteria(mode: str) -> list[Criterion]:
    """
    unconstrained: single criterion — did the agent produce *any* essay text?
    constrained:   three criteria — length (~800 words), required sections, thesis statement
    """
    if mode == "unconstrained":
        def check_produced(state: State) -> tuple[bool, Evidence]:
            text = state.kv.get(ESSAY_KEY, "")
            ok = len(text.strip()) > 100
            return ok, Evidence(
                summary="Essay produced" if ok else "No essay in state.kv['essay']",
                detail={"output": text[:500], "length": len(text)},
            )
        return [Criterion(id="essay-produced", description="Essay text was produced",
                          predicate=check_produced, is_oracle=True)]
    else:
        criteria = []

        def check_length(state: State) -> tuple[bool, Evidence]:
            text = state.kv.get(ESSAY_KEY, "")
            words = len(text.split())
            ok = 700 <= words <= 1000
            return ok, Evidence(
                summary=f"Word count {words} {'OK' if ok else 'OUT OF RANGE (need 700-1000)'}",
                detail={"output": f"Word count: {words}\n{text[:200]}", "words": words},
            )

        def check_sections(state: State) -> tuple[bool, Evidence]:
            text = state.kv.get(ESSAY_KEY, "").lower()
            required = ["introduction", "evidence", "conclusion"]
            missing = [s for s in required if s not in text]
            ok = not missing
            return ok, Evidence(
                summary="All sections present" if ok else f"Missing sections: {missing}",
                detail={"output": f"Missing: {missing}\nText preview: {text[:300]}"},
            )

        def check_thesis(state: State) -> tuple[bool, Evidence]:
            text = state.kv.get(ESSAY_KEY, "")
            # Must mention natural selection + adaptation somewhere
            lower = text.lower()
            ok = "natural selection" in lower and ("adapt" in lower or "survival" in lower)
            return ok, Evidence(
                summary="Core thesis present" if ok else "Missing 'natural selection' or adaptation concept",
                detail={"output": text[:400]},
            )

        criteria.append(Criterion(id="length", description="Essay is 700-1000 words",
                                   predicate=check_length, is_oracle=True))
        criteria.append(Criterion(id="sections", description="Has Introduction, Evidence, Conclusion sections",
                                   predicate=check_sections, is_oracle=True))
        criteria.append(Criterion(id="thesis", description="Mentions natural selection and adaptation",
                                   predicate=check_thesis, is_oracle=True))
        return criteria


def _make_essay_capability():
    """Essay registry: local capabilities + write_essay tool."""
    from cat.capabilities import Capability, ToolResult, build_default_registry

    def write_essay(args: dict, state: State):
        content = args.get("content", "")
        state.kv[ESSAY_KEY] = content
        return ToolResult(tool="write_essay", is_error=False,
                          output=f"Essay stored ({len(content.split())} words)")

    cap = Capability(
        name="write_essay",
        description="Store the final essay text. Call this once when the essay is ready.",
        json_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The full essay text"},
            },
            "required": ["content"],
        },
        handler=write_essay,
    )

    reg = build_default_registry()
    reg.register(cap)
    return reg


def run_essay(mode: str) -> None:
    print(f"\n{'='*60}")
    print(f"  ESSAY EXPERIMENT — mode: {mode.upper()}")
    print(f"{'='*60}\n")

    constraints = ("" if mode == "unconstrained" else
                   "Requirements:\n"
                   "- Must be 700-1000 words\n"
                   "- Must have three labelled sections: Introduction, Evidence, Conclusion\n"
                   "- Must explain natural selection and adaptation\n")
    obligation = textwrap.dedent(f"""
        Write an essay about Darwin's theory of evolution and submit it using write_essay.

        {constraints}
        IMPORTANT: You MUST call write_essay(content=<full essay text>) to submit the essay.
        Do NOT use bash or write files. Use ONLY the write_essay tool.
    """).strip()

    state = State(workdir=Path("/tmp"))
    state.kv[ESSAY_KEY] = ""

    criteria = _make_essay_criteria(mode)

    # Only expose write_essay; block bash/read/write to prevent the agent from
    # "writing code" instead of calling the designated submission tool.
    contract = Contract.new(
        obligation=obligation,
        criteria=criteria,
        capabilities=["write_essay"],
    )
    # Pre-populate a trivial representation so REPRESENT step is a no-op
    contract.representations = [
        Representation(key="task", value="write essay", evidence="direct", probed_at=time.time())
    ]

    logs_dir = Path(f"/tmp/essay-experiment-{mode}-{uuid.uuid4().hex[:8]}")
    logs_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(
        run_id=f"essay-{mode}",
        task_id=f"essay-{mode}",
        agent="cat-essay",
        model=os.environ.get("CAT_MODEL", "openrouter/deepseek/deepseek-v4-flash"),
        out_dir=logs_dir,
    )

    cap_reg = _make_essay_capability()

    # _BaseActivator.__init__(llm, registry, recorder=...)
    # llm=None is fine here — Activator creates its own kernel-resolved LLMs
    from cat.llm import LLM
    model = os.environ.get("CAT_MODEL", "openrouter/deepseek/deepseek-v4-flash")
    llm = LLM(model=model, recorder=recorder)
    activator = Activator(llm, cap_reg, recorder=recorder)

    t0 = time.time()
    run = activator.run(contract, state)
    elapsed = time.time() - t0

    essay_text = state.kv.get(ESSAY_KEY, "")
    word_count = len(essay_text.split()) if essay_text else 0

    print(f"\nResult:     {contract.state.value}")
    print(f"Words:      {word_count}")
    print(f"Elapsed:    {elapsed:.1f}s")
    print(f"Trace:      {logs_dir}")
    print(f"\n--- Essay ---\n")
    print(essay_text if essay_text else "(no essay produced)")
    print(f"\n--- End ---\n")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode == "both":
        run_essay("unconstrained")
        run_essay("constrained")
    else:
        run_essay(mode)
