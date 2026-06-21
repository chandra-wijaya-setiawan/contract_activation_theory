"""cat-cli — CAT agent with profile-based loop configuration.

Usage:
    cat-cli --profile chat --chat              # interactive REPL, fast
    cat-cli --profile chat --task "summarize design.md"  # one-shot chat
    cat-cli --profile task --task "fix the pagination bug"  # full hypothesis loop
    cat-cli --profile task --chat              # interactive REPL, full loop per message

Profiles are YAML directories in (first match wins):
    ./profiles/<name>/skill.yaml
    ~/.cat/profiles/<name>/skill.yaml
    cat/agent/profiles/<name>/skill.yaml

Or point directly:
    cat-cli --profile ./my_profiles/writing_assistant

Architecture:
  Pi InteractiveMode pattern: Recorder.append() fires on every trace event
  → StreamingRecorder renders each event immediately (no buffering).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from cat.contract.model import (
    Contract, Constraints, ContractState,
    Criterion, Evidence, Representation, State,
)
from cat.llm.chat_judge import call_chat_judge
from cat.capability.full_registry import build_default_registry, register_subagent_tool
from cat.capability.git_checkpoint import GitCheckpointer
from cat.agent.definition import AgentDef
from cat.agent.registry import AgentRegistry
from cat.agent.runner import run_oracle, run_teardown
from cat.trace import Recorder, TraceEvent
from cat.llm.client import LLM
from cat.agent.hypothesis import Activator

# ── ANSI colour helpers ───────────────────────────────────────────────────────

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_GREY   = "\033[90m"
_ERASE  = "\033[2K"   # erase entire current line

_CAT_ICON = "😸"      # shown in REPL prompt
_SPINNER  = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

console = Console(stderr=True)
_md_console = Console()   # stdout — used to render Markdown responses


# ── Spinner ───────────────────────────────────────────────────────────────────

class Spinner:
    """Animated braille spinner on stderr with elapsed-seconds counter.

    Writes on the *current* line using \\r — no \\n, no cursor-up tricks.
    All content output must go to stderr too so the single stream's cursor
    position stays authoritative.
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, label: str = "working") -> None:
        self.stop()
        if not self._enabled:
            return
        self._stop.clear()
        t0 = time.time()
        label_snap = label
        def _run() -> None:
            i = 0
            while True:
                ch = _SPINNER[i % len(_SPINNER)]
                elapsed = int(time.time() - t0)
                suffix = f"  {_DIM}{elapsed}s{_RESET}" if elapsed >= 2 else ""
                # \r → go to start of current line, overwrite in place
                sys.stderr.write(f"\r   {_DIM}{ch}  {label_snap}…{_RESET}{suffix}   ")
                sys.stderr.flush()
                i += 1
                if self._stop.wait(0.1):
                    break
            sys.stderr.write(f"\r{_ERASE}")   # erase spinner line, stay on same line
            sys.stderr.flush()
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.3)
        self._thread = None


# ── Streaming recorder ────────────────────────────────────────────────────────

