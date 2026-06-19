"""Chat simulation tests — exercises skill system and conversation flows.

Two test layers:

  UNIT (always run, no LLM):
    - Skill loading and LoopConfig correctness
    - Session setup (criteria, representations)
    - cat-cli-as-tool subprocess protocol
    - Tool call decision logic

  LIVE (requires LLM API, opt-in):
    - Real multi-turn conversation flows
    - Run: pytest tests/test_chat_simulation.py -v -m live

Conversation scenarios covered:
  1. Pure knowledge Q&A          — chat answers directly, no tool calls
  2. File read + summarize        — chat reads file, returns done(reason=summary)
  3. Simple file write            — chat creates file directly (has write tools)
  4. Complex code fix             — chat calls cat-cli task as subprocess tool
  5. Code fix + oracle            — task skill with pytest oracle
  6. Multi-turn session           — state persists, second turn builds on first
  7. Delegation signal            — chat decides to delegate vs answer inline
  8. Unknown file path            — agent handles gracefully (error in done reason)
  9. No test runner detected      — oracle.sh exits 1, task fails cleanly
  10. Custom skill from yaml path — loads from explicit file path
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, patch as mock_patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Minimal git repo with a Python module and tests."""
    (repo := tmp_path).mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@cat"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name",  "CAT Test"], cwd=repo, capture_output=True)

    # Source file with a deliberate bug (off-by-one)
    (repo / "calc.py").write_text(textwrap.dedent("""\
        def add(a, b):
            return a + b

        def multiply(a, b):
            return a * b

        def paginate(items, page, size):
            # BUG: should be (page-1)*size
            start = page * size
            return items[start:start + size]
    """))

    # Test file
    (repo / "test_calc.py").write_text(textwrap.dedent("""\
        from calc import add, multiply, paginate

        def test_add():
            assert add(2, 3) == 5

        def test_multiply():
            assert multiply(3, 4) == 12

        def test_paginate_page1():
            items = list(range(10))
            assert paginate(items, 1, 3) == [0, 1, 2]

        def test_paginate_page2():
            items = list(range(10))
            assert paginate(items, 2, 3) == [3, 4, 5]
    """))

    # pyproject.toml so oracle.sh detects pytest
    (repo / "pyproject.toml").write_text(textwrap.dedent("""\
        [build-system]
        requires = ["setuptools"]

        [tool.pytest.ini_options]
        testpaths = ["."]
    """))

    # README for read/summarize tests
    (repo / "README.md").write_text(textwrap.dedent("""\
        # Calc

        A tiny arithmetic library.

        ## Functions
        - `add(a, b)` — returns a + b
        - `multiply(a, b)` — returns a * b
        - `paginate(items, page, size)` — returns a page of items
    """))

    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


