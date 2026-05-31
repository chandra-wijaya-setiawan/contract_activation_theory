"""Layer attribution — credit/blame across the 8-step CAT loop.

For every task run, attribute the outcome (PASS / FAIL / SUSPENDED) across
the four substantive layers:

  REPRESENT  (Step 2)  — probes ran; representations captured the environment
  DECOMPOSE  (Step 3)  — planner produced a plan grounded in those facts
  INVOKE     (Step 6)  — executor drove tools toward plan completion
  VERIFY    (Step 7)  — criterion (oracle/judge) decided closure

Two modes:
  - `attribute_task_llm(...)` — LLM-driven (default in development). Given
    trace + plan + last evidence, a judge LLM returns structured layer
    verdicts + primary credit/blame. Higher signal on subtle FAILs.
  - `attribute_task_heuristic(...)` — deterministic rule set. Used as
    fallback when no LLM is provided / LLM call fails.

Both return the same `Attribution` dataclass so downstream report code is
mode-agnostic.

Cached by `(trace_path_mtime, mode)` — re-running the report is free.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


LAYERS = ("represent", "stipulate", "perform", "verify")
STATUSES = ("OK", "DEGRADED", "FAILED", "NA")
# Legacy layer aliases (old reports may store these); normalize on read.
_LAYER_ALIAS = {"decompose": "stipulate", "invoke": "perform"}

# 1.6.5.1: diagnostic taxonomy
LIMITATION_KINDS = (
    "framework_structural",   # CAT missing a mechanism
    "framework_parameter",    # CAT setting wrong but design OK
    "model_intelligence",     # stronger model would solve
    "model_prompt",           # better instruction would solve
    "tool_environment",       # tool/env got in the way
    "task_complexity",        # genuinely hard for SOTA
    "none",                   # for PASS outcomes
)
IMPROVEMENT_VECTORS = (
    "framework_code", "prompt", "model", "tool", "infra", "accept", "none",
)
CONFIDENCE = ("high", "medium", "low")


@dataclass
class LayerVerdict:
    layer: str            # one of LAYERS
    status: str           # one of STATUSES
    evidence: str = ""    # 1-2 sentence rationale


@dataclass
class Attribution:
    task_id: str
    outcome: str          # PASS | FAIL | SUSPENDED | INFRA_ERROR
    layers: list[LayerVerdict] = field(default_factory=list)
    primary: str = ""     # which layer earned primary credit (PASS) or blame (FAIL)
    rationale: str = ""   # 1-2 sentences for the primary verdict
    method: str = ""      # "llm" | "heuristic" | "cached"
    # 1.6.5.1 — diagnostic taxonomy fields (populated by LLM auditor; heuristic
    # fills minimally). For PASS outcomes, limitation_kind="none".
    limitation_kind: str = ""            # framework_structural | framework_parameter | model_intelligence | model_prompt | tool_environment | task_complexity | none
    limitation_secondary: str = ""       # same vocab, or empty
    improvement_vector: str = ""         # framework_code | prompt | model | tool | infra | accept | none
    improvement_specifics: str = ""      # 1-line: which file/prompt/model
    narrative: str = ""                  # paragraph-length root-cause analysis
    confidence: str = ""                 # high | medium | low


# ── Public entry ───────────────────────────────────────────────────────────


def attribute_task(
    task_id: str,
    metrics: Any,                       # cat.metrics.RunMetrics
    events: list[dict],
    *,
    outcome: Optional[str] = None,      # PASS|FAIL|SUSPENDED|INFRA_ERROR; if None, derived from trace
    llm: Optional[Any] = None,          # cat.llm.LLM or None for heuristic-only
    cache_dir: Optional[Path] = None,   # if set, cache by (events_hash, method)
) -> Attribution:
    """Attribute a single task's outcome across CAT layers.

    Tries LLM first if `llm` is provided. Falls back to deterministic
    heuristic on any LLM failure (parse error, network, missing key).

    `outcome` should be passed by the caller when authoritative ground truth
    is available (e.g. harness results.json). When omitted, falls back to
    deriving from the trace's final contract_state — less reliable.
    """
    if outcome is None:
        outcome = _classify_outcome(metrics, events)
    cache_key = _cache_key(events) if cache_dir else None
    method = "llm" if llm is not None else "heuristic"

    # Cached?
    if cache_dir and cache_key:
        cached = _load_cache(cache_dir, cache_key, method)
        if cached:
            cached.method = "cached"
            return cached

    if llm is not None:
        try:
            attr = attribute_task_llm(task_id, metrics, events, outcome, llm)
            if cache_dir and cache_key:
                _save_cache(cache_dir, cache_key, "llm", attr)
            return attr
        except Exception as exc:  # noqa: BLE001 — any LLM failure → heuristic
            attr = attribute_task_heuristic(task_id, metrics, events, outcome)
            attr.rationale = (
                f"{attr.rationale} [LLM attribution failed: {type(exc).__name__}; "
                f"heuristic used]"
            )
            return attr

    attr = attribute_task_heuristic(task_id, metrics, events, outcome)
    if cache_dir and cache_key:
        _save_cache(cache_dir, cache_key, "heuristic", attr)
    return attr


# ── Outcome classification ─────────────────────────────────────────────────


def _classify_outcome(metrics: Any, events: list[dict]) -> str:
    """Map metrics + final contract_state event to a normalized outcome."""
    if getattr(metrics, "solved", False):
        return "PASS"
    final = (getattr(metrics, "contract_final_state", "") or "").lower()
    if final == "suspended":
        return "SUSPENDED"
    if final in ("failed", "expired"):
        return "FAIL"
    # No contract state recorded (infra crash before agent started)
    return "INFRA_ERROR"


# ── LLM-driven attribution ─────────────────────────────────────────────────


_ATTR_SYSTEM_PROMPT = """\
You are the AUDITOR role in a contract-disciplined agent (CAT). Your job is
to diagnose a single task's execution trace — not just assign blame, but
identify WHERE THE LIMITATION ACTUALLY LIVES so the user knows what to invest in.

