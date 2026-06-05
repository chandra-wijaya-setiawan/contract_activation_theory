#!/usr/bin/env python3
"""Generate a readable HTML report from a CAT v3 trace JSONL file.

Usage:
    python scripts/trace_report.py <path/to/cat-tb-*.jsonl> [output.html]
    python scripts/trace_report.py <run_dir>   # auto-finds latest JSONL

Output: standalone HTML with:
  - Run summary (PASS/FAIL, cost, hypothesis count)
  - Per-hypothesis section: location, change, predicts
  - Per-attempt: plan steps, tool calls + outputs, verdict
  - Color-coded: PASS=green, FAIL=red, errors=orange
"""
import json
import sys
import os
import glob
import html
from pathlib import Path
from collections import Counter


# ── Load trace ────────────────────────────────────────────────────────────

def load_trace(path: str) -> list[dict]:
    p = Path(path)
    if p.is_dir():
        candidates = sorted(p.glob("**/agent-logs/cat-tb-*.jsonl"))
        if not candidates:
            sys.exit(f"No JSONL found under {path}")
        p = candidates[-1]
    return [json.loads(l) for l in open(p)]


def find_results_json(jsonl_path: str) -> dict:
    p = Path(jsonl_path)
    rj = p.parent.parent / "results.json"
    if rj.exists():
        return json.loads(rj.read_text())
    return {}


# ── Parse events ─────────────────────────────────────────────────────────

def parse_trace(events: list[dict]) -> dict:
    """Group events into structured data per hypothesis."""
    summary = {
        "kinds": dict(Counter(e["kind"] for e in events)),
        "total_events": len(events),
        "hypotheses": [],
        "llm_cost": {"usd": 0.0, "tok_in": 0, "tok_out": 0, "by_role": {}},
        "contract_state": [],
    }

    for e in events:
        p = e.get("payload", {})
        k = e["kind"]
        if k == "llm_usage":
            summary["llm_cost"]["usd"] += p.get("usd", 0) or 0
            summary["llm_cost"]["tok_in"] += p.get("in", 0) or 0
            summary["llm_cost"]["tok_out"] += p.get("out", 0) or 0
            role = p.get("role", "?")
            summary["llm_cost"]["by_role"][role] = (
                summary["llm_cost"]["by_role"].get(role, 0) + (p.get("usd", 0) or 0)
            )
        if k == "contract_state":
            summary["contract_state"].append({
                "from": p.get("from", "?"),
                "to": p.get("to", "?"),
                "reason": p.get("reason", ""),
            })

    # Group by hypothesis
    current_h = None
    current_attempt = None
    tool_call_map = {}  # tc_id → tool_call payload

    for e in events:
        p = e.get("payload", {})
        k = e["kind"]

        if k == "hypothesis_attempt":
            current_h = {
                "n": p.get("n"),
                "location": p.get("location", ""),
                "change": p.get("change", ""),
                "predicts": p.get("predicts", ""),
                "basis": p.get("basis", ""),
                "attempts": [],
                "verdict": None,
                "knowledge_update": None,
            }
            summary["hypotheses"].append(current_h)
            current_attempt = None

        if k == "performer_plan" and current_h is not None:
            current_h["plan"] = {
                "current_state": p.get("current_state", ""),
                "steps": p.get("steps", []),
                "confidence": p.get("confidence", "?"),
            }

        if k == "tool_call" and current_h is not None:
            tc_id = p.get("tool_call_id", p.get("args", {}).get("tool_call_id", ""))
            entry = {
                "tool": p.get("tool", "?"),
                "args": p.get("args", {}),
                "reason": p.get("reason", ""),
                "tc_id": tc_id,
                "output": None,
                "is_error": False,
            }
            tool_call_map[tc_id] = entry
            if not current_attempt or current_attempt.get("closed"):
                current_attempt = {"tool_calls": [], "closed": False}
                current_h["attempts"].append(current_attempt)
            current_attempt["tool_calls"].append(entry)

        if k == "tool_result" and current_h is not None:
            tc_id = p.get("tool_call_id", "")
            out = p.get("output", "") or ""
            is_err = p.get("is_error", False)
            if tc_id in tool_call_map:
                tool_call_map[tc_id]["output"] = out
                tool_call_map[tc_id]["is_error"] = is_err

        if k == "knowledge_update" and current_h is not None:
            current_h["knowledge_update"] = {
                "verdict": p.get("verdict", "?"),
                "reason": p.get("reason", ""),
                "findings_count": p.get("findings_count", 0),
            }
            current_h["verdict"] = p.get("verdict")
            if current_attempt:
                current_attempt["closed"] = True

    return summary