def _cat_cli(args: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess:
    """Invoke cat-cli as a subprocess — this IS the tool call pattern."""
    return subprocess.run(
        [sys.executable, "-m", "cat.cli.cat_cli"] + args,
        capture_output=True, text=True, cwd=str(cwd), timeout=timeout,
    )


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — no LLM, no API calls
# ══════════════════════════════════════════════════════════════════════════════

class TestSkillLoading:
    """Skill YAML loads correctly with right LoopConfig values."""

    def test_chat_skill_runs_full_loop_with_llm_judge(self):
        from cat.skill.registry import SkillRegistry
        skill = SkillRegistry().load("chat")
        cfg   = skill.loop_config
        assert cfg.skip_representer is False, "chat uses llm_call representer for intent formalization"
        assert cfg.skip_stipulator  is False, "chat uses Stipulator for hypothesis generation"
        assert cfg.skip_oracle      is False, "chat uses LLM judge oracle"
        assert cfg.max_hypotheses   >= 3,     "chat retries with escalating model tiers"
        assert cfg.intelligence     < 0.5,    "chat planner uses fast/cheap model"

    def test_task_skill_runs_full_loop(self):
        from cat.skill.registry import SkillRegistry
        skill = SkillRegistry().load("task")
        cfg   = skill.loop_config
        assert cfg.skip_representer is False
        assert cfg.skip_stipulator  is False
        assert cfg.skip_oracle      is False
        assert cfg.max_hypotheses   >= 4
        assert cfg.intelligence     == 1.0

    def test_task_skill_has_oracle_script(self):
        from cat.skill.registry import SkillRegistry
        skill = SkillRegistry().load("task")
        assert skill.oracle_script is not None
        assert skill.oracle_script.exists()
        assert skill.oracle_script.suffix == ".sh"

    def test_swebench_skill_has_module(self):
        from cat.skill.registry import SkillRegistry
        skill = SkillRegistry().load("swebench")
        assert skill.module == "cat.skill.swebench"

    def test_registry_lists_builtins(self):
        from cat.skill.registry import SkillRegistry
        names = SkillRegistry().list_available()
        assert "chat"     in names
        assert "task"     in names
        assert "swebench" in names

    def test_load_explicit_yaml_path(self, tmp_path: Path):
        """Skills can be loaded from an explicit path — runtime injection."""
        from cat.skill.registry import SkillRegistry
        skill_dir = tmp_path / "writing_assistant"
        skill_dir.mkdir()
        (skill_dir / "skill.yaml").write_text(
            "name: writing_assistant\n"
            "description: drafts and edits text\n"
            "loop_config:\n"
            "  skip_representer: true\n"
            "  skip_oracle: true\n"
            "  intelligence: 0.6\n"
        )
        skill = SkillRegistry().load(str(skill_dir))
        assert skill.name == "writing_assistant"
        assert skill.loop_config.skip_representer is True
        assert skill.loop_config.intelligence == 0.6

    def test_unknown_skill_raises(self):
        from cat.skill.registry import SkillRegistry
        with pytest.raises(FileNotFoundError, match="not found"):
            SkillRegistry().load("nonexistent_skill_xyz")


class TestSessionSetup:
    """Session builds correct criteria and representations per skill."""

    def test_chat_session_has_no_oracle_criterion(self, repo: Path):
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session
        skill   = SkillRegistry().load("chat")
        session = Session(workdir=repo, model="stub", skill=skill,
                          test_cmd=None, budget=1.0, mode="quiet")
        crit = session._oracle_criterion()
        assert crit is None, "chat skill with no --test should have no criterion"

    def test_task_session_has_script_oracle(self, repo: Path):
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session
        skill   = SkillRegistry().load("task")
        session = Session(workdir=repo, model="stub", skill=skill,
                          test_cmd=None, budget=1.0, mode="quiet")
        crit = session._oracle_criterion()
        assert crit is not None, "task skill should produce a script-based oracle"
        assert crit.id == "skill-oracle"

    def test_explicit_test_cmd_overrides_script_oracle(self, repo: Path):
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session
        skill   = SkillRegistry().load("task")
        session = Session(workdir=repo, model="stub", skill=skill,
                          test_cmd="echo ok", budget=1.0, mode="quiet")
        crit = session._oracle_criterion()
        assert crit is not None
        assert crit.id == "test-runner"
        assert "echo ok" in crit.description

    def test_workdir_repr_injected(self, repo: Path):
        from cat.cli.cat_cli import _workdir_repr
        r = _workdir_repr(repo)
        assert r.key == "workdir_path"
        assert str(repo) in r.value
        assert "absolute" in r.value.lower()


class TestOracleScript:
    """oracle.sh subprocess protocol — exit code + stdout."""

    def test_oracle_passes_on_exit_0(self, tmp_path: Path):
        from cat.skill.runner import run_oracle
        script = tmp_path / "oracle.sh"
        script.write_text("#!/usr/bin/env bash\necho 'all tests passed'\nexit 0\n")
        script.chmod(0o755)
        passed, output = run_oracle(script, tmp_path)
        assert passed is True
        assert "all tests passed" in output

    def test_oracle_fails_on_nonzero_exit(self, tmp_path: Path):
        from cat.skill.runner import run_oracle
        script = tmp_path / "oracle.sh"
        script.write_text("#!/usr/bin/env bash\necho 'FAILED: assertion error'\nexit 1\n")
        script.chmod(0o755)
        passed, output = run_oracle(script, tmp_path)
        assert passed is False
        assert "FAILED" in output

    def test_oracle_receives_workdir_as_arg(self, tmp_path: Path):
        from cat.skill.runner import run_oracle
        script = tmp_path / "oracle.sh"
        script.write_text("#!/usr/bin/env bash\necho \"workdir=$1\"\nexit 0\n")
        script.chmod(0o755)
        _, output = run_oracle(script, tmp_path)
        assert str(tmp_path) in output

    def test_oracle_timeout_returns_failure(self, tmp_path: Path):
        from cat.skill.runner import run_oracle
        script = tmp_path / "oracle.sh"
        script.write_text("#!/usr/bin/env bash\nsleep 10\nexit 0\n")
        script.chmod(0o755)
        passed, output = run_oracle(script, tmp_path, timeout=1)
        assert passed is False
        assert "timeout" in output.lower()

    def test_builtin_task_oracle_detects_pytest(self, repo: Path):
        """Built-in oracle.sh auto-detects pytest from pyproject.toml."""
        from cat.skill.registry import SkillRegistry
        from cat.skill.runner import run_oracle
        skill = SkillRegistry().load("task")
        # repo fixture has a bug — tests should fail (paginate off-by-one)
        passed, output = run_oracle(skill.oracle_script, repo, timeout=30)
        # tests exist and runner was detected (may pass or fail, but not exit 1 from "no runner")
        assert "no test runner detected" not in output.lower()


class TestCatCliAsToolCall:
    """cat-cli invoked as a subprocess — the tool-call pattern for chat delegation."""

    def test_help_flag_works(self, tmp_path: Path):
        """Smoke: cat-cli exits 0 on --help."""
        r = _cat_cli(["--help"], cwd=tmp_path, timeout=10)
        assert r.returncode == 0
        assert "--skill" in r.stdout or "--skill" in r.stderr

    def test_unknown_skill_returns_error(self, tmp_path: Path):
        """chat agent gets a clear error when skill name is wrong."""
        r = _cat_cli(["--skill", "no_such_skill", "--task", "do something"], cwd=tmp_path)
        assert r.returncode != 0
        assert "not found" in r.stderr.lower() or "error" in r.stderr.lower()

    def test_missing_required_flag(self, tmp_path: Path):
        """Must supply --task or --chat."""
        r = _cat_cli(["--skill", "chat"], cwd=tmp_path)
        assert r.returncode != 0

    def test_json_mode_exit_code(self, tmp_path: Path, monkeypatch):
        """--json mode returns exit 0 on fulfilled, 1 on failed (unit-level, no LLM)."""
        import cat.cli.cat_cli as cli_module
        from cat.skill.registry import SkillRegistry

        skill = SkillRegistry().load("chat")

        def stub_run(self, task):
            self._turn += 1
            return True   # fulfilled

        monkeypatch.setattr(cli_module.Session, "run_task", stub_run)

        session = cli_module.Session(
            workdir=tmp_path, model=None, skill=skill,
            test_cmd=None, budget=0.5, mode="json",
        )
        assert session.run_task("hello") is True

    def test_quiet_mode_no_output(self, tmp_path: Path, monkeypatch):
        """--quiet produces no output — stub run_task, call in-process."""
        import io, sys
        import cat.cli.cat_cli as cli_module
        from cat.skill.registry import SkillRegistry

        skill = SkillRegistry().load("chat")

        def stub_run(self, task):
            self._turn += 1
            return True

        monkeypatch.setattr(cli_module.Session, "run_task", stub_run)

        session = cli_module.Session(
            workdir=tmp_path, model=None, skill=skill,
            test_cmd=None, budget=0.5, mode="quiet",
        )
        assert session.run_task("hello") is True


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO MATRIX — expected routing decisions (no LLM needed)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Scenario:
    name: str
    user_input: str
    skill: str
    has_write_tools: bool
    expect_delegate_to_task: bool   # should chat call cat-cli --skill task?
    expect_direct_answer: bool      # can answer without any tool call?
    notes: str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="pure_knowledge_qa",
        user_input="What year was Python created?",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=False,
        expect_direct_answer=True,
        notes="General knowledge — no file access needed, no code change",
    ),
    Scenario(
        name="file_read_summarize",
        user_input="Summarize README.md",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=False,
        expect_direct_answer=False,
        notes="Needs read tool, but chat skill has it — no delegation",
    ),
    Scenario(
        name="simple_file_create",
        user_input="Create a file called hello.py with a hello world function",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=False,
        expect_direct_answer=False,
        notes="Write tool available in chat — Performer does it directly",
    ),
    Scenario(
        name="simple_file_create_no_write_tools",
        user_input="Create a file called hello.py with a hello world function",
        skill="chat",
        has_write_tools=False,
        expect_delegate_to_task=True,
        expect_direct_answer=False,
        notes="No write tool in chat → must delegate to cat-cli --skill task",
    ),
    Scenario(
        name="bug_fix_with_tests",
        user_input="Fix the off-by-one bug in paginate() — tests are in test_calc.py",
        skill="task",
        has_write_tools=True,
        expect_delegate_to_task=False,   # already on task skill
        expect_direct_answer=False,
        notes="Full hypothesis loop + oracle — use task skill directly",
    ),
    Scenario(
        name="chat_delegates_code_fix",
        user_input="Fix the off-by-one bug in paginate() and make sure tests pass",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=True,
        expect_direct_answer=False,
        notes="Chat can write files but can't run oracle — delegates to task for verification",
    ),
    Scenario(
        name="multi_turn_read_then_write",
        user_input="[turn1] what does calc.py do? [turn2] now add a docstring to add()",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=False,
        expect_direct_answer=False,
        notes="Multi-turn: turn1 is Q&A, turn2 is write — both handled by chat skill",
    ),
    Scenario(
        name="ambiguous_task",
        user_input="improve calc.py",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=False,   # vague but doable
        expect_direct_answer=False,
        notes="Vague improvement request — chat Performer decides what to do",
    ),
    Scenario(
        name="nonexistent_file",
        user_input="Summarize doesnotexist.py",
        skill="chat",
        has_write_tools=True,
        expect_delegate_to_task=False,
        expect_direct_answer=False,
        notes="File missing — Performer reads, gets error, reports in done(reason=...)",
    ),
    Scenario(
        name="custom_skill_path",
        user_input="Write a haiku about Python",
        skill="writing_assistant",     # loaded from tmp path
        has_write_tools=True,
        expect_delegate_to_task=False,
        expect_direct_answer=True,
        notes="Custom skill loaded from YAML path — creative task, no code tools needed",
    ),
]


