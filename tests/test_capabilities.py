"""Unit tests for the built-in capabilities."""
from __future__ import annotations
import sys
from pathlib import Path

from cat.capabilities import (
    OUTPUT_CAP_BYTES,
    build_default_registry,
    workdir_snapshot,
)
from cat.contracts import State


# Use the venv's Python explicitly — bare `python` on PATH may resolve to a
# broken uv-managed install (SRE module mismatch) on this machine.
PY = sys.executable.replace("\\", "/")


def test_registry_has_four_tools():
    reg = build_default_registry()
    names = reg.names()
    assert set(names) == {"bash", "write", "read", "list"}


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
    assert "not found" in r.output


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