# ── HTML generation ───────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       font-size: 13px; background: #f5f5f5; color: #333; padding: 16px; }
h1 { font-size: 18px; margin-bottom: 12px; }
h2 { font-size: 15px; margin: 16px 0 8px; }
h3 { font-size: 13px; font-weight: 600; margin: 8px 0 4px; color: #555; }
.summary { background: white; border: 1px solid #ddd; border-radius: 6px;
           padding: 14px; margin-bottom: 16px; display: flex; gap: 24px;
           flex-wrap: wrap; }
.stat { display: flex; flex-direction: column; gap: 2px; }
.stat-label { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .5px; }
.stat-value { font-size: 20px; font-weight: 700; }
.pass { color: #22863a; }
.fail { color: #cb2431; }
.exhausted { color: #e36209; }
.completed { color: #22863a; }
.give_up { color: #cb2431; }
.badge { display: inline-block; padding: 2px 7px; border-radius: 10px;
         font-size: 11px; font-weight: 600; }
.badge-pass { background: #dcfce7; color: #166534; }
.badge-fail { background: #fee2e2; color: #991b1b; }
.badge-exhausted { background: #fff7ed; color: #9a3412; }
.badge-completed { background: #dcfce7; color: #166534; }
.badge-give_up { background: #fee2e2; color: #991b1b; }
.badge-low { background: #fee2e2; color: #991b1b; }
.badge-medium { background: #fef9c3; color: #854d0e; }
.badge-high { background: #dcfce7; color: #166534; }
.hypothesis { background: white; border: 1px solid #ddd; border-radius: 6px;
              margin-bottom: 12px; overflow: hidden; }
.hyp-header { padding: 12px 14px; border-bottom: 1px solid #eee;
              background: #fafafa; cursor: pointer; }
.hyp-header:hover { background: #f0f0f0; }
.hyp-n { font-size: 11px; font-weight: 700; color: #888;
          text-transform: uppercase; letter-spacing: .5px; }
.hyp-location { font-family: monospace; font-size: 12px; color: #0550ae;
                margin: 3px 0; }
.hyp-change { color: #444; margin: 2px 0; }
.hyp-body { padding: 12px 14px; }
.field { margin-bottom: 8px; }
.field-label { font-size: 11px; font-weight: 700; color: #888;
               text-transform: uppercase; letter-spacing: .5px; margin-bottom: 2px; }
.field-value { color: #333; }
.plan-steps { background: #f8f9fa; border: 1px solid #e9ecef;
              border-radius: 4px; padding: 8px 10px; margin: 6px 0; }
.step { padding: 3px 0; border-bottom: 1px solid #eee; display: flex;
        align-items: flex-start; gap: 6px; }
.step:last-child { border-bottom: none; }
.step-status { font-size: 13px; width: 16px; flex-shrink: 0; }
.step-action { color: #333; }
.step-rationale { font-size: 11px; color: #888; margin-top: 1px; }
.attempt { border: 1px solid #eee; border-radius: 4px;
           margin-bottom: 8px; overflow: hidden; }
.attempt-header { background: #f8f9fa; padding: 6px 10px; font-size: 11px;
                  font-weight: 600; color: #666; border-bottom: 1px solid #eee; }
.tool-call { padding: 6px 10px; border-bottom: 1px solid #f0f0f0; }
.tool-call:last-child { border-bottom: none; }
.tool-name { font-family: monospace; font-weight: 700; font-size: 12px; color: #6f42c1; }
.tool-name.error { color: #cb2431; }
.tool-args { font-family: monospace; font-size: 11px; color: #555;
             margin: 3px 0; white-space: pre-wrap; word-break: break-all; }
.tool-reason { font-size: 11px; color: #888; font-style: italic; margin-top: 2px; }
.tool-output { background: #f6f8fa; border: 1px solid #e9ecef; border-radius: 3px;
               font-family: monospace; font-size: 11px; padding: 6px 8px;
               margin-top: 4px; white-space: pre-wrap; word-break: break-all;
               max-height: 200px; overflow-y: auto; }
.tool-output.error-out { background: #fff5f5; border-color: #fca5a5; color: #991b1b; }
details summary { cursor: pointer; color: #0550ae; font-size: 11px; }
details summary:hover { text-decoration: underline; }
.knowledge { background: #f0fdf4; border: 1px solid #bbf7d0;
             border-radius: 4px; padding: 8px 10px; margin-top: 8px;
             font-size: 12px; }
.knowledge.fail-k { background: #fff5f5; border-color: #fca5a5; }
.state-flow { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
.state-chip { background: #e0e7ff; color: #3730a3; padding: 2px 8px;
              border-radius: 10px; font-size: 11px; font-weight: 600; }
.state-chip.fulfilled { background: #dcfce7; color: #166534; }
.state-chip.failed { background: #fee2e2; color: #991b1b; }
.collapsible { display: none; }
.collapsible.open { display: block; }
"""

JS = """
function toggle(id) {
    var el = document.getElementById(id);
    el.classList.toggle('open');
}
"""

def h(s: str) -> str:
    return html.escape(str(s) if s is not None else "")


def badge(text: str) -> str:
    cls = f"badge-{str(text).lower().replace(' ', '_')}"
    return f'<span class="badge {cls}">{h(text)}</span>'


def render_args(args: dict, tool: str) -> str:
    if not args:
        return ""
    # Show most important args first
    important = []
    for key in ("command", "path", "pattern", "name", "reason"):
        if key in args and key != "reason":
            val = str(args[key])
            if len(val) > 120:
                val = val[:120] + "…"
            important.append(f'<span style="color:#888">{h(key)}</span>=<span style="color:#0550ae">{h(val)}</span>')
    # old_str/new_str separately
    if "old_str" in args:
        old = str(args["old_str"])
        important.append(f'<span style="color:#888">old_str</span>({len(old)}c)=<span style="color:#b45309">{h(old[:80])}{"…" if len(old)>80 else ""}</span>')
    if "new_str" in args:
        new = str(args["new_str"])
        important.append(f'<span style="color:#888">new_str</span>({len(new)}c)=<span style="color:#166534">{h(new[:80])}{"…" if len(new)>80 else ""}</span>')
    return "<br>".join(important)


def render_output(output: str | None, is_error: bool, tc_id: str) -> str:
    if not output:
        return ""
    short = output[:300]
    rest = output[300:] if len(output) > 300 else ""
    cls = "tool-output error-out" if is_error else "tool-output"
    uid = f"out-{tc_id[:8]}"
    if rest:
        return (
            f'<div class="{cls}">{h(short)}'
            f'<details><summary>show more ({len(output)} chars total)</summary>'
            f'<span>{h(rest)}</span></details></div>'
        )
    return f'<div class="{cls}">{h(short)}</div>'


def render_hypothesis(hyp: dict, idx: int) -> str:
    n = hyp.get("n", idx)
    verdict = hyp.get("verdict", "?") or "?"
    verdict_badge = badge(verdict)
    ku = hyp.get("knowledge_update")

    collapsed_id = f"hyp-{idx}"
    loc = hyp.get("location", "")
    change = hyp.get("change", "")[:100]

    parts = [
        f'<div class="hypothesis">',
        f'<div class="hyp-header" onclick="toggle(\'{collapsed_id}\')">',
        f'<div class="hyp-n">Hypothesis {n} &nbsp; {verdict_badge}</div>',
        f'<div class="hyp-location">{h(loc)}</div>',
        f'<div class="hyp-change">{h(change)}{"…" if len(hyp.get("change",""))>100 else ""}</div>',
        f'</div>',
        f'<div id="{collapsed_id}" class="collapsible hyp-body">',
    ]

    # Fields
    for label, val in [
        ("Location", hyp.get("location", "")),
        ("Change", hyp.get("change", "")),
        ("Predicts", hyp.get("predicts", "")),
        ("Basis", hyp.get("basis", "")),
    ]:
        if val:
            parts.append(f'<div class="field"><div class="field-label">{label}</div>'
                         f'<div class="field-value">{h(val)}</div></div>')

    # Plan if captured
    plan = hyp.get("plan")
    if plan:
        steps_html = ""
        for s in plan.get("steps", []):
            status = s.get("status", "pending")
            icon = "✓" if status == "done" else ("✗" if status == "failed" else "○")
            color = "#22863a" if status == "done" else ("#cb2431" if status == "failed" else "#888")
            steps_html += (
                f'<div class="step">'
                f'<span class="step-status" style="color:{color}">{icon}</span>'
                f'<div><div class="step-action">{h(s.get("action",""))}</div>'
                f'<div class="step-rationale">{h(s.get("rationale","")[:80])}</div></div>'
                f'</div>'
            )
        conf = plan.get("confidence", "?")
        parts.append(
            f'<div class="field"><div class="field-label">Plan '
            f'{badge(conf)}</div>'
            f'<div class="plan-steps">'
            f'<div class="field-value" style="font-size:11px;color:#888;margin-bottom:4px">'
            f'{h(plan.get("current_state",""))}</div>'
            f'{steps_html}</div></div>'
        )

    # Attempts / tool calls
    attempts = hyp.get("attempts", [])
    if attempts:
        parts.append('<div class="field"><div class="field-label">Tool calls</div>')
        for ai, att in enumerate(attempts):
            parts.append(f'<div class="attempt">')
            parts.append(f'<div class="attempt-header">Attempt {ai+1}</div>')
            for tc in att.get("tool_calls", []):
                tool = tc.get("tool", "?")
                is_err = tc.get("is_error", False)
                tc_id = tc.get("tc_id", f"tc{ai}{tool}")
                reason = tc.get("reason", "")
                name_cls = "tool-name error" if is_err else "tool-name"
                err_badge = ' <span style="color:#cb2431;font-size:10px">ERROR</span>' if is_err else ""
                parts.append(
                    f'<div class="tool-call">'
                    f'<span class="{name_cls}">{h(tool)}</span>{err_badge}'
                )
                args_html = render_args(tc.get("args", {}), tool)
                if args_html:
                    parts.append(f'<div class="tool-args">{args_html}</div>')
                if reason:
                    parts.append(f'<div class="tool-reason">reason: {h(reason[:100])}</div>')
                out_html = render_output(tc.get("output"), is_err, tc_id)
                if out_html:
                    parts.append(out_html)
                parts.append('</div>')
            parts.append('</div>')
        parts.append('</div>')

    # Knowledge update
    if ku:
        ku_v = ku.get("verdict", "?")
        ku_cls = "knowledge" if ku_v in ("completed",) else "knowledge fail-k"
        parts.append(
            f'<div class="{ku_cls}">'
            f'<strong>Verdict:</strong> {badge(ku_v)} &nbsp;'
            f'<strong>Findings:</strong> {ku.get("findings_count", 0)} &nbsp;'
            f'{h(ku.get("reason","")[:150])}'
            f'</div>'
        )

    parts.append('</div></div>')  # close collapsible + hypothesis
    return "\n".join(parts)


def generate_html(jsonl_path: str, results: dict) -> str:
    events = load_trace(jsonl_path)
    data = parse_trace(events)

    is_resolved = results.get("is_resolved")
    task_id = results.get("task_id", Path(jsonl_path).parent.parent.name)
    status_cls = "pass" if is_resolved else "fail"
    status_text = "PASS" if is_resolved else "FAIL"

    usd = data["llm_cost"]["usd"]
    tok_in = data["llm_cost"]["tok_in"]
    n_hyp = len(data["hypotheses"])
    by_role = data["llm_cost"]["by_role"]

    # Contract state flow
    states = data["contract_state"]
    final_state = states[-1]["to"] if states else "?"
    state_chips = " → ".join(
        f'<span class="state-chip {s["to"]}">{s["to"]}</span>'
        for s in states
    )

    # Role cost table
    role_rows = "".join(
        f'<tr><td>{h(r)}</td><td style="text-align:right">${v:.4f}</td></tr>'
        for r, v in sorted(by_role.items(), key=lambda x: -x[1])
    )

    hyps_html = "\n".join(
        render_hypothesis(hyp, i + 1)
        for i, hyp in enumerate(data["hypotheses"])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CAT v3 Trace — {h(task_id)}</title>
<style>{CSS}</style>
<script>{JS}</script>
</head>
<body>
<h1>CAT v3 Trace Report</h1>
<h2 style="color:#555;font-weight:400">{h(task_id)}</h2>

<div class="summary">
  <div class="stat">
    <span class="stat-label">Result</span>
    <span class="stat-value {status_cls}">{status_text}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Hypotheses</span>
    <span class="stat-value">{n_hyp}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Tokens in</span>
    <span class="stat-value">{tok_in//1000}K</span>
  </div>
  <div class="stat">
    <span class="stat-label">Total cost</span>
    <span class="stat-value">${usd:.4f}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Events</span>
    <span class="stat-value">{data["total_events"]}</span>
  </div>
  <div class="stat" style="flex-grow:1">
    <span class="stat-label">Contract state</span>
    <div class="state-flow">{state_chips}</div>
  </div>
</div>

<div style="background:white;border:1px solid #ddd;border-radius:6px;padding:12px;margin-bottom:16px">
  <h3>Cost by role</h3>
  <table style="margin-top:6px;border-collapse:collapse;font-size:12px">
    {role_rows}
  </table>
</div>

<h2>Hypotheses ({n_hyp})</h2>
<p style="font-size:11px;color:#888;margin-bottom:8px">Click a hypothesis header to expand/collapse details.</p>
{hyps_html}

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/trace_report.py <path.jsonl|run_dir> [output.html]")
        sys.exit(1)

    input_path = sys.argv[1]

    # Find JSONL
    p = Path(input_path)
    if p.is_dir():
        candidates = sorted(p.glob("**/agent-logs/cat-tb-*.jsonl"))
        if not candidates:
            sys.exit(f"No JSONL found under {input_path}")
        jsonl_path = str(candidates[-1])
    else:
        jsonl_path = input_path

    # Find results.json
    results = find_results_json(jsonl_path)

    # Output path
    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
    else:
        base = Path(jsonl_path).stem
        out_path = str(Path(jsonl_path).parent.parent / f"{base}_report.html")

    html_content = generate_html(jsonl_path, results)
    Path(out_path).write_text(html_content)
    print(f"Report written to: {out_path}")
    print(f"Open with: open {out_path}")