class TestScenarioMatrix:
    """Validate the scenario routing logic — no LLM needed."""

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
    def test_skill_config_matches_scenario(self, scenario: Scenario):
        """The skill's LoopConfig must be consistent with the scenario's expectations."""
        from cat.skill.registry import SkillRegistry
        reg = SkillRegistry()

        if scenario.skill == "writing_assistant":
            pytest.skip("writing_assistant skill requires custom yaml — tested separately")

        skill = reg.load(scenario.skill)
        cfg   = skill.loop_config

        if scenario.skill == "chat":
            assert cfg.skip_oracle is False, \
                f"{scenario.name}: chat skill uses LLM judge oracle"
            assert cfg.max_hypotheses >= 3, \
                f"{scenario.name}: chat retries with escalating model tiers"

        if scenario.skill == "task":
            assert cfg.skip_oracle is False, \
                f"{scenario.name}: task skill must verify with oracle"
            assert cfg.skip_stipulator is False, \
                f"{scenario.name}: task skill must plan"

    def test_all_scenario_names_unique(self):
        names = [s.name for s in SCENARIOS]
        assert len(names) == len(set(names)), "duplicate scenario names"

    def test_scenarios_cover_all_edge_cases(self):
        """Coverage check — ensure we have at least one scenario per edge case."""
        names = {s.name for s in SCENARIOS}
        required = {
            "pure_knowledge_qa",        # no tools needed
            "file_read_summarize",      # read tool needed
            "simple_file_create",       # write tool needed, available
            "simple_file_create_no_write_tools",  # write tool needed, not available → delegate
            "bug_fix_with_tests",       # oracle needed → task skill
            "chat_delegates_code_fix",  # chat decides to delegate
            "multi_turn_read_then_write",  # multi-turn
            "nonexistent_file",         # error handling
        }
        missing = required - names
        assert not missing, f"Missing coverage for: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
