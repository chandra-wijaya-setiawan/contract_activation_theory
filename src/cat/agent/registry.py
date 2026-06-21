"""AgentRegistry — finds and loads agent profile definitions from YAML files.

Search order (first match wins):
  1. Explicit path passed directly (absolute, relative, or .yaml file)
  2. ./profiles/<name>/        project-local directory
  3. ./profiles/<name>.yaml    project-local single file
  4. ~/.cat/profiles/<name>/   user-defined directory
  5. ~/.cat/profiles/<name>.yaml
  6. <package>/agent/profiles/<name>/    framework defaults
"""
from __future__ import annotations

import platform
from pathlib import Path
from typing import Optional

import yaml

from cat.agent.definition import AgentDef, LoopConfig

# Built-in profiles directory (shipped with the package)
_BUILTIN_DIR = Path(__file__).parent / "profiles"

# Per-platform oracle script name preference
_ORACLE_NAMES = (
    ("oracle.ps1",) if platform.system() == "Windows" else ()
) + ("oracle.sh", "oracle")

_SETUP_NAMES    = ("setup.sh",    "setup.ps1")
_TEARDOWN_NAMES = ("teardown.sh", "teardown.ps1")


def _find_script(skill_dir: Path, names: tuple) -> Optional[Path]:
    for name in names:
        p = skill_dir / name
        if p.exists():
            return p
    return None


def _parse_loop_config(raw: dict) -> LoopConfig:
    cfg = LoopConfig()
    cfg.skip_representer      = bool(raw.get("skip_representer",      cfg.skip_representer))
    cfg.skip_stipulator       = bool(raw.get("skip_stipulator",       cfg.skip_stipulator))
    cfg.skip_oracle           = bool(raw.get("skip_oracle",           cfg.skip_oracle))
    cfg.require_explicit_done = bool(raw.get("require_explicit_done", cfg.require_explicit_done))
    cfg.max_hypotheses     = int(raw.get("max_hypotheses",      cfg.max_hypotheses))
    cfg.max_inner_attempts = int(raw.get("max_inner_attempts",  cfg.max_inner_attempts))
    cfg.intelligence       = float(raw.get("intelligence",      cfg.intelligence))
    cfg.speed              = float(raw.get("speed",             cfg.speed))
    # model_array: skill owns the tiers; falls back to env var if empty
    raw_array = raw.get("model_array", [])
    cfg.model_array = [str(m) for m in raw_array] if raw_array else []
    return cfg


def _load_yaml(yaml_path: Path, skill_dir: Path) -> AgentDef:
    """Parse a skill.yaml profile and discover sibling scripts."""
    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    loop_cfg = _parse_loop_config(raw.get("loop_config", {}))

    rep_raw = raw.get("representer", {})
    representer_mode   = str(rep_raw.get("mode", "bash_probes"))
    representer_probes = list(rep_raw.get("probes", []))

    if representer_mode == "adapter":
        loop_cfg.skip_representer = True

    return AgentDef(
        name         = raw.get("name", yaml_path.parent.name),
        description  = raw.get("description", ""),
        loop_config  = loop_cfg,
        represent    = raw.get("represent", []),
        representer_mode   = representer_mode,
        representer_probes = representer_probes,
        oracle_script   = _find_script(skill_dir, _ORACLE_NAMES),
        setup_script    = _find_script(skill_dir, _SETUP_NAMES),
        teardown_script = _find_script(skill_dir, _TEARDOWN_NAMES),
        module          = raw.get("module"),
    )


def _search_dirs() -> list[Path]:
    dirs: list[Path] = []
    # project-local
    local = Path("profiles")
    if local.is_dir():
        dirs.append(local)
    # user
    user = Path.home() / ".cat" / "profiles"
    if user.is_dir():
        dirs.append(user)
    # built-in
    if _BUILTIN_DIR.is_dir():
        dirs.append(_BUILTIN_DIR)
    return dirs


class AgentRegistry:
    """Locate and load an AgentDef by name or path."""

    def load(self, name_or_path: str) -> AgentDef:
        """Return the first matching AgentDef or raise FileNotFoundError."""
        p = Path(name_or_path)

        # 1. Explicit path to a .yaml file
        if p.suffix == ".yaml" and p.exists():
            return _load_yaml(p, p.parent)

        # 2. Explicit path to a directory containing profile.yaml
        if p.is_dir() and (p / "profile.yaml").exists():
            return _load_yaml(p / "profile.yaml", p)

        # 3. Search standard locations by name
        name = name_or_path
        for search_dir in _search_dirs():
            # directory form: profiles/<name>/profile.yaml
            candidate_dir = search_dir / name
            if candidate_dir.is_dir() and (candidate_dir / "profile.yaml").exists():
                return _load_yaml(candidate_dir / "profile.yaml", candidate_dir)
            # flat form: profiles/<name>.yaml
            candidate_yaml = search_dir / f"{name}.yaml"
            if candidate_yaml.exists():
                return _load_yaml(candidate_yaml, candidate_yaml.parent)

        raise FileNotFoundError(
            f"Profile '{name_or_path}' not found. "
            f"Search paths: {[str(d) for d in _search_dirs()]}"
        )

    def list_available(self) -> list[str]:
        """Return names of all discoverable profiles (for --list tab-complete / help)."""
        names: list[str] = []
        seen: set[str] = set()
        for search_dir in _search_dirs():
            if not search_dir.is_dir():
                continue
            for child in sorted(search_dir.iterdir()):
                if child.is_dir() and (child / "profile.yaml").exists():
                    if child.name not in seen:
                        names.append(child.name)
                        seen.add(child.name)
                elif child.suffix == ".yaml" and child.stem not in seen:
                    names.append(child.stem)
                    seen.add(child.stem)
        return names



