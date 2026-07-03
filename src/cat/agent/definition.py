"""Agent definition — data models for the CAT skill system.

Skills are YAML-defined, script-executed units that require zero Python to add.

CAT Skill system — YAML-defined, script-executed, zero Python to add a skill.

A skill is a directory:
    my_profile/
        skill.yaml      ← LoopConfig + represent text (required)
        oracle.sh       ← bash oracle: exit 0 = pass, stdout = evidence (optional)
        oracle.ps1      ← Windows alternative (optional)
        setup.sh        ← optional: run before the hypothesis loop
        teardown.sh     ← optional: run after the hypothesis loop

The core never imports skill code. It reads YAML (data) and spawns scripts
(subprocess IPC via exit code + stdout). Adding a new skill is zero Python.

Search order (first match wins):
    1. explicit path (--profile ./my_profile.yaml or directory)
    2. ./profiles/         project-local
    3. ~/.cat/profiles/    user-defined
    4. cat/agent/profiles/   framework defaults

Trust follows the same model as Makefile / package.json scripts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional



@dataclass
class LoopConfig:
    """Controls which steps the hypothesis loop runs and at what capacity.

    Model selection:
      model_array   — ordered list of model IDs, cheapest first (T0, T1, T2, …)
                      The skill owns this; env var CAT_MODEL_ARRAY is the fallback.
      intelligence  — float 0.0–1.0: which tier to start on.
                      0.0 = T0 (fastest), 1.0 = last tier (strongest).
                      The kernel climbs tiers on give_up / escalation.
    """
    # Step skips
    skip_representer:      bool = False   # if True: use skill's represent list only
    skip_stipulator:       bool = False   # if True: pass obligation straight to Performer
    skip_oracle:           bool = False   # if True: always break after 1 hypothesis
    require_explicit_done: bool = False   # if True: durable changes alone don't fulfill; Performer must call done()
    # Capacity
    max_hypotheses:     int   = 6
    max_inner_attempts: int   = 5
    # Model selection — skill owns the array; env var CAT_MODEL_ARRAY is the fallback
    model_array:        list  = field(default_factory=list)
    intelligence:       float = 0.5   # 0.0=fastest tier, 1.0=strongest tier
    speed:              float = 0.5   # reserved — future streaming latency target



@dataclass
class AgentDef:
    """Loaded from skill.yaml — pure data, no code."""
    name:        str
    description: str = ""
    loop_config: LoopConfig = field(default_factory=LoopConfig)
    # represent: list of {key, value} dicts from YAML
    represent:   list[dict] = field(default_factory=list)
    # representer configuration
    representer_mode:   str  = "bash_probes"   # "llm_call" | "bash_probes" | "probes_then_llm_call" | "adapter"
    representer_probes: list = field(default_factory=list)  # [{key, command}, ...]
    # Paths to scripts (None = not present)
    oracle_script:   Optional[Path] = None
    setup_script:    Optional[Path] = None
    teardown_script: Optional[Path] = None
    # Optional Python module override for complex skills (e.g. SWEBench)
    module:      Optional[str] = None


