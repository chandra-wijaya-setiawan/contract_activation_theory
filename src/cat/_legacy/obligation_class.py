"""obligation_class — task-level canonicalization (docs/iteration.md §8.1).

L1 categorical hash over (framework, subsystem-top-2). Indexes plan trees
in the librarian for cross-task transfer.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Optional


_PATH_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_/]*\.py)")
_REPO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9_-]*)__([a-zA-Z][a-zA-Z0-9_-]*)-(\d+)")
# matches e.g. "django__django-11239" terminal-bench style


def compute_obligation_class(
    obligation: str,
    representations: "Optional[list[Any]]" = None,
    task_id: "Optional[str]" = None,
) -> str:
    """Returns the L1 obligation_class hash (12 hex chars).

    L1 = (framework, subsystem-top-2). Falls back gracefully when either
    cannot be extracted.
    """
    framework = _extract_framework(obligation, representations, task_id)
    subsystem = _extract_subsystem(representations)
    return hashlib.sha256(f"{framework}|{subsystem}".encode()).hexdigest()[:12]


def compute_obligation_class_features(
    obligation: str,
    representations: "Optional[list[Any]]" = None,
    task_id: "Optional[str]" = None,
) -> dict[str, str]:
    """Returns the same hash + the underlying features (for debugging/logging)."""
    framework = _extract_framework(obligation, representations, task_id)
    subsystem = _extract_subsystem(representations)
    h = hashlib.sha256(f"{framework}|{subsystem}".encode()).hexdigest()[:12]
    return {"framework": framework, "subsystem": subsystem, "hash": h}


def _extract_framework(
    obligation: str,
    reps: "Optional[list[Any]]",
    task_id: "Optional[str]",
) -> str:
    # 1. task_id is the strongest signal in terminal-bench / SWE-bench
    if task_id:
        m = _REPO_RE.match(task_id)
        if m:
            return m.group(1).lower()
    # 2. Try repository root from representations
    if reps:
        for r in reps:
            ev = getattr(r, "evidence", "") or ""
            # look for "Repository root: <name>"
            mr = re.search(r"(?:repository root|repo|workdir)[:=]\s*[/\\]?([a-zA-Z_][a-zA-Z0-9_-]*)", ev, re.IGNORECASE)
            if mr:
                return mr.group(1).lower()
            # look for /testbed/<framework>/ in paths
            for path_m in _PATH_RE.finditer(ev):
                p = path_m.group(1)
                parts = p.strip("/").split("/")
                if parts:
                    return parts[0].lower()
    # 3. Look in obligation text for first-mentioned package
    for path_m in _PATH_RE.finditer(obligation):
        p = path_m.group(1)
        parts = p.strip("/").split("/")
        if parts:
            return parts[0].lower()
    return "unknown"


def _extract_subsystem(reps: "Optional[list[Any]]") -> str:
    """Top-2 path components of longest common prefix of *.py paths in evidence,
    after dropping the framework component."""
    if not reps:
        return "unknown"
    all_paths: list[list[str]] = []
    for r in reps:
        ev = getattr(r, "evidence", "") or ""
        for m in _PATH_RE.finditer(ev):
            p = m.group(1).strip("/")
            parts = p.split("/")
            # drop the .py file itself
            parts = parts[:-1] if parts and parts[-1].endswith(".py") else parts
            if parts:
                all_paths.append(parts)
    if not all_paths:
        return "unknown"
    # Longest common prefix
    lcp = list(all_paths[0])
    for parts in all_paths[1:]:
        new_lcp: list[str] = []
        for a, b in zip(lcp, parts):
            if a == b:
                new_lcp.append(a)
            else:
                break
        lcp = new_lcp
        if not lcp:
            break
    # Drop framework component (lcp[0]) and take top 2 of remainder
    after_framework = lcp[1:] if len(lcp) > 1 else lcp
    top2 = after_framework[:2]
    if not top2:
        return "unknown"
    return ".".join(top2)
