"""Unit tests for the built-in capabilities."""
from __future__ import annotations
import sys
from pathlib import Path

from cat.capability.full_registry import (
    OUTPUT_CAP_BYTES,
    build_default_registry,
    workdir_snapshot,
)
from cat.contract.model import State


# Use the venv's Python explicitly — bare `python` on PATH may resolve to a
# broken uv-managed install (SRE module mismatch) on this machine.
PY = sys.executable.replace("\\", "/")


def test_registry_has_default_tools():
    reg = build_default_registry()
    names = set(reg.names())
    assert {"bash", "write", "read", "list", "str_replace"}.issubset(names)
    # R27: request_revision was deleted; replaced by `reason` arg on every tool.
    assert "request_revision" not in names


def test_every_tool_schema_exposes_reason_param():
    """R27: tool_schemas() injects an optional `reason` property into every
    tool's JSON schema so the Performer can record execution-side metadata."""
    reg = build_default_registry()
    schemas = reg.tool_schemas()
    assert len(schemas) > 0
    for tool in schemas:
        props = tool["function"]["parameters"]["properties"]
        assert "reason" in props, f"tool {tool['function']['name']} missing reason"
        assert props["reason"]["type"] == "string"
        # `reason` must remain OPTIONAL — never in the required list.
        required = tool["function"]["parameters"].get("required", [])
        # done/give_up exit signals require reason by design — they have no
        # other args, so mandatory reason is correct and intentional.
        if tool["function"]["name"] not in ("done", "give_up"):
            assert "reason" not in required, f"reason should not be required on {tool['function']['name']}"


def test_reason_arg_is_stripped_before_handler(tmp_path: Path):
    """R27: when the LLM passes a `reason` arg, the dispatch layer strips
    it from args (handlers see pure tool params). state.last_tool_result.args
    reflects what the handler actually received — `reason` must not appear."""
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    state_args = {"path": "x.txt", "content": "hi", "reason": "exploring write semantics"}
    result = reg.invoke(
        name="write", args=state_args, state=state,
        recorder=None, contract_id="x", tool_call_id="t1",
    )
    assert not result.is_error
    # After invoke, the handler received args without `reason`
    assert "reason" not in state_args, "reason should be popped from args"
    last = state.last_tool_result or {}
    assert "reason" not in (last.get("args") or {})