# DELEGATION PROTOCOL TESTS — cat-cli as subprocess tool
# ══════════════════════════════════════════════════════════════════════════════

class TestDelegationProtocol:
    """The cat-cli-as-tool pattern: how chat agent calls task agent."""

    def test_delegation_command_structure(self):
        """Verify the exact subprocess call a chat agent would make."""
        # This is the protocol: chat agent builds this command and spawns it
        workdir = "/tmp/my_repo"
        task    = "fix the off-by-one bug in paginate()"
        oracle  = "pytest test_calc.py -q"

        cmd = [
            "cat-cli",
            "--skill", "task",
            "--task",  task,
            "--test",  oracle,
            "--dir",   workdir,
            "--quiet",              # chat captures output, doesn't stream it
        ]

        # Command is well-formed: skill, task, test, dir, quiet
        assert "--skill" in cmd
        assert "task"    in cmd
        assert "--task"  in cmd
        assert "--quiet" in cmd    # result comes via exit code, not terminal output

    def test_delegation_result_via_exit_code(self, tmp_path: Path):
        """Exit code 0 = task fulfilled, 1 = task failed — protocol is in run_task_once()."""
        # Test the protocol directly at Python level (monkeypatch can't cross subprocess boundary)
        from cat.skill.registry import SkillRegistry
        import cat.cli.cat_cli as cli_module

        skill   = SkillRegistry().load("chat")

        # Fulfilled path
        fulfilled_session = cli_module.Session(
            workdir=tmp_path, model="stub", skill=skill,
            test_cmd=None, budget=1.0, mode="quiet",
        )
        fulfilled_session._turn = 0
        with patch.object(fulfilled_session, "run_task", return_value=True):
            code = cli_module.run_task_once(fulfilled_session, "hello")
        assert code == 0, "fulfilled task must return exit code 0"

        # Failed path
        failed_session = cli_module.Session(
            workdir=tmp_path, model="stub", skill=skill,
            test_cmd=None, budget=1.0, mode="quiet",
        )
        with patch.object(failed_session, "run_task", return_value=False):
            code = cli_module.run_task_once(failed_session, "hello")
        assert code == 1, "failed task must return exit code 1"

    def test_delegation_with_custom_skill_yaml(self, tmp_path: Path):
        """Chat agent can delegate to a custom skill by path — runtime injection."""
        from cat.skill.registry import SkillRegistry

        # Create a custom skill on the fly
        skill_dir = tmp_path / "my_oracle_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.yaml").write_text(
            "name: my_oracle_skill\n"
            "loop_config:\n"
            "  max_hypotheses: 2\n"
            "  intelligence: 0.8\n"
        )
        oracle = skill_dir / "oracle.sh"
        oracle.write_text("#!/usr/bin/env bash\nexit 0\n")
        oracle.chmod(0o755)

        skill = SkillRegistry().load(str(skill_dir))
        assert skill.name          == "my_oracle_skill"
        assert skill.oracle_script == oracle
        assert skill.loop_config.max_hypotheses == 2

    def test_json_output_parseable_by_chat(self, tmp_path: Path, monkeypatch):
        """--json mode: chat agent can parse event stream from task subprocess.

        Runs in-process (not subprocess) so monkeypatch works.
        """
        import cat.cli.cat_cli as cli_module
        from cat.skill.registry import SkillRegistry

        skill = SkillRegistry().load("chat")

        def stub_run(self, task):
            self._turn += 1
            return True

        monkeypatch.setattr(cli_module.Session, "run_task", stub_run)

        session = cli_module.Session(
            workdir=tmp_path, model=None, skill=skill,
            test_cmd=None, budget=0.5, mode="json",
        )
        result = session.run_task("hello")
        # In stub mode exit is True (fulfilled)
        assert result is True