The four CAT layers:
  REPRESENT  — Step 2. Deterministic probes capture verified facts (workdir,
               test command, file types). No LLM.
  STIPULATE  — Step 3. Stipulator LLM emits ordered performance terms (plan).
  PERFORM    — Step 6. Performer LLM picks tools to discharge stipulations.
  VERIFY     — Step 7. Criterion (oracle test) decides FULFILLED.

Diagnose along FOUR dimensions:

1. WHICH LAYER manifests the failure: represent | stipulate | perform | verify

2. LIMITATION KIND (the *nature* of the weakness; pick 1, optional secondary):
   - framework_structural — CAT is missing a mechanism (e.g., no replan on
     dead-end, no candidate exploration, no fix-on-retry, no sub-contracts)
   - framework_parameter  — CAT design fine but a setting is wrong (envelope
     too small, criterion_check_interval too coarse, plan too short)
   - model_intelligence   — base model couldn't reason about this; a stronger
     model class (Sonnet 4.6/4.7 vs Flash) would likely solve
   - model_prompt         — same model could solve with better instruction;
     prompt engineering opportunity
   - tool_environment     — tool / env got in the way (tmux timeouts, Docker
     latency, file-not-found loops, network flakiness)
   - task_complexity      — task genuinely too hard / ambiguous for current
     SOTA; out of scope
   - none                 — only for PASS outcomes

3. IMPROVEMENT VECTOR (where to invest):
   - framework_code — change CAT code (name the module: activator.py / capabilities.py / etc.)
   - prompt         — tune a system prompt (name which: stipulator / performer / attestor)
   - model          — upgrade model class (specify class)
   - tool           — fix a specific tool (name it: build_tmux_registry / oracle bash / etc.)
   - infra          — fix environment (Docker / network / cpubox)
   - accept         — out of scope; categorize as task_complexity
   - none           — for PASS outcomes

4. CONFIDENCE: high | medium | low — how sure is the diagnosis. Use low if the
   evidence is ambiguous; medium for plausible but not certain; high only if
   the trace explicitly demonstrates the failure mode.

For PASS outcomes:
- Set limitation_kind = "none", improvement_vector = "none", confidence = high.
- The narrative still explains WHY the agent succeeded (what was the enabler).

For FAIL / SUSPENDED:
- Be specific. "Plan misidentified function name `_dependencies` (actual:
  `_get_deps_info`)" is useful; "plan was wrong" is not.
