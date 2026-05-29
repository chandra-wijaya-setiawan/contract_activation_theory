"""Run metrics for CAT and ReAct agents.

Field names align with OTel GenAI semantic conventions where applicable:
  gen_ai.usage.input_tokens          → tokens_in
  gen_ai.usage.output_tokens         → tokens_out
  gen_ai.usage.cache_creation.*      → tokens_cache_write
  gen_ai.usage.cache_read.*          → tokens_cache_read
  gen_ai.client.operation.duration   → wall_s
  gen_ai.response.time_to_first_chunk → ttft_s  (proxy: time to first tool call)
  gen_ai.tool.*                      → tool_calls_*

Beyond the OTel baseline we add:
  - LLM call breakdown by role (planner / executor / judge / react)
  - Token breakdown by role (where the tokens went)
  - CAT-specific: criterion checks, judge overhead, plan adherence,
    contract state, budget utilization per constraint

Usage:
    m = RunMetrics.from_jsonl("path/to/trace.jsonl")
    print(m.summary())

    runs = [RunMetrics.from_jsonl(p) for p in paths]
    print(AggregateMetrics.from_runs(runs).table())

    print(compare(cat_run, react_run))
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# RunMetrics
# ---------------------------------------------------------------------------

@dataclass
class RunMetrics:
    """All observable signals for a single agent run.

    Built from a JSONL trace file via RunMetrics.from_jsonl().
    All fields have sensible zero defaults so partial traces don't crash.
    """

    # ── Identity ──────────────────────────────────────────────────────────
    run_id: str = ""
    task_id: str = ""
    agent: str = ""
    model: str = ""

    # ── Outcome ───────────────────────────────────────────────────────────
    solved: bool = False
    verdict: str = ""               # solved / failed / timeout / over_budget
    failure_reason: Optional[str] = None
    contract_final_state: Optional[str] = None  # fulfilled/failed/expired (CAT)

    # ── Loop / iteration counts ───────────────────────────────────────────
    # OTel: gen_ai.agent loop iterations (tracked via capability_activate events)
    iterations: int = 0             # capability activations (CAT) or tool calls (ReAct)

    # ── Tool calls ────────────────────────────────────────────────────────
    # OTel: gen_ai.tool.*
    tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0            # calls where is_error=True
    tool_retries: int = 0           # tool calls attempted more than once on same step
    tool_wall_s: float = 0.0        # cumulative time in tool execution

    # ── LLM calls ─────────────────────────────────────────────────────────
    # OTel: gen_ai.client.operation; broken down by role (our extension)
    llm_calls: int = 0
    llm_calls_by_role: dict[str, int] = field(default_factory=dict)
    llm_errors: int = 0             # API errors / parse failures

    # ── Token counts ──────────────────────────────────────────────────────
    # OTel: gen_ai.usage.input_tokens / output_tokens
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_in_by_role: dict[str, int] = field(default_factory=dict)
    tokens_out_by_role: dict[str, int] = field(default_factory=dict)
    # OTel: gen_ai.usage.cache_creation.input_tokens / cache_read.input_tokens
    tokens_cache_write: int = 0     # tokens that populated the cache (cache_creation)
    tokens_cache_read: int = 0      # tokens served from cache (cheaper)

    # ── Cost (USD) ────────────────────────────────────────────────────────
    usd: float = 0.0
    usd_by_role: dict[str, float] = field(default_factory=dict)

    # ── Latency ───────────────────────────────────────────────────────────
    # OTel: gen_ai.client.operation.duration
    wall_s: float = 0.0
    wall_s_llm: float = 0.0         # cumulative time in LLM API calls
    wall_s_tools: float = 0.0       # cumulative time in tool execution
    # OTel: gen_ai.response.time_to_first_chunk (proxy: time to first tool call)
    ttft_s: float = 0.0             # wall time until first tool was invoked

    # ── Context window pressure ───────────────────────────────────────────
    # OTel: gen_ai.request.max_tokens (we record actual max observed)
    max_prompt_tokens: int = 0      # largest single prompt (context pressure indicator)
    messages_total: int = 0         # total messages in conversation thread

    # ── CAT-specific ──────────────────────────────────────────────────────
    criterion_checks: int = 0           # times _verify_criteria ran
    criterion_first_satisfied: Optional[int] = None  # iteration when criterion first True
    judge_calls: int = 0                # LLM calls with role="judge"
    plan_steps_total: int = 0           # steps emitted by planner
    plan_steps_completed: int = 0       # steps judged complete
    budget_tokens_used_pct: float = 0.0  # tokens_total / max_tokens_total
    budget_tool_calls_used_pct: float = 0.0  # tool_calls / max_tool_calls

    # ── Derived properties ────────────────────────────────────────────────

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def tool_success_rate(self) -> float:
        """Fraction of tool calls that returned without error."""
        if self.tool_calls == 0:
            return 1.0
        return (self.tool_calls - self.tool_errors) / self.tool_calls

    @property
    def llm_calls_per_tool(self) -> float:
        """LLM calls per tool call. ReAct ~1; CAT with LLM judge ~4."""
        if self.tool_calls == 0:
            return float("inf")
        return self.llm_calls / self.tool_calls

    @property
    def usd_per_tool_call(self) -> float:
        """Average cost per capability invocation."""
        if self.tool_calls == 0:
            return float("inf")
        return self.usd / self.tool_calls

    @property
    def judge_overhead_fraction(self) -> float:
        """Fraction of input tokens consumed by judge calls (0–1). CAT-only signal."""
        judge_in = self.tokens_in_by_role.get("judge", 0)
        if self.tokens_in == 0:
            return 0.0
        return judge_in / self.tokens_in

    @property
    def plan_completion_rate(self) -> float:
        """Fraction of planned steps completed. 1.0 = all steps done."""
        if self.plan_steps_total == 0:
            return 0.0
        return self.plan_steps_completed / self.plan_steps_total

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of input tokens served from cache (reduces cost)."""
        total = self.tokens_in + self.tokens_cache_read
        if total == 0:
            return 0.0
        return self.tokens_cache_read / total

    # ── Builders ──────────────────────────────────────────────────────────

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "RunMetrics":
        """Build a RunMetrics from a CAT/ReAct trace JSONL file."""
        events: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return _build_from_events(events)

    # ── Display ───────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Single-run human-readable summary."""
        lines = [
            f"run_id       : {self.run_id}",
            f"agent        : {self.agent}  model={self.model}",
            f"task         : {self.task_id}",
            f"verdict      : {self.verdict}  solved={self.solved}",
            f"",
            f"── Loop ──────────────────────────────",
            f"iterations   : {self.iterations}",
            f"tool_calls   : {self.tool_calls}  errors={self.tool_errors}"
            f"  success_rate={self.tool_success_rate:.0%}",
            f"tool_by_name : {dict(sorted(self.tool_calls_by_name.items()))}",
            f"",
            f"── LLM calls ─────────────────────────",
            f"llm_calls    : {self.llm_calls}  ({self.llm_calls_per_tool:.1f} per tool call)",
            f"by_role      : {dict(sorted(self.llm_calls_by_role.items()))}",
            f"",
            f"── Tokens (OTel gen_ai.usage.*) ──────",
            f"tokens_in    : {self.tokens_in:,}",
            f"tokens_out   : {self.tokens_out:,}",
            f"tokens_total : {self.tokens_total:,}",
            f"cache_write  : {self.tokens_cache_write:,}",
            f"cache_read   : {self.tokens_cache_read:,}  hit_rate={self.cache_hit_rate:.0%}",
            f"in_by_role   : {dict(sorted(self.tokens_in_by_role.items()))}",
            f"",
            f"── Cost ──────────────────────────────",
            f"usd          : ${self.usd:.5f}",
            f"usd/tool_call: ${self.usd_per_tool_call:.5f}",
            f"usd_by_role  : {{{', '.join(f'{k}: ${v:.5f}' for k, v in sorted(self.usd_by_role.items()))}}}",
            f"",
            f"── Latency (OTel gen_ai.client.*) ────",
            f"wall_s       : {self.wall_s:.1f}s",
            f"wall_s_llm   : {self.wall_s_llm:.1f}s",
            f"wall_s_tools : {self.wall_s_tools:.1f}s",
            f"ttft_s       : {self.ttft_s:.1f}s  (time to first tool call)",
            f"max_prompt   : {self.max_prompt_tokens:,} tokens",
        ]
        if self.plan_steps_total > 0:
            lines += [
                f"",
                f"── CAT plan / criterion ──────────────",
                f"plan_steps   : {self.plan_steps_completed}/{self.plan_steps_total}"
                f"  ({self.plan_completion_rate:.0%})",
                f"criterion_checks: {self.criterion_checks}",
                f"judge_calls  : {self.judge_calls}",
                f"judge_overhead: {self.judge_overhead_fraction:.0%} of input tokens",
                f"budget_tokens: {self.budget_tokens_used_pct:.0%}",
                f"budget_tools : {self.budget_tool_calls_used_pct:.0%}",
            ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# AggregateMetrics — summary across multiple runs
# ---------------------------------------------------------------------------

@dataclass
class AggregateMetrics:
    """Statistics across a list of RunMetrics (same agent + task)."""

    agent: str = ""
    task_id: str = ""
    n: int = 0
    solved: int = 0

    tool_calls_mean: float = 0.0
    llm_calls_mean: float = 0.0
    judge_calls_mean: float = 0.0
    tokens_in_mean: float = 0.0
    tokens_out_mean: float = 0.0
    usd_mean: float = 0.0
    wall_s_mean: float = 0.0
    tool_success_rate_mean: float = 0.0
    llm_per_tool_mean: float = 0.0
    judge_overhead_mean: float = 0.0  # fraction
    plan_completion_mean: float = 0.0

    @classmethod
    def from_runs(cls, runs: list[RunMetrics]) -> "AggregateMetrics":
        if not runs:
            return cls()
        n = len(runs)

        def mean(vals: list) -> float:
            finite = [v for v in vals if v != float("inf")]
            return sum(finite) / len(finite) if finite else 0.0

        return cls(
            agent=runs[0].agent,
            task_id=runs[0].task_id,
            n=n,
            solved=sum(1 for r in runs if r.solved),
            tool_calls_mean=mean([r.tool_calls for r in runs]),
            llm_calls_mean=mean([r.llm_calls for r in runs]),
            judge_calls_mean=mean([r.judge_calls for r in runs]),
            tokens_in_mean=mean([r.tokens_in for r in runs]),
            tokens_out_mean=mean([r.tokens_out for r in runs]),
            usd_mean=mean([r.usd for r in runs]),
            wall_s_mean=mean([r.wall_s for r in runs]),
            tool_success_rate_mean=mean([r.tool_success_rate for r in runs]),
            llm_per_tool_mean=mean([r.llm_calls_per_tool for r in runs]),
            judge_overhead_mean=mean([r.judge_overhead_fraction for r in runs]),
            plan_completion_mean=mean([r.plan_completion_rate for r in runs]),
        )

    @property
    def solve_rate(self) -> float:
        return self.solved / self.n if self.n else 0.0


# ---------------------------------------------------------------------------
# compare() — side-by-side two aggregate metric sets
# ---------------------------------------------------------------------------

def compare(a: AggregateMetrics, b: AggregateMetrics, label_a: str = "", label_b: str = "") -> str:
    """Markdown table comparing two AggregateMetrics objects."""
    la = label_a or a.agent
    lb = label_b or b.agent

    def ratio(va: float, vb: float) -> str:
        if vb == 0:
            return "—"
        r = va / vb
        return f"{r:.2f}×"

    rows = [
        ("solve rate",      f"{a.solve_rate:.0%}",       f"{b.solve_rate:.0%}",          "—"),
        ("tool_calls μ",    f"{a.tool_calls_mean:.1f}",  f"{b.tool_calls_mean:.1f}",     ratio(a.tool_calls_mean, b.tool_calls_mean)),
        ("llm_calls μ",     f"{a.llm_calls_mean:.1f}",   f"{b.llm_calls_mean:.1f}",      ratio(a.llm_calls_mean, b.llm_calls_mean)),
        ("judge_calls μ",   f"{a.judge_calls_mean:.1f}", f"{b.judge_calls_mean:.1f}",    ratio(a.judge_calls_mean, b.judge_calls_mean)),
        ("llm/tool μ",      f"{a.llm_per_tool_mean:.1f}",f"{b.llm_per_tool_mean:.1f}",  ratio(a.llm_per_tool_mean, b.llm_per_tool_mean)),
        ("tokens_in μ",     f"{a.tokens_in_mean:,.0f}",  f"{b.tokens_in_mean:,.0f}",     ratio(a.tokens_in_mean, b.tokens_in_mean)),
        ("tokens_out μ",    f"{a.tokens_out_mean:,.0f}", f"{b.tokens_out_mean:,.0f}",    ratio(a.tokens_out_mean, b.tokens_out_mean)),
        ("usd μ",           f"${a.usd_mean:.5f}",        f"${b.usd_mean:.5f}",           ratio(a.usd_mean, b.usd_mean)),
        ("usd/tool μ",      f"${a.usd_mean/max(a.tool_calls_mean,1e-9):.5f}",
                             f"${b.usd_mean/max(b.tool_calls_mean,1e-9):.5f}",           "—"),
        ("wall_s μ",        f"{a.wall_s_mean:.1f}s",     f"{b.wall_s_mean:.1f}s",        ratio(a.wall_s_mean, b.wall_s_mean)),
        ("tool_success μ",  f"{a.tool_success_rate_mean:.0%}", f"{b.tool_success_rate_mean:.0%}", "—"),
        ("judge_overhead μ",f"{a.judge_overhead_mean:.0%}",f"{b.judge_overhead_mean:.0%}","—"),
        ("plan_complete μ", f"{a.plan_completion_mean:.0%}",f"{b.plan_completion_mean:.0%}","—"),
    ]

    header = f"| metric | {la} | {lb} | ratio ({la}/{lb}) |"
    sep    = "|---|---:|---:|---:|"
    body   = "\n".join(f"| {m} | {va} | {vb} | {r} |" for m, va, vb, r in rows)
    return f"{header}\n{sep}\n{body}"


# ---------------------------------------------------------------------------
# summary_table() — many runs → markdown
# ---------------------------------------------------------------------------

def summary_table(runs: list[RunMetrics]) -> str:
    """One-row-per-run Markdown table for all runs."""
    header = (
        "| run_id | agent | task | solved | tool_calls | llm_calls | "
        "judge_calls | tokens_in | usd | wall_s | llm/tool | judge_overhead |"
    )
    sep = "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    rows = []
    for r in runs:
        rows.append(
            f"| {r.run_id[:12]} | {r.agent} | {r.task_id} | {'✓' if r.solved else '✗'} "
            f"| {r.tool_calls} | {r.llm_calls} | {r.judge_calls} "
            f"| {r.tokens_in:,} | ${r.usd:.4f} | {r.wall_s:.1f}s "
            f"| {r.llm_calls_per_tool:.1f} | {r.judge_overhead_fraction:.0%} |"
        )
    return f"{header}\n{sep}\n" + "\n".join(rows)


# ---------------------------------------------------------------------------
# from_results_json() — build RunMetrics list from tb results.json
# ---------------------------------------------------------------------------

def from_results_json(results_path: str | Path, traces_dir: str | Path) -> list[RunMetrics]:
    """Load RunMetrics for all trials in a terminal-bench results.json.

    `traces_dir` should be the run output directory containing per-trial
    agent-logs subdirectories (e.g. data/tb-runs/cat/cat-phase2).
    """
    results_path = Path(results_path)
    traces_dir = Path(traces_dir)
    with open(results_path) as f:
        data = json.load(f)

    out: list[RunMetrics] = []
    for trial in data.get("results", []):
        task_id = trial["task_id"]
        trial_name = trial["trial_name"]
        pattern = f"{task_id}/{trial_name}/agent-logs/*.jsonl"
        matches = list(traces_dir.glob(pattern))

        if not matches:
            m = RunMetrics(
                run_id=trial.get("id", "")[:12],
                task_id=task_id,
                solved=trial.get("is_resolved", False),
                verdict="solved" if trial.get("is_resolved") else "failed",
                tokens_in=trial.get("total_input_tokens", 0),
                tokens_out=trial.get("total_output_tokens", 0),
            )
        else:
            m = RunMetrics.from_jsonl(matches[0])
            # Identity from results.json (TbCatAgent/TbReactAgent don't emit session_start)
            if not m.task_id:
                m.task_id = task_id
            if not m.run_id:
                m.run_id = trial.get("id", "")[:12]
            if not m.agent:
                # infer from path: cat-tb-xxx.jsonl or react-tb-xxx.jsonl
                stem = matches[0].stem
                m.agent = "cat-tb" if stem.startswith("cat") else "react-tb"
            # wall_s from tb timestamps if not in trace
            if m.wall_s == 0.0:
                a, b = trial.get("agent_started_at"), trial.get("agent_ended_at")
                if a and b:
                    from datetime import datetime
                    m.wall_s = (
                        datetime.fromisoformat(b) - datetime.fromisoformat(a)
                    ).total_seconds()
            # Overlay ground-truth solve verdict from tb grader
            m.solved = trial.get("is_resolved", False)
            m.verdict = "solved" if m.solved else "failed"

        out.append(m)

    return out


# ---------------------------------------------------------------------------
# Internal builder
# ---------------------------------------------------------------------------

def _build_from_events(events: list[dict]) -> RunMetrics:
    m = RunMetrics()
    first_tool_ts: Optional[float] = None
    session_start_ts: Optional[float] = None

    for ev in events:
        kind = ev.get("kind", "")
        p = ev.get("payload", {})

        # ── identity ──────────────────────────────────────────────────────
        if kind == "session_start":
            m.run_id = ev.get("event_id", "")
            m.task_id = p.get("task_id", "")
            m.agent = p.get("agent", "")
            m.model = p.get("model", "")
            budget = p.get("budget", {})
            _max_tokens = budget.get("tokens_total", 0)
            _max_tools = budget.get("tool_calls", 0)
            session_start_ts = _parse_wall(ev.get("ts", ""))

        elif kind == "session_end":
            m.verdict = p.get("verdict", "")
            m.solved = m.verdict == "solved"
            m.failure_reason = p.get("failure_reason")
            m.wall_s = float(p.get("wall_s", 0))
            if not m.iterations:
                m.iterations = int(p.get("iterations", 0))

        # ── LLM usage ────────────────────────────────────────────────────
        elif kind == "llm_usage":
            role = p.get("role", "unknown")
            tin = int(p.get("in", 0))
            tout = int(p.get("out", 0))
            usd = float(p.get("usd", 0))
            ws = float(p.get("wall_s", 0))

            m.llm_calls += 1
            m.llm_calls_by_role[role] = m.llm_calls_by_role.get(role, 0) + 1
            m.tokens_in += tin
            m.tokens_out += tout
            m.tokens_in_by_role[role] = m.tokens_in_by_role.get(role, 0) + tin
            m.tokens_out_by_role[role] = m.tokens_out_by_role.get(role, 0) + tout
            m.usd += usd
            m.usd_by_role[role] = m.usd_by_role.get(role, 0.0) + usd
            m.wall_s_llm += ws
            m.max_prompt_tokens = max(m.max_prompt_tokens, tin)

            if role == "judge":
                m.judge_calls += 1

        # ── tool calls ───────────────────────────────────────────────────
        elif kind == "tool_call":
            name = p.get("tool", "unknown")
            m.tool_calls_by_name[name] = m.tool_calls_by_name.get(name, 0) + 1
            ts = _parse_wall(ev.get("ts", ""))
            if first_tool_ts is None and session_start_ts is not None and ts:
                first_tool_ts = ts
                m.ttft_s = ts - session_start_ts

        elif kind == "tool_result":
            m.tool_calls += 1
            ws = float(p.get("wall_s", 0))
            m.wall_s_tools += ws
            if p.get("is_error"):
                m.tool_errors += 1

        # ── CAT loop ─────────────────────────────────────────────────────
        elif kind in ("capability_perform", "capability_invoke", "capability_activate"):
            # capability_perform is the v1.6.2.9 name (was: capability_invoke / capability_activate).
            m.iterations += 1

        elif kind == "criterion_check":
            if p.get("judge"):
                # advisory judge call on a plan step
                if p.get("complete"):
                    m.plan_steps_completed += 1
            else:
                # deterministic criterion check
                m.criterion_checks += 1
                if p.get("satisfied") and m.criterion_first_satisfied is None:
                    m.criterion_first_satisfied = m.iterations

        elif kind in ("contract_stipulate", "contract_decompose"):
            # contract_stipulate is the new name (PRD 1.6.2.9); accept both.
            steps = p.get("steps", [])
            m.plan_steps_total += len(steps)

        elif kind == "contract_state":
            to_state = p.get("to", "")
            if to_state in ("fulfilled", "failed", "expired"):
                m.contract_final_state = to_state

    # post-build derived fields
    m.tool_calls = m.tool_calls or sum(m.tool_calls_by_name.values())

    return m


def _parse_wall(ts_str: str) -> Optional[float]:
    """Parse ISO-8601 timestamp to Unix float. Returns None on failure."""
    if not ts_str:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None