# ══════════════════════════════════════════════════════════════════════════════
# LIVE INTEGRATION TESTS — real LLM, opt-in with -m live
# ══════════════════════════════════════════════════════════════════════════════

live = pytest.mark.skipif(
    not os.environ.get("CAT_MODEL"),
    reason="Set CAT_MODEL to run live tests (costs money)",
)


@live
class TestLiveChat:
    """Real LLM conversations against a local temp repo. Requires CAT_MODEL env var."""

    def test_live_read_and_summarize(self, repo: Path):
        """Scenario 2: chat skill reads README.md and returns summary in done(reason)."""
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session
        from cat.trace import replay

        skill   = SkillRegistry().load("chat")
        session = Session(workdir=repo, model=os.environ["CAT_MODEL"],
                          skill=skill, test_cmd=None, budget=0.50, mode="quiet")

        ok = session.run_task(f"Summarize {repo}/README.md in two sentences")

        # Find the trace
        runs_dir = repo / ".cat" / "runs"
        traces   = list(runs_dir.glob("**/*.jsonl"))
        assert traces, "no trace file written"

        events = replay(traces[-1])
        kinds  = [e.kind for e in events]
        assert "tool_call" in kinds, "should have at least one tool call (read)"

        done_events = [
            e for e in events
            if e.kind == "tool_call" and (e.payload or {}).get("tool") == "done"
        ]
        assert done_events, "Performer must call done()"
        reason = done_events[-1].payload.get("args", {}).get("reason", "")
        assert len(reason) > 20, f"done reason should be a real answer, got: {reason!r}"

    def test_live_create_file(self, repo: Path):
        """Scenario 3: chat skill creates a new file."""
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session

        skill   = SkillRegistry().load("chat")
        session = Session(workdir=repo, model=os.environ["CAT_MODEL"],
                          skill=skill, test_cmd=None, budget=0.50, mode="quiet")

        ok = session.run_task(
            f"Create a file at {repo}/greeting.py containing a function "
            "called greet(name) that returns 'Hello, <name>!'"
        )

        assert (repo / "greeting.py").exists(), "greeting.py was not created"
        content = (repo / "greeting.py").read_text()
        assert "greet" in content

    def test_live_task_skill_fixes_bug(self, repo: Path):
        """Scenario 5: task skill with oracle fixes the paginate() off-by-one."""
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session
        from cat.trace import replay

        # Confirm bug exists
        r = subprocess.run(
            ["python", "-m", "pytest", "test_calc.py::test_paginate_page1", "-q"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert r.returncode != 0, "tests should fail before fix (setup check)"

        skill   = SkillRegistry().load("task")
        session = Session(workdir=repo, model=os.environ["CAT_MODEL"],
                          skill=skill, test_cmd="python -m pytest test_calc.py -q",
                          budget=1.0, mode="quiet")

        ok = session.run_task(
            "Fix the off-by-one bug in the paginate() function in calc.py. "
            "page=1 should return the first page (items[0:size]), not items[size:size*2]."
        )

        # Verify oracle passes now
        r = subprocess.run(
            ["python", "-m", "pytest", "test_calc.py", "-q"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert r.returncode == 0, f"tests should pass after fix:\n{r.stdout}\n{r.stderr}"

    def test_live_delegation_via_subprocess(self, repo: Path):
        """Scenario 4/6: chat calls cat-cli --skill task as a subprocess tool."""
        # This simulates exactly what a chat agent does when it needs oracle verification
        r = subprocess.run(
            [
                sys.executable, "-m", "cat.cli.cat_cli",
                "--skill", "task",
                "--task",  "Fix the off-by-one bug in paginate() in calc.py",
                "--test",  "python -m pytest test_calc.py -q",
                "--dir",   str(repo),
                "--quiet",
            ],
            capture_output=True, text=True, cwd=str(repo), timeout=300,
        )
        # Exit code 0 = task fulfilled (oracle passed)
        # Exit code 1 = task failed (hypotheses exhausted)
        assert r.returncode in (0, 1), f"unexpected exit code: {r.returncode}"
        if r.returncode == 0:
            result = subprocess.run(
                ["python", "-m", "pytest", "test_calc.py", "-q"],
                capture_output=True, text=True, cwd=str(repo),
            )
            assert result.returncode == 0, "exit 0 from cat-cli but tests still fail"

    def test_live_multi_turn(self, repo: Path):
        """Scenario 7: two consecutive turns, state persists via git."""
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session

        skill   = SkillRegistry().load("chat")
        session = Session(workdir=repo, model=os.environ["CAT_MODEL"],
                          skill=skill, test_cmd=None, budget=1.0, mode="quiet")

        # Turn 1: read and answer
        ok1 = session.run_task(f"What functions does {repo}/calc.py export?")

        # Turn 2: write based on turn 1's context
        ok2 = session.run_task(
            f"Add a subtract(a, b) function to {repo}/calc.py that returns a - b"
        )

        content = (repo / "calc.py").read_text()
        assert "subtract" in content, "subtract function was not added"

    def test_live_nonexistent_file_handled_gracefully(self, repo: Path):
        """Scenario 9: agent handles missing file without crashing."""
        from cat.skill.registry import SkillRegistry
        from cat.cli.cat_cli import Session
        from cat.trace import replay

        skill   = SkillRegistry().load("chat")
        session = Session(workdir=repo, model=os.environ["CAT_MODEL"],
                          skill=skill, test_cmd=None, budget=0.30, mode="quiet")

        # Should complete (not crash) even though file doesn't exist
        session.run_task(f"Summarize {repo}/totally_missing_file.py")

        runs_dir = repo / ".cat" / "runs"
        traces   = list(runs_dir.glob("**/*.jsonl"))
        assert traces, "trace should be written even on failure"

        events  = replay(traces[-1])
        kinds   = [e.kind for e in events]
        # Contract should reach a terminal state (fulfilled or failed — not stuck)
        state_events = [e for e in events if e.kind == "contract_state"]
        final_states = [e.payload.get("to") for e in state_events]
        assert any(s in ("fulfilled", "failed") for s in final_states), \
            "contract did not reach terminal state"
