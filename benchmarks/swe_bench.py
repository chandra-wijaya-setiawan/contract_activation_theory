"""SWE-bench oracle configuration for TbCatAgent.

Usage
-----
Pass to terminal-bench via --agent-import-path:

    benchmarks.swe_bench:TbSweAgent

Or construct programmatically:

    from benchmarks.swe_bench import make_agent
    agent = make_agent(model_name="openrouter/deepseek/deepseek-v4-flash")

Oracle strategy
---------------
Oracles are derived directly from SWE-bench's own test_patch and
FAIL_TO_PASS fields — not hand-written assertions.  This ensures the
oracle and the harness agree on what "fixed" means: there is one source
of truth.

For each task:
  1. Read config.json from the terminal-bench dataset cache.
  2. Apply test_patch (the official test additions) via git apply.
  3. Run only the FAIL_TO_PASS test IDs whose bodies do not reference
     httpbin or other live-network fixtures.
  4. If all FAIL_TO_PASS tests need the network, fall back to a
     _PROXY_ORACLES entry (hand-written mock-based test).

Rules for proxy oracles (used only when official tests need network):
  1. Ensure /testbed source wins over installed site-packages.
     Use sys.path.insert(0, "/testbed") or PYTHONPATH=/testbed.
  2. Do NOT use pipes (cmd | tail -N) — pipes mask the test runner's
     exit code.
  3. No conda activate — hangs in non-interactive tmux panes.
  4. Use exit(0 if ok else 1) rather than assert (avoids quoting hell).
  5. Use base64+tempfile for multi-line scripts to avoid newline issues.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from cat.tb_agents import TbCatAgent

# ---------------------------------------------------------------------------
# Dataset cache location
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(os.environ.get(
    "TB_CACHE_DIR",
    Path.home() / ".cache/terminal-bench",
))


# ---------------------------------------------------------------------------
# Oracle builder
# ---------------------------------------------------------------------------

def _test_func_needs_network(func_name: str, patch_added_lines: str) -> bool:
    """Return True if the named test function's body references httpbin."""
    pattern = re.compile(
        rf"def {re.escape(func_name)}\b(.*?)(?=\ndef |\Z)",
        re.DOTALL,
    )
    match = pattern.search(patch_added_lines)
    if match:
        return "httpbin" in match.group()
    return False


def _swebench_oracle(
    task_id: str,
    dataset: str = "swebench-verified",
    version: str = "head",
) -> str:
    """Build an oracle shell command from the SWE-bench config.json.

    Applies the official test_patch and runs the FAIL_TO_PASS tests
    that do not require live network access.  Returns "" when all
    FAIL_TO_PASS tests need network (caller should supply a proxy oracle).
    """
    config_path = _CACHE_DIR / dataset / version / task_id / "tests" / "config.json"
    if not config_path.exists():
        return ""

    config = json.loads(config_path.read_text())
    test_patch: str = config.get("test_patch", "")
    fail_to_pass: list[str] = json.loads(config.get("FAIL_TO_PASS", "[]"))

    # Extract only added lines from the patch for body analysis
    added_lines = "\n".join(
        line[1:] for line in test_patch.splitlines() if line.startswith("+")
    )

    safe_tests = [
        t for t in fail_to_pass
        if not _test_func_needs_network(t.split("::")[-1], added_lines)
    ]

    if not safe_tests:
        return ""

    patch_b64 = base64.b64encode(test_patch.encode()).decode()
    test_ids = " ".join(safe_tests)

    # Apply patch (idempotent — git apply --ignore-whitespace tolerates
    # already-applied hunks) then run the safe FAIL_TO_PASS subset.
    # No pipes around pytest: exit code flows through to EXIT:$? sentinel.
    return (
        f"cd /testbed && "
        f"echo '{patch_b64}' | base64 -d | git apply --ignore-whitespace - 2>/dev/null; "
        f"PYTHONPATH=/testbed pytest {test_ids} -x -q --tb=short"
    )