class StreamingRecorder(Recorder):
    """Renders trace events to terminal as they fire."""

    def __init__(self, mode: str, *args, spinner: Optional[Spinner] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._mode     = mode
        self._start_ts = time.time()
        self._cost_usd = 0.0
        self._current_h = 0
        self._spinner  = spinner or Spinner(enabled=(mode == "stream"))

    def append(self, event: TraceEvent) -> None:
        super().append(event)
        if self._mode == "quiet":
            return
        if self._mode == "json":
            print(event.model_dump_json(), flush=True)
            return
        self._spinner.stop()
        self._render(event)

    def _print(self, s: str, flush: bool = True) -> None:
        """Write a line to stderr — same stream as the spinner."""
        sys.stderr.write(s + "\n")
        if flush:
            sys.stderr.flush()

    def _render(self, event: TraceEvent) -> None:
        kind = event.kind
        p = event.payload or {}

        if kind == "contract_represent":
            self._print(f"   {_DIM}⟳  planning…{_RESET}")
            self._spinner.start("planning")

        elif kind == "hypothesis_attempt":
            n     = p.get("n", "?")
            self._current_h = n
            start = p.get("start_from_requested", "base")
            model = p.get("execution_model", "")
            tier  = f"  {_DIM}{model}{_RESET}" if model else ""
            self._print(f"\n{_BOLD}{_CYAN}◈  Hypothesis {n}{_RESET} [{start}]{tier}")
            loc  = (p.get("location") or "").strip()
            chg  = (p.get("change")   or "").strip()
            pred = (p.get("predicts") or "").strip()
            basis = (p.get("basis")   or "").strip()
            if loc and loc != "*":
                self._print(f"   {_DIM}target  {_RESET}{loc}")
            if chg:
                self._print(f"   {_DIM}plan    {_RESET}{chg}")
            if pred:
                self._print(f"   {_DIM}expects {_RESET}{pred}")
            if basis:
                self._print(f"   {_DIM}basis   {_RESET}{basis}")
            self._spinner.start("generating")

        elif kind == "performer_response":
            text = (p.get("text") or "").strip()
            if text and len(text) > 60:
                self._print("")
                for line in text.splitlines():
                    self._print(f"  {_DIM}{line}{_RESET}")
                self._print("")

        elif kind == "tool_call":
            tool = p.get("tool", "")
            args = p.get("args", {})
            if tool == "bash":
                cmd = (args.get("command") or "")[:70].replace("\n", " ")
                self._print(f"   {_GREY}▸ bash{_RESET}  {cmd}")
            elif tool in ("str_replace", "multi_edit", "write", "apply_patch"):
                path  = args.get("path") or args.get("source_path") or ""
                extra = f"  ({len(args['edits'])} edits)" if tool == "multi_edit" and "edits" in args else ""
                self._print(f"   {_YELLOW}✎ {tool}{_RESET}  {path}{extra}")
            elif tool in ("read", "outline", "git_diff", "git_status", "rg", "fd"):
                path = args.get("path") or args.get("file_path") or args.get("pattern") or args.get("name") or ""
                self._print(f"   {_GREY}▸ {tool}{_RESET}  {str(path)[:70]}")
            elif tool == "done":
                reason = (args.get("reason") or "").strip()
                if reason:
                    self._print("")
                    console.print(Markdown(reason))
                    self._print("")
            elif tool == "give_up":
                reason = (args.get("reason") or "").strip()
                if reason:
                    self._print(f"   {_RED}↺  give_up{_RESET} — {reason[:120]}")
            elif tool in ("web_browse", "web_search"):
                q = (args.get("url") or args.get("query") or "")[:70]
                verb = "browse" if tool == "web_browse" else "search"
                self._print(f"   {_GREY}▸ {verb}{_RESET}  {q}")
            _TOOL_SPIN = {
                "bash": "running", "rg": "searching", "fd": "searching",
                "read": "reading", "outline": "reading",
                "git_diff": "diffing", "git_status": "diffing",
                "str_replace": "editing", "multi_edit": "editing",
                "write": "writing", "apply_patch": "patching",
                "web_browse": "browsing", "web_search": "searching",
                "done": None, "give_up": None,
            }
            label = _TOOL_SPIN.get(tool, "reasoning")
            if label:
                self._spinner.start(label)

        elif kind == "tool_result":
            if p.get("is_error"):
                out = (p.get("output") or "")[:80].replace("\n", " ")
                self._print(f"   {_RED}✗ error{_RESET}  {out}")
            # LLM is about to decide its next action — keep spinner going
            if p.get("tool") not in ("done", "give_up"):
                self._spinner.start("reasoning")

        elif kind == "criterion_check":
            sat  = p.get("satisfied")
            ev   = (p.get("evidence") or "").replace("\n", " ").strip()
            mark = f"{_GREEN}✓" if sat else f"{_RED}✗"
            self._print(f"   {mark} oracle{_RESET}  {ev}")

        elif kind == "knowledge_update":
            verdict = p.get("verdict", "")
            reason = (p.get("reason") or "").strip()
            if verdict in ("completed", "exhausted") and reason:
                self._print("")
                console.print(Markdown(reason))
                self._print("")
            elif verdict == "give_up" and reason:
                self._print(f"   {_RED}↺  give_up{_RESET} — {reason[:120]}")

        elif kind == "llm_usage":
            self._cost_usd += float(p.get("usd", 0))

        elif kind == "contract_state":
            to      = p.get("to", "")
            reason  = (p.get("reason") or "")[:80]
            elapsed = time.time() - self._start_ts
            if to == "fulfilled":
                self._print(f"\n{_GREEN}{_BOLD}✅  done{_RESET}  "
                             f"({self._current_h} hypothesis · {elapsed:.0f}s · ${self._cost_usd:.4f})")
            elif to == "failed":
                self._print(f"\n{_RED}{_BOLD}✗  failed{_RESET}  {reason}  "
                             f"({elapsed:.0f}s · ${self._cost_usd:.4f})")


# ── Infrastructure helpers ────────────────────────────────────────────────────

def _local_exec(argv: list[str], workdir: str) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, cwd=workdir, timeout=60)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as exc:
        return 1, str(exc)


