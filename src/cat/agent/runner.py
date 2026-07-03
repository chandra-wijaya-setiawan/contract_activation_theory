"""Script runner — spawns oracle/setup/teardown shell scripts as subprocesses.

Protocol:
  oracle.sh <workdir>   → exit 0 = passed, non-zero = failed
                          stdout = evidence summary (captured, shown to user)
  setup.sh  <workdir>   → exit 0 = ok, non-zero = setup failed (logged, non-fatal)
  teardown.sh <workdir> → exit 0 = ok (always runs, errors ignored)

The core never imports skill code — all behavior crosses the process boundary
via exit codes and stdout/stderr. Adding a new oracle is just editing a .sh file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def run_oracle(
    script: Path,
    workdir: Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Run oracle.sh and return (passed, evidence_text)."""
    try:
        r = subprocess.run(
            ["bash", str(script), str(workdir)],
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=timeout,
        )
        output = (r.stdout + r.stderr)[-3000:]
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"oracle timeout after {timeout}s"
    except Exception as exc:
        return False, f"oracle error: {exc}"


def run_setup(
    script: Path,
    workdir: Path,
    timeout: int = 60,
) -> tuple[bool, str]:
    """Run setup.sh — returns (ok, output). Non-fatal on failure."""
    try:
        r = subprocess.run(
            ["bash", str(script), str(workdir)],
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=timeout,
        )
        return r.returncode == 0, (r.stdout + r.stderr)[-1000:]
    except Exception as exc:
        return False, str(exc)


def run_teardown(
    script: Optional[Path],
    workdir: Path,
    timeout: int = 30,
) -> None:
    """Run teardown.sh — always, errors silently ignored."""
    if script is None or not script.exists():
        return
    try:
        subprocess.run(
            ["bash", str(script), str(workdir)],
            capture_output=True,
            cwd=str(workdir),
            timeout=timeout,
        )
    except Exception:
        pass