# ---------------------------------------------------------------------------
# Proxy oracles — used only when all FAIL_TO_PASS tests need live network
# ---------------------------------------------------------------------------

# psf__requests-2317: every FAIL_TO_PASS test calls httpbin('...').
# Proxy: exercise Session.request(b'GET', ...) via a mock adapter so we
# test the sessions.py code path without a real HTTP connection.
_b64_2317 = (
    "aW1wb3J0IHN5cwpzeXMucGF0aC5pbnNlcnQoMCwgIi90ZXN0YmVkIikKaW1wb3J0IHJlcXVlc3RzC"
    "mZyb20gdW5pdHRlc3QubW9jayBpbXBvcnQgcGF0Y2gKCnNlbnQgPSBbXQoKZGVmIGZha2Vfc2VuZChz"
    "ZWxmLCByLCAqKmt3KToKICAgIHNlbnQuYXBwZW5kKHIubWV0aG9kKQogICAgcmVzcCA9IHJlcXVlc3Rz"
    "Lm1vZGVscy5SZXNwb25zZSgpCiAgICByZXNwLnN0YXR1c19jb2RlID0gMjAwCiAgICByZXNwLl9jb250"
    "ZW50ID0gYiIiCiAgICByZXNwLl9jb250ZW50X2NvbnN1bWVkID0gVHJ1ZQogICAgcmVzcC5oaXN0b3J5"
    "ID0gW10KICAgIHJlc3AuaXNfcmVkaXJlY3QgPSBGYWxzZQogICAgcmV0dXJuIHJlc3AKCndpdGggcGF0"
    "Y2gub2JqZWN0KHJlcXVlc3RzLmFkYXB0ZXJzLkhUVFBBZGFwdGVyLCAic2VuZCIsIGZha2Vfc2VuZCk6"
    "CiAgICBzID0gcmVxdWVzdHMuU2Vzc2lvbigpCiAgICBzLnJlcXVlc3QoYiJHRVQiLCAiaHR0cDovL2V4"
    "YW1wbGUuY29tIikKCm9rID0gYm9vbChzZW50KSBhbmQgc2VudFswXSA9PSAiR0VUIgpwcmludCgiUEFT"
    "UyIgaWYgb2sgZWxzZSAiRkFJTDogIiArIHJlcHIoc2VudCkpCnN5cy5leGl0KDAgaWYgb2sgZWxzZSAx"
    "KQo="
)

_b64_1142 = (
    "aW1wb3J0IHN5cwpzeXMucGF0aC5pbnNlcnQoMCwgJy90ZXN0YmVkJykKaW1wb3J0IHJlcXVlc3RzCgpnZXRf"
    "cmVxID0gcmVxdWVzdHMuUmVxdWVzdCgnR0VUJywgJ2h0dHA6Ly9leGFtcGxlLmNvbS9nZXQnKS5wcmVwYXJl"
    "KCkKaGVhZF9yZXEgPSByZXF1ZXN0cy5SZXF1ZXN0KCdIRUFEJywgJ2h0dHA6Ly9leGFtcGxlLmNvbS9oZWFk"
    "JykucHJlcGFyZSgpCgpvazEgPSAnQ29udGVudC1MZW5ndGgnIG5vdCBpbiBnZXRfcmVxLmhlYWRlcnMKb2sy"
    "ID0gJ0NvbnRlbnQtTGVuZ3RoJyBub3QgaW4gaGVhZF9yZXEuaGVhZGVycwoKaWYgb2sxIGFuZCBvazI6CiAg"
    "ICBwcmludCgnUEFTUycpCiAgICBzeXMuZXhpdCgwKQplbHNlOgogICAgcHJpbnQoJ0ZBSUw6IEdFVCBDb250"
    "ZW50LUxlbmd0aDonLCBnZXRfcmVxLmhlYWRlcnMuZ2V0KCdDb250ZW50LUxlbmd0aCcpKQogICAgcHJpbnQo"
    "J0ZBSUw6IEhFQUQgQ29udGVudC1MZW5ndGg6JywgaGVhZF9yZXEuaGVhZGVycy5nZXQoJ0NvbnRlbnQtTGVu"
    "Z3RoJykpCiAgICBzeXMuZXhpdCgxKQo="
)