def _make_local_checkpointer(workdir: Path) -> GitCheckpointer:
    return GitCheckpointer(
        exec_fn=lambda argv, wd=None: _local_exec(argv, str(wd or workdir)),
        workdir=str(workdir),
    )


def _make_llm_judge_criterion(obligation: str, llm) -> Criterion:
    """Generic LLM-judge oracle for chat / no-shell-oracle sessions.

    Closes over obligation and llm; reads state.last_response at check time.
    """
    def predicate(state: State) -> tuple[bool, Evidence]:
        response = state.last_response
        if not response:
            return False, Evidence(
                summary="chat_judge: no response captured",
                detail={"verdict": "retry", "reason": "performer did not call done()"},
            )
        schema, _ = call_chat_judge(obligation, response, llm)
        ok = schema.verdict == "accept"
        return ok, Evidence(
            summary=f"chat_judge: {schema.verdict} — {schema.reason[:120]}",
            detail={"verdict": schema.verdict, "reason": schema.reason},
        )

    return Criterion(
        id="chat-quality",
        description="LLM judge: does the response adequately answer the user's request?",
        predicate=predicate,
        is_oracle=True,
    )


def _make_script_criterion(skill: AgentDef, workdir: Path) -> Optional[Criterion]:
    """Build a Criterion from skill's oracle.sh — or None if no script exists."""
    if not skill.oracle_script or not skill.oracle_script.exists():
        return None

    script = skill.oracle_script

    def predicate(state: State) -> tuple[bool, Evidence]:
        passed, output = run_oracle(script, state.workdir)
        return passed, Evidence(
            summary=f"oracle.sh exit={'0' if passed else 'non-zero'} {'PASS' if passed else 'FAIL'}",
            detail={"passed": passed, "output": output, "script": str(script)},
        )

    return Criterion(
        id="skill-oracle",
        description=f"oracle.sh: {script.name}",
        predicate=predicate,
        is_oracle=True,
    )


def _make_cmd_criterion(test_cmd: str, workdir: Path) -> Criterion:
    """Build a Criterion from an explicit --test command string."""
    def predicate(state: State) -> tuple[bool, Evidence]:
        try:
            r = subprocess.run(
                ["bash", "-c", test_cmd], capture_output=True, text=True,
                cwd=str(state.workdir), timeout=120,
            )
            output = (r.stdout + r.stderr)[-3000:]
            passed = (r.returncode == 0)
            return passed, Evidence(
                summary=f"test exit_code={r.returncode} {'PASS' if passed else 'FAIL'}",
                detail={"exit_code": r.returncode, "output": output, "cmd": test_cmd},
            )
        except subprocess.TimeoutExpired:
            return False, Evidence(summary="test timeout", detail={})
        except Exception as exc:
            return False, Evidence(summary=f"oracle error: {exc}", detail={})

    return Criterion(
        id="test-runner",
        description=f"exit_code==0 for: {test_cmd}",
        predicate=predicate,
        is_oracle=True,
    )