def test_write_then_read_roundtrip(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    w = reg.invoke("write", {"path": "hello.txt", "content": "hi\n"}, state=state)
    assert not w.is_error
    r = reg.invoke("read", {"path": "hello.txt"}, state=state)
    assert not r.is_error
    assert r.output == "hi\n"


def test_write_rejects_workdir_escape(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    bad = reg.invoke("write", {"path": "../escape.txt", "content": "x"}, state=state)
    assert bad.is_error
    assert "escapes workdir" in bad.output


def test_read_missing_file(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    r = reg.invoke("read", {"path": "nope.txt"}, state=state)
    assert r.is_error
    assert "nope.txt" in r.output


def test_list_shows_entries(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    r = reg.invoke("list", {"path": "."}, state=state)
    assert not r.is_error
    assert "a.txt" in r.output
    assert "sub/" in r.output


def test_bash_runs_in_workdir(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    (tmp_path / "hi.txt").write_text("HELLO")
    r = reg.invoke("bash", {"command": f"\"{PY}\" -c \"import os; print(sorted(os.listdir('.')))\""},
                   state=state)
    assert not r.is_error, r.output
    assert "hi.txt" in r.output


def test_bash_nonzero_exit_is_error(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    r = reg.invoke("bash", {"command": f"\"{PY}\" -c \"import sys; sys.exit(2)\""}, state=state)
    assert r.is_error
    assert r.detail["returncode"] == 2


def test_last_tool_result_recorded_on_state(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    reg.invoke("write", {"path": "z.txt", "content": "x"}, state=state)
    assert state.last_tool_result is not None
    assert state.last_tool_result["tool"] == "write"


def test_workdir_snapshot(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hi2")
    snap = workdir_snapshot(tmp_path)
    assert "a.txt" in snap
    assert "sub" in snap
    assert "sub/b.txt" in snap.replace("\\", "/")


def test_output_truncation(tmp_path: Path):
    reg = build_default_registry()
    state = State(workdir=tmp_path)
    big_n = OUTPUT_CAP_BYTES * 2
    r = reg.invoke("bash", {"command": f"\"{PY}\" -c \"print('x' * {big_n})\""}, state=state)
    assert not r.is_error, r.output
    assert "[truncated" in r.output
    assert len(r.output.encode("utf-8")) <= OUTPUT_CAP_BYTES + 200


# ── docker-registry tests (apply_patch, multi_edit, git_status, git_diff) ──
#
# The docker registry routes everything through `container.exec_run`. We stub
# that with a LocalContainer that runs the argv locally via subprocess; this
# exercises the actual python-via-base64 scripts and the git-apply pipeline
# without needing a real Docker daemon.

import subprocess as _sp

from cat.capability.full_registry import build_docker_registry


class _LocalContainer:
    """Stub Container whose exec_run runs the argv locally with subprocess."""

    def __init__(self, root: Path):
        self.root = root

    def exec_run(self, cmd, workdir=None, demux=False, stdout=True, stderr=True):
        cwd = workdir if workdir and Path(workdir).is_dir() else str(self.root)

        class _Res:
            pass

        try:
            proc = _sp.run(cmd, cwd=cwd, capture_output=True, timeout=30)
        except _sp.TimeoutExpired as exc:
            r = _Res()
            r.exit_code = 124
            r.output = (exc.stdout or b"") + (exc.stderr or b"")
            return r
        r = _Res()
        r.exit_code = proc.returncode
        r.output = (proc.stdout or b"") + (proc.stderr or b"")
        return r


def _init_git_repo(root: Path):
    _sp.run(["git", "init", "-q"], cwd=root, check=True)
    _sp.run(["git", "config", "user.email", "t@e"], cwd=root, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def test_multi_edit_atomic_against_original(tmp_path: Path):
    """All-or-nothing: every edit's old_text validates against ORIGINAL."""
    f = tmp_path / "src.py"
    f.write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke(
        "multi_edit",
        {
            "path": str(f),
            "edits": [
                {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                {"old_text": "gamma = 3", "new_text": "gamma = 30"},
            ],
        },
        state=state,
    )
    assert not r.is_error, r.output
    after = f.read_text()
    assert "alpha = 10" in after
    assert "beta = 2" in after
    assert "gamma = 30" in after


def test_multi_edit_aborts_when_one_edit_misses(tmp_path: Path):
    """If any edit's old_text doesn't match, NO writes happen."""
    f = tmp_path / "src.py"
    original = "alpha = 1\nbeta = 2\n"
    f.write_text(original)
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke(
        "multi_edit",
        {
            "path": str(f),
            "edits": [
                {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                {"old_text": "DOES_NOT_EXIST", "new_text": "x"},
            ],
        },
        state=state,
    )
    assert r.is_error
    assert f.read_text() == original  # rollback: no partial write


def test_multi_edit_rejects_non_unique_old_text(tmp_path: Path):
    f = tmp_path / "src.py"
    f.write_text("x = 1\nx = 1\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke(
        "multi_edit",
        {"path": str(f), "edits": [{"old_text": "x = 1", "new_text": "y"}]},
        state=state,
    )
    assert r.is_error
    assert "matches" in r.output and "specific" in r.output


def test_apply_patch_applies_unified_diff(tmp_path: Path):
    _init_git_repo(tmp_path)
    f = tmp_path / "hello.py"
    f.write_text("print('hi')\n")
    _sp.run(["git", "add", "hello.py"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    patch = (
        "diff --git a/hello.py b/hello.py\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1 +1 @@\n"
        "-print('hi')\n"
        "+print('bye')\n"
    )
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("apply_patch", {"patch": patch}, state=state)
    assert not r.is_error, r.output
    assert f.read_text() == "print('bye')\n"


def test_apply_patch_partial_apply_writes_rej(tmp_path: Path):
    """A diff with one valid + one invalid hunk applies the valid one and
    writes a .rej file the agent can read."""
    _init_git_repo(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("line1\nline2\nline3\n")
    _sp.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    # First hunk matches; second hunk targets nonexistent context
    bad_patch = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-line1\n"
        "+LINE1\n"
        "@@ -50 +50 @@\n"
        "-ghost\n"
        "+phantom\n"
    )
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("apply_patch", {"patch": bad_patch}, state=state)
    # is_error true (some rejected) but partial apply happened
    assert "REJECTS" in r.output


def test_git_status_and_git_diff(tmp_path: Path):
    _init_git_repo(tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("orig\n")
    _sp.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    f.write_text("changed\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    s = reg.invoke("git_status", {}, state=state)
    assert not s.is_error, s.output
    assert "f.txt" in s.output
    d = reg.invoke("git_diff", {}, state=state)
    assert not d.is_error, d.output
    assert "-orig" in d.output and "+changed" in d.output


def test_rg_context_param(tmp_path: Path):
    f = tmp_path / "code.py"
    f.write_text("line_a\nline_b\nMATCH_HERE\nline_d\nline_e\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("rg", {"pattern": "MATCH_HERE", "paths": str(f), "context": 1}, state=state)
    assert not r.is_error, r.output
    # -C 1 should include the line before and after the match
    assert "line_b" in r.output and "line_d" in r.output


# ── symbol_nav (jedi) tests ────────────────────────────────────────────────


def _make_sample_pkg(root: Path):
    """Create a small Python package with goto/refs targets across files."""
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text(
        "def parse(s):\n"
        "    return s.strip()\n"
        "\n"
        "class Widget:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
    )
    (root / "pkg" / "uses.py").write_text(
        "from pkg.core import parse, Widget\n"
        "\n"
        "def run():\n"
        "    w = Widget('x')\n"
        "    return parse(w.name)\n"
    )


def test_outline_lists_top_level_defs(tmp_path: Path):
    _make_sample_pkg(tmp_path)
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("outline", {"path": "pkg/core.py"}, state=state)
    assert not r.is_error, r.output
    import json as _json
    rows = _json.loads(r.output)
    names = {row["name"]: row["type"] for row in rows}
    assert names.get("parse") == "function"
    assert names.get("Widget") == "class"


def test_goto_def_finds_definition_site(tmp_path: Path):
    _make_sample_pkg(tmp_path)
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("goto_def", {"name": "Widget"}, state=state)
    assert not r.is_error, r.output
    import json as _json
    rows = _json.loads(r.output)
    # Should find Widget defined in pkg/core.py
    assert any(row["name"] == "Widget" and "core.py" in row["path"] for row in rows)


def test_find_refs_locates_all_usages(tmp_path: Path):
    _make_sample_pkg(tmp_path)
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("find_refs", {"name": "parse"}, state=state)
    assert not r.is_error, r.output
    import json as _json
    rows = _json.loads(r.output)
    paths = {row["path"] for row in rows if isinstance(row, dict) and "path" in row}
    # parse is defined in core.py and used in uses.py — both should appear
    assert any("core.py" in p for p in paths)
    assert any("uses.py" in p for p in paths)


# ── 1.6.6.14 read truncation nudge tests ────────────────────────────────────


def test_read_supports_offset_and_limit(tmp_path: Path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("read", {"path": str(f), "offset": 10, "limit": 5}, state=state)
    assert not r.is_error, r.output
    # Should contain only lines 10-14
    assert "line 10" in r.output
    assert "line 14" in r.output
    assert "line 15" not in r.output
    assert "line 9" not in r.output
    # Should include the range notice
    assert "Showing lines 10-14 of 100" in r.output


def test_read_truncation_message_tells_total_and_resume(tmp_path: Path):
    """When the file exceeds the 2000-line default, the truncation notice
    must name the total line count AND the offset to resume from."""
    f = tmp_path / "huge.py"
    f.write_text("\n".join(f"L{i}" for i in range(1, 2501)) + "\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("read", {"path": str(f)}, state=state)
    assert not r.is_error, r.output
    # 2000-line cap should kick in
    assert "of 2500" in r.output, r.output[-200:]
    assert "offset=2001" in r.output  # tells the LLM where to resume
    assert "2000 line limit" in r.output


def test_read_full_file_no_notice(tmp_path: Path):
    """Small files that fit entirely should NOT emit a truncation notice."""
    f = tmp_path / "small.py"
    f.write_text("hello\nworld\n")
    reg = build_docker_registry(_LocalContainer(tmp_path), default_workdir=str(tmp_path))
    state = State(workdir=tmp_path)
    r = reg.invoke("read", {"path": str(f)}, state=state)
    assert not r.is_error
    assert "Truncated" not in r.output
    assert "Showing lines" not in r.output