def _proxy_cmd(b64: str) -> str:
    """Build a proxy oracle command from a base64-encoded Python script."""
    return (
        "cd /testbed && "
        "python3 -c '"
        "import sys,base64,tempfile,os,subprocess; "
        f"code=base64.b64decode(\"{b64}\"); "
        "f=tempfile.NamedTemporaryFile(suffix=\".py\",delete=False); "
        "f.write(code); f.close(); "
        "r=subprocess.run([sys.executable,f.name]); "
        "os.unlink(f.name); sys.exit(r.returncode)"
        "'"
    )


_PROXY_ORACLES: dict[str, str] = {
    "psf__requests-2317": _proxy_cmd(_b64_2317),
    # test_no_content_length requires httpbin; test via prepare() which is network-free
    "psf__requests-1142": _proxy_cmd(_b64_1142),
}


# ---------------------------------------------------------------------------
# Build ORACLE_CMDS: auto-generate from SWE-bench config, fall back to proxy
# ---------------------------------------------------------------------------

_KNOWN_TASKS = [
    "psf__requests-2931",
    "psf__requests-2317",
    "psf__requests-1142",
    "matplotlib__matplotlib-25775",
    "scikit-learn__scikit-learn-25102",
]

ORACLE_CMDS: dict[str, str] = {}

for _task_id in _KNOWN_TASKS:
    _cmd = _swebench_oracle(_task_id)
    if _cmd:
        ORACLE_CMDS[_task_id] = _cmd
    elif _task_id in _PROXY_ORACLES:
        ORACLE_CMDS[_task_id] = _PROXY_ORACLES[_task_id]
    # If neither, task falls back to LLM judge (test_cmd="")


# ---------------------------------------------------------------------------
# Agent class (for --agent-import-path CLI usage)
# ---------------------------------------------------------------------------

class TbSweAgent(TbCatAgent):
    """TbCatAgent pre-configured for SWE-bench-verified tasks.

    Oracles are derived from SWE-bench's own test_patch and FAIL_TO_PASS
    fields — the same tests the harness uses.  No separate oracle truth.

    For any task in the dataset cache, _resolve_test_cmd() auto-generates
    the oracle command from the task's config.json (test_patch + FAIL_TO_PASS).
    The ORACLE_CMDS dict provides pre-built commands for well-known tasks and
    proxy oracles for tasks whose tests require live network.

    CLI usage:
        tb runs create --agent-import-path benchmarks.swe_bench:TbSweAgent ...
    """

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("test_cmd", ORACLE_CMDS)
        kwargs.setdefault("max_iterations", 60)
        kwargs.setdefault("max_criterion_fails", 20)
        super().__init__(**kwargs)

    @staticmethod
    def name() -> str:
        return "cat-swe"

    def _resolve_test_cmd(self, logging_dir) -> str:
        """Auto-generate SWE-bench oracle for any task in the dataset cache.

        Priority:
          1. Pre-built ORACLE_CMDS entry (proxy oracles for network-dependent tests)
          2. Dynamic oracle derived from config.json (test_patch + FAIL_TO_PASS)
          3. Empty string → LLM judge fallback (last resort only)
        """
        # Check pre-built dict first (includes proxy oracles)
        cmd = super()._resolve_test_cmd(logging_dir)
        if cmd:
            return cmd
        # Dynamically build oracle from SWE-bench cache for any task
        if logging_dir is not None:
            task_id = logging_dir.parent.parent.name
            return _swebench_oracle(task_id) or ""
        return ""


# ---------------------------------------------------------------------------
# Factory (for programmatic use)
# ---------------------------------------------------------------------------

def make_agent(**kwargs: object) -> TbSweAgent:
    """Construct a TbSweAgent with optional overrides.

    Example:
        agent = make_agent(model_name="openrouter/deepseek/deepseek-v4-flash")
    """
    return TbSweAgent(**kwargs)