def _diff_stat(workdir: Path, base_sha: Optional[str] = None) -> str:
    try:
        ref = base_sha or "HEAD"
        r = subprocess.run(
            ["git", "diff", "--stat", ref],
            capture_output=True, text=True, cwd=str(workdir),
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _pre_sha(workdir: Path) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "stash", "create"], capture_output=True, text=True, cwd=str(workdir),
        )
        sha = r.stdout.strip()
        if sha:
            return sha
        r2 = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(workdir),
        )
        return r2.stdout.strip() or None
    except Exception:
        return None


def _git_log(workdir: Path, n: int = 5) -> str:
    try:
        r = subprocess.run(
            ["git", "log", f"-{n}", "--oneline"],
            capture_output=True, text=True, cwd=str(workdir),
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _run_workspace_probes(skill: "AgentDef", workdir: Path) -> list:
    """Run representer.probes once at session start — not per turn.

    Returns a list of Representation objects with probe outputs as evidence.
    The Representer LLM call receives these as static_reps context.
    """
    probes = getattr(skill, "representer_probes", []) or []
    if not probes:
        return []
    reps = []
    for probe in probes:
        key = probe.get("key", "")
        cmd = probe.get("command", "")
        if not key or not cmd:
            continue
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                cwd=str(workdir), timeout=15,
            )
            output = (r.stdout + r.stderr).strip()
        except Exception:
            output = ""
        reps.append(Representation(key=key, value=output[:120], evidence=output))
    return reps


def _workdir_repr(workdir: Path) -> Representation:
    """Inject absolute workdir so the Performer uses correct paths."""
    return Representation(
        key="workdir_path",
        value=f"Working directory (absolute): {workdir}\nAlways use absolute paths when reading or editing files.",
        evidence="injected by cat-cli",
    )


# ── Session ───────────────────────────────────────────────────────────────────