- For envelope-exhausted runs: if plan_progress is low (<50%), prefer
  framework_structural (missing replan-on-deadlock) OR model_intelligence
  (couldn't converge). If plan_progress is high but criterion never satisfied,
  prefer model_intelligence (edits were wrong) or tool_environment.

Output ONLY a JSON object with this exact shape:
{
  "represent": {"status": "OK|DEGRADED|FAILED|NA", "evidence": "<short>"},
  "stipulate": {"status": "OK|DEGRADED|FAILED|NA", "evidence": "<short>"},
  "perform":   {"status": "OK|DEGRADED|FAILED|NA", "evidence": "<short>"},
  "verify":    {"status": "OK|DEGRADED|FAILED|NA", "evidence": "<short>"},
  "primary":   "represent|stipulate|perform|verify",
  "rationale": "<one or two sentences for the primary layer>",
  "limitation_kind":      "framework_structural|framework_parameter|model_intelligence|model_prompt|tool_environment|task_complexity|none",
  "limitation_secondary": "<same enum, or empty string>",
  "improvement_vector":   "framework_code|prompt|model|tool|infra|accept|none",
  "improvement_specifics":"<one sentence: which file/prompt/model/tool, optionally a 1-line code suggestion>",
  "narrative":            "<paragraph: what the agent tried, why it didn't work, where the root cause lives. 3-5 sentences>",
  "confidence":           "high|medium|low"
}
"""


def attribute_task_llm(
    task_id: str,
    metrics: Any,
    events: list[dict],
    outcome: str,
    llm: Any,
) -> Attribution:
    """LLM-driven attribution. Raises on any failure (caller falls back to heuristic)."""
    summary = _build_trace_summary(metrics, events, outcome)
    messages = [
        {"role": "system", "content": _ATTR_SYSTEM_PROMPT},
        {"role": "user", "content": f"TASK: {task_id}\nOUTCOME: {outcome}\n\n{summary}"},
    ]
    result = llm.chat(
        messages=messages,
        response_format={"type": "json_object"},
        role_tag="auditor",
        temperature=0.0,
    )
    if not result.text:
        raise ValueError("auditor returned no text")

    # Pydantic-validate the structural shape (per-layer status enum + primary
    # enum). Diagnostic fields stay strings; their enum-membership check
    # happens below since the allowed values are auditor-specific constants.
    from cat.plan_schema import AttributionVerdict
    from pydantic import ValidationError
    try:
        v = AttributionVerdict.model_validate_json(result.text)
    except ValidationError as e:
        raise ValueError(f"auditor schema validation failed: {e}") from e

    layers: list[LayerVerdict] = []
    for layer in LAYERS:
        layer_v = getattr(v, layer)
        layers.append(LayerVerdict(
            layer=layer,
            status=layer_v.status,
            evidence=layer_v.evidence[:300],
        ))
    primary = v.primary  # Pydantic already constrained to LAYERS enum

    # 1.6.5.1 diagnostic fields — these have their own enum constants in this
    # module that don't belong in the schema (auditor-internal). Validate here.
    lim_kind = v.limitation_kind
    if lim_kind not in LIMITATION_KINDS:
        lim_kind = "none" if outcome == "PASS" else ""
    lim_sec = v.limitation_secondary or ""
    if lim_sec and lim_sec not in LIMITATION_KINDS:
        lim_sec = ""
    imp_vec = v.improvement_vector
    if imp_vec not in IMPROVEMENT_VECTORS:
        imp_vec = "none" if outcome == "PASS" else ""
    conf = v.confidence
    if conf not in CONFIDENCE:
        conf = "low"

    return Attribution(
        task_id=task_id,
        outcome=outcome,
        layers=layers,
        primary=primary,
        rationale=str(data.get("rationale", ""))[:400],
        method="llm",
        limitation_kind=lim_kind,
        limitation_secondary=lim_sec,
        improvement_vector=imp_vec,
        improvement_specifics=str(data.get("improvement_specifics", ""))[:300],
        narrative=str(data.get("narrative", ""))[:1500],
        confidence=conf,
    )


def _build_trace_summary(metrics: Any, events: list[dict], outcome: str) -> str:
    """Rich summary fed to the auditor LLM (~3-5K tokens). Used by 1.6.5.1
    diagnostic taxonomy.
    """
    parts: list[str] = []

    # ── Cost line ──
    tot = getattr(metrics, "tokens_total", 0) or 0
    parts.append(
        f"COST: tokens={tot:,}  llm_calls={metrics.llm_calls}  "
        f"tool_calls={metrics.tool_calls}  wall_s={metrics.wall_s:.1f}"
    )

    # ── Plan ──
    plan_steps: list[str] = []
    for ev in events:
        if ev.get("kind") in ("contract_stipulate", "contract_decompose"):
            for s in ev.get("payload", {}).get("steps", []):
                plan_steps.append(f"  [{s.get('id')}] {s.get('description','')[:200]}")
            break
    parts.append("PLAN:\n" + ("\n".join(plan_steps) if plan_steps else "  (none)"))

    # ── Plan completion + alignment ──
    if hasattr(metrics, "plan_steps_total"):
        parts.append(
            f"PLAN_PROGRESS: {metrics.plan_steps_completed}/{metrics.plan_steps_total} "
            f"steps judged complete"
        )

    # ── Files touched (write / str_replace tool_calls) ──
    files_touched: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") == "tool_call":
            p = ev.get("payload", {})
            tool = p.get("tool", "")
            args = p.get("args", {}) or {}
            if tool in ("write", "str_replace") and args.get("path"):
                path = args["path"]
                files_touched[path] = files_touched.get(path, 0) + 1
    if files_touched:
        parts.append("FILES_TOUCHED (write/str_replace counts):\n" + "\n".join(
            f"  {p}  ×{n}" for p, n in sorted(files_touched.items(), key=lambda x: -x[1])
        ))

    # ── Tool error histogram ──
    tool_err = getattr(metrics, "tool_errors", 0)
    tool_total = getattr(metrics, "tool_calls", 0)
    err_by_tool: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") == "tool_result" and ev.get("payload", {}).get("is_error"):
            tool = ev["payload"].get("tool", "?")
            err_by_tool[tool] = err_by_tool.get(tool, 0) + 1
    if tool_total:
        rate = 100 * tool_err / tool_total
        line = f"TOOL_ERROR_RATE: {tool_err}/{tool_total} = {rate:.0f}%"
        if err_by_tool:
            hist = " ".join(f"{t}={n}" for t, n in err_by_tool.items())
            line += f"  (by_tool: {hist})"
        parts.append(line)

    # ── Final state + reason ──
    final_state = ""
    final_reason = ""
    for ev in reversed(events):
        if ev.get("kind") == "contract_state":
            p = ev.get("payload", {})
            if not p.get("rejected"):
                final_state = p.get("to", "")
                final_reason = p.get("reason", "")
                break
    parts.append(f"FINAL: state={final_state}  reason={final_reason[:300]}")

    # ── Last oracle check FULL evidence (the test output that drove FULFILLED/SUSPENDED) ──
    oracle_checks = [
        ev for ev in events
        if ev.get("kind") == "criterion_check" and ev.get("payload", {}).get("is_oracle")
    ]
    if oracle_checks:
        last = oracle_checks[-1]
        p = last["payload"]
        evidence = p.get("evidence") or ""
        parts.append(
            f"LAST_ORACLE_CHECK:\n"
            f"  iter={p.get('iteration')}  sat={p.get('satisfied')}\n"
            f"  evidence:\n    {evidence[:1500].replace(chr(10), chr(10)+'    ')}"
        )
        if len(oracle_checks) > 1:
            n_pass = sum(1 for c in oracle_checks if c["payload"].get("satisfied"))
            parts.append(
                f"ORACLE_CHECK_HISTORY: {len(oracle_checks)} checks; "
                f"{n_pass} passed"
            )

    # ── Last 3 tool errors (full output, not just first 120 chars) ──
    err_events = [
        ev for ev in events
        if ev.get("kind") == "tool_result" and ev.get("payload", {}).get("is_error")
    ]
    if err_events:
        err_lines = []
        for ev in err_events[-3:]:
            p = ev["payload"]
            out = (p.get("output") or "")[:300]
            err_lines.append(f"  {p.get('tool')}: {out}")
        parts.append("LAST_TOOL_ERRORS:\n" + "\n".join(err_lines))

    # ── Attestor verdicts on plan steps (which stipulations attested complete) ──
    attestor = [
        ev for ev in events
        if ev.get("kind") == "criterion_check" and ev.get("payload", {}).get("judge")
    ]
    if attestor:
        seen_steps: set[str] = set()
        att_lines = []
        for ev in reversed(attestor):
            p = ev["payload"]
            sid = p.get("step_id")
            if sid in seen_steps:
                continue
            seen_steps.add(sid)
            att_lines.append(
                f"  {sid}: complete={p.get('complete')}  reason={(p.get('reason') or '')[:120]}"
            )
        if att_lines:
            parts.append("ATTESTOR_VERDICTS (latest per step):\n" + "\n".join(reversed(att_lines)))

    return "\n\n".join(parts)


# ── Heuristic attribution (deterministic fallback) ─────────────────────────


def attribute_task_heuristic(
    task_id: str,
    metrics: Any,
    events: list[dict],
    outcome: str,
) -> Attribution:
    """Pure-rule attribution. Cheap; runs on every per-task auto-emit."""
    # Find final state reason
    final_reason = ""
    final_state = ""
    for ev in reversed(events):
        if ev.get("kind") == "contract_state":
            p = ev.get("payload", {})
            if not p.get("rejected"):
                final_state = p.get("to", "")
                final_reason = p.get("reason", "") or ""
                break

    # Layer-level signals
    n_reps = sum(
        1 for ev in events
        if ev.get("kind") == "contract_represent"
        for _ in ev.get("payload", {}).get("representations", [])
    )
    n_plan = sum(
        len(ev.get("payload", {}).get("steps", []))
        for ev in events
        if ev.get("kind") in ("contract_stipulate", "contract_decompose")
    )
    tool_err = getattr(metrics, "tool_errors", 0)
    tool_total = max(getattr(metrics, "tool_calls", 0), 1)
    err_rate = tool_err / tool_total
    iters = getattr(metrics, "iterations", 0)
    plan_done = getattr(metrics, "plan_steps_completed", 0)
    plan_total = max(getattr(metrics, "plan_steps_total", 0), 1)
    plan_rate = plan_done / plan_total

    # Layer verdicts — layer names match the new vocabulary (PRD 1.6.2.9)
    represent = LayerVerdict(
        layer="represent",
        status="OK" if n_reps >= 5 else ("DEGRADED" if n_reps else "FAILED"),
        evidence=f"{n_reps} representations captured",
    )
    stipulate = LayerVerdict(
        layer="stipulate",
        status="OK" if n_plan >= 2 else ("DEGRADED" if n_plan else "FAILED"),
        evidence=f"{n_plan} plan steps emitted",
    )
    perform = LayerVerdict(
        layer="perform",
        status="OK" if err_rate < 0.3 else ("DEGRADED" if err_rate < 0.6 else "FAILED"),
        evidence=f"{iters} iterations, tool error rate {err_rate:.0%}",
    )
    verify_status = "OK" if outcome == "PASS" else (
        "DEGRADED" if getattr(metrics, "criterion_checks", 0) else "FAILED"
    )
    verify = LayerVerdict(
        layer="verify",
        status=verify_status,
        evidence=f"{getattr(metrics, 'criterion_checks', 0)} criterion checks",
    )

    # Primary
    primary = "perform"
    rationale = ""
    if outcome == "PASS":
        if iters <= n_plan * 2 and plan_rate >= 0.9:
            primary, rationale = "stipulate", f"plan was tight ({n_plan} steps, completed {plan_rate:.0%}); performer followed it directly"
        elif err_rate < 0.1:
            primary, rationale = "perform", f"performer drove cleanly (err_rate {err_rate:.0%}) over {iters} iterations"
        else:
            primary, rationale = "verify", "oracle confirmed solve despite a noisy execution path"
    elif outcome == "SUSPENDED":
        primary, rationale = "perform", f"execution loop exhausted envelope: {final_reason[:120]}"
    elif outcome == "FAIL":
        rl = final_reason.lower()
        if "admission_boundary" in rl:
            primary, rationale = "represent", "contract malformed at admission gate"
        elif "admission_environment" in rl:
            primary, rationale = "represent", "world did not match contract assumptions at admission"
        elif "executor_quit" in rl or "performer_quit" in rl:
            primary, rationale = "perform", "performer stopped before criterion satisfied"
        elif "deadline" in rl or "wall_s" in rl:
            if plan_rate < 0.5:
                primary, rationale = "stipulate", f"plan only {plan_rate:.0%} complete when wall envelope expired"
            else:
                primary, rationale = "perform", "wall envelope exhausted despite plan progress"
        elif "tool_calls" in rl or "tokens" in rl:
            primary, rationale = "perform", f"resource envelope exhausted: {rl[:120]}"
        else:
            primary, rationale = "perform", f"unclassified failure: {final_reason[:120]}"
    else:  # INFRA_ERROR
        primary, rationale = "represent", "no contract state recorded — likely infra failure before agent started"

    # 1.6.5.1 heuristic diagnostic fields (best-effort from rule signals)
    lim_kind, imp_vec, lim_specifics = _heuristic_limitation(
        outcome, final_reason, err_rate, plan_rate, iters, n_plan,
    )
    narrative = rationale
    confidence = "low"   # heuristic is always low-confidence vs LLM

    return Attribution(
        task_id=task_id,
        outcome=outcome,
        layers=[represent, stipulate, perform, verify],
        primary=primary,
        rationale=rationale,
        method="heuristic",
        limitation_kind=lim_kind,
        limitation_secondary="",
        improvement_vector=imp_vec,
        improvement_specifics=lim_specifics,
        narrative=narrative,
        confidence=confidence,
    )


def _heuristic_limitation(
    outcome: str, final_reason: str, err_rate: float, plan_rate: float,
    iters: int, n_plan: int,
) -> tuple[str, str, str]:
    """Best-effort heuristic mapping to (limitation_kind, improvement_vector, specifics)."""
    if outcome == "PASS":
        return "none", "none", ""
    rl = (final_reason or "").lower()
    if "admission_boundary" in rl:
        return ("framework_structural", "framework_code",
                "Contract was malformed at admission; check ingest validation in activator._admit")
    if "admission_environment" in rl:
        return ("tool_environment", "infra",
                "World state didn't match contract assumptions at admission")
    if err_rate > 0.6:
        return ("tool_environment", "tool",
                f"high tool error rate ({err_rate:.0%}); likely tool/env reliability issue")
    if "deadline" in rl or "wall_s" in rl:
        if plan_rate < 0.5:
            return ("framework_structural", "framework_code",
                    "wall envelope expired with low plan progress; add replan-on-deadlock")
        return ("model_intelligence", "model",
                "wall envelope expired with plan progress; performer couldn't converge")
    if "tool_calls" in rl or "tokens" in rl:
        return ("framework_parameter", "framework_code",
                f"envelope exhausted: {final_reason[:120]}; consider raising max_iterations")
    if "executor_quit" in rl or "performer_quit" in rl:
        return ("model_prompt", "prompt",
                "performer quit before criterion; revise PERFORMER_SYSTEM_PROMPT to discourage early exit")
    if outcome == "SUSPENDED":
        return ("framework_structural", "framework_code",
                "criterion unreachable; no resume mechanism — needs 1.7.3 DAG-grounded SUSPENDED resume")
    return ("model_intelligence", "model",
            f"unclassified failure: {final_reason[:120]}")


# ── Cache ───────────────────────────────────────────────────────────────────


def _cache_key(events: list[dict]) -> str:
    """Stable hash of the trace event id list."""
    ids = "|".join(ev.get("event_id", "") for ev in events)
    return hashlib.sha256(ids.encode()).hexdigest()[:16]


def _load_cache(cache_dir: Path, key: str, method: str) -> Optional[Attribution]:
    p = cache_dir / f"{key}.{method}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        layers = [LayerVerdict(**lv) for lv in data.get("layers", [])]
        return Attribution(
            task_id=data["task_id"],
            outcome=data["outcome"],
            layers=layers,
            primary=data.get("primary", ""),
            rationale=data.get("rationale", ""),
            method=data.get("method", "cached"),
            # 1.6.5.1 diagnostic fields (optional for backward-compat with old cache)
            limitation_kind=data.get("limitation_kind", "") or "",
            limitation_secondary=data.get("limitation_secondary", "") or "",
            improvement_vector=data.get("improvement_vector", "") or "",
            improvement_specifics=data.get("improvement_specifics", "") or "",
            narrative=data.get("narrative", "") or "",
            confidence=data.get("confidence", "") or "",
        )
    except Exception:
        return None


def _save_cache(cache_dir: Path, key: str, method: str, attr: Attribution) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / f"{key}.{method}.json"
    p.write_text(json.dumps(asdict(attr), indent=2))