class Session:
    """Shared infrastructure for the lifetime of a chat/one-shot session."""

    def __init__(
        self,
        workdir: Path,
        model: Optional[str],
        skill: AgentDef,
        test_cmd: Optional[str],
        budget: float,
        mode: str,
    ):
        self.workdir  = workdir
        self.skill    = skill

        # Resolve model: --model override → skill's model_array[0] → fallback.
        arr = list(skill.loop_config.model_array) or ["openrouter/deepseek/deepseek-v4-flash"]
        if model:
            arr[0] = model                         # power-user override: replace T0
            skill.loop_config.model_array = arr
        self.model = arr[0]
        self.test_cmd = test_cmd
        self.budget   = budget
        self.mode     = mode
        self.registry    = build_default_registry()
        self.checkpointer = _make_local_checkpointer(workdir)
        self._turn    = 0
        self._history: list[dict] = []  # [{user, assistant}, ...]
        self._spinner = Spinner(enabled=(mode == "stream"))
        # Run workspace probes once at session start; injected as static_reps each turn.
        self._workspace_reps: list[Representation] = _run_workspace_probes(skill, workdir)

    def _oracle_criterion(self) -> Optional[Criterion]:
        if self.skill.loop_config.skip_oracle:
            return None
        if self.test_cmd:
            return _make_cmd_criterion(self.test_cmd, self.workdir)
        return _make_script_criterion(self.skill, self.workdir)

    def run_task(self, task: str) -> bool:
        self._turn += 1
        self._spinner.start("understanding")   # animate before first trace event fires
        run_id  = f"cat-cli-{uuid.uuid4().hex[:8]}"
        out_dir = self.workdir / ".cat" / "runs" / run_id

        before_sha = _pre_sha(self.workdir)

        recorder = StreamingRecorder(
            mode=self.mode,
            run_id=run_id,
            task_id=f"turn-{self._turn}",
            agent="cat-cli",
            model=self.model,
            out_dir=out_dir,
            spinner=self._spinner,   # shared — recorder.stop() clears the pre-spinner too
        )

        llm = LLM(model=self.model, recorder=recorder)

        oracle_crit = self._oracle_criterion()
        if oracle_crit is not None:
            criteria = [oracle_crit]
        elif not self.skill.loop_config.skip_oracle:
            # No shell oracle but oracle enabled → inject generic LLM judge.
            criteria = [_make_llm_judge_criterion(task, llm)]
        else:
            criteria = []

        contract = Contract.new(
            obligation=task,
            criteria=criteria,
            constraints=Constraints(max_usd=self.budget, max_wall_s=600.0),
        )
        contract.raw_request = task
        contract.representations = [_workdir_repr(self.workdir)] + self._workspace_reps

        # Option B: real message pairs — prepended to Performer inner_messages
        # so prefix is stable across turns → KV cache reuse.
        if self._history:
            history_msgs: list[dict] = []
            for h in self._history[-6:]:   # keep last 6 turns
                history_msgs.append({"role": "user",      "content": h["user"]})
                history_msgs.append({"role": "assistant", "content": h["assistant"]})
            contract.chat_history = history_msgs

        state = State(workdir=self.workdir)

        # Register spawn_subagent so the Performer can delegate to sub-agents.
        # Done here (not in __init__) because we need the live recorder + llm.
        from cat.agent.registry import AgentRegistry as _AgentReg
        _sub_agent_reg = _AgentReg()
        register_subagent_tool(
            registry=self.registry,
            agent_registry=_sub_agent_reg,
            capability_registry=self.registry,
            recorder=recorder,
            llm=llm,
            checkpointer=self.checkpointer,
        )

        activator = Activator(
            llm=llm,
            registry=self.registry,
            recorder=recorder,
            checkpointer=self.checkpointer,
            skill=self.skill,       # ← LoopConfig gates applied here
        )
        activator.run(contract, state)

        # Capture assistant response for next turn's history.
        # Include give_up turns too — without them the next turn loses all context
        # of what was tried and why it failed, leading to hallucinated explanations.
        response = ""
        for ev in reversed(recorder.traj.events):
            if ev.kind == "knowledge_update":
                p = ev.payload or {}
                if p.get("verdict") in ("completed", "give_up"):
                    response = (p.get("reason") or "").strip()
                break
        if response:
            self._history.append({"user": task, "assistant": response})

        run_teardown(self.skill.teardown_script, self.workdir)

        stat = _diff_stat(self.workdir, base_sha=before_sha)
        if stat and self.mode == "stream":
            print()
            for line in stat.splitlines():
                print(f"  {_DIM}{line}{_RESET}")

        if self.mode == "stream":
            print(f"  {_DIM}trace: {recorder.jsonl_path}{_RESET}")

        recorder.finalize()
        return contract.state == ContractState.FULFILLED


# ── Chat REPL ─────────────────────────────────────────────────────────────────

_CHAT_HELP = """\
Commands:
  /diff    show git diff --stat since last task
  /log     show recent git log
  /skills  list available skills
  /quit    exit  (also Ctrl-C or Ctrl-D)
  /help    this message
"""

_CAT_ART = "  /\\_/\\\n=( ^.^ )=\n  )   ("

def _chat_welcome(workdir: Path, skill: AgentDef) -> None:
    cwd_str = str(workdir)
    if cwd_str.startswith(str(Path.home())):
        cwd_str = "~" + cwd_str[len(str(Path.home())):]
    cfg = skill.loop_config
    flags = []
    if cfg.skip_representer: flags.append("no-probes")
    if cfg.skip_stipulator:  flags.append("no-plan")
    if cfg.skip_oracle:      flags.append("no-oracle")
    flag_str = "  ·  " + "  ".join(flags) if flags else ""
    model_short = (cfg.model_array[0].split("/")[-1] if cfg.model_array else "").split("-")[0:2]
    model_str = "-".join(model_short) if model_short else ""
    console.print(Panel.fit(
        f"[bold]{_CAT_ART}[/bold]\n"
        f"[bold]CAT[/bold] · {skill.name}  "
        f"{skill.description}\n"
        f"{cwd_str}{flag_str}  "
        f"{model_str}\n"
        f"/help for commands · Ctrl-D to quit",
        style="black on bright_green",
        border_style="black on bright_green",
        padding=(0, 1),
    ))


def chat(session: Session) -> None:
    _chat_welcome(session.workdir, session.skill)
    print()

    last_sha: Optional[str] = None

    while True:
        try:
            task = Prompt.ask(f"{_CAT_ICON}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_DIM}see you later 😺{_RESET}")
            break

        if not task:
            continue

        if task.startswith("/quit") or task.startswith("/exit") or task in ("quit", "exit"):
            print(f"{_DIM}see you later 😺{_RESET}")
            break
        elif task == "/help":
            print(_CHAT_HELP)
            continue
        elif task == "/diff":
            stat = _diff_stat(session.workdir, base_sha=last_sha)
            print(stat if stat else f"{_DIM}(no changes){_RESET}")
            print()
            continue
        elif task == "/log":
            log = _git_log(session.workdir)
            print(log if log else f"{_DIM}(no commits){_RESET}")
            print()
            continue
        elif task == "/skills":
            names = AgentRegistry().list_available()
            print("  " + "  ".join(names) if names else "(none found)")
            print()
            continue

        last_sha = _pre_sha(session.workdir)
        session.run_task(task)
        sys.stderr.write("\n")
        sys.stderr.flush()


# ── One-shot ──────────────────────────────────────────────────────────────────

def run_task_once(session: Session, task: str) -> int:
    cwd_str = str(session.workdir)
    if cwd_str.startswith(str(Path.home())):
        cwd_str = "~" + cwd_str[len(str(Path.home())):]

    if session.mode == "stream":
        console.print(Panel.fit(
            f"[bold]{task}[/bold]\n"
            f"[dim]{cwd_str}[/dim]  [dim]·  skill: {session.skill.name}[/dim]",
            title="[bold cyan]CAT[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))

    ok = session.run_task(task)
    return 0 if ok else 1


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    reg = AgentRegistry()
    available = ", ".join(reg.list_available()) or "chat, task"

    parser = argparse.ArgumentParser(
        prog="cat-cli",
        description=f"CAT agent. --profile selects loop behaviour. Available: {available}",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--task", metavar="TASK",  help="One-shot task")
    mode_group.add_argument("--chat", action="store_true", help="Interactive REPL")

    parser.add_argument("--profile",  metavar="NAME",  default="chat",
                        help="Skill name or path to skill directory/yaml (default: chat)")
    parser.add_argument("--test",   metavar="CMD",   help="Oracle command override, e.g. 'pytest tests/'")
    parser.add_argument("--dir",    metavar="PATH",   default=".", help="Working directory (default: .)")
    parser.add_argument("--model",  metavar="MODEL",  default=None,
                        help="Override model (default: from skill's model_array[0])")
    parser.add_argument("--budget", metavar="USD",    type=float, default=2.0)
    parser.add_argument("--quiet",  action="store_true")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    workdir = Path(args.dir).resolve()
    if not workdir.exists():
        print(f"error: directory not found: {workdir}", file=sys.stderr)
        return 1

    try:
        skill = reg.load(args.profile)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "quiet" if args.quiet else ("json" if args.json else "stream")

    session = Session(
        workdir=workdir,
        model=args.model,
        skill=skill,
        test_cmd=args.test,
        budget=args.budget,
        mode=mode,
    )

    if args.chat:
        chat(session)
        return 0
    else:
        return run_task_once(session, args.task)


if __name__ == "__main__":
    sys.exit(main())
