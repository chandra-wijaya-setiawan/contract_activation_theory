#!/usr/bin/env python3
"""Generate a readable HTML report from a CAT v3 trace JSONL file.

Usage:
    python scripts/trace_report.py <path/to/cat-tb-*.jsonl> [output.html]
    python scripts/trace_report.py <run_dir>   # auto-finds latest JSONL

Output: standalone HTML with full prompt/response/tool detail per iteration.
  - Run summary (PASS/FAIL, cost, hypothesis count)
  - Per-hypothesis: planning output, performer plan, react loop iterations
  - Per-iteration: LLM response text + tool call + output (all collapsible)
  - Oracle result per hypothesis
  - Color-coded: PASS=green, FAIL=red, errors=orange
"""
import json
import sys
import html
from pathlib import Path
from collections import Counter


# ── Load ──────────────────────────────────────────────────────────────────

def load_trace(path: str) -> list[dict]:
    p = Path(path)
    if p.is_dir():
        candidates = sorted(p.glob("**/agent-logs/cat-tb-*.jsonl"))
        if not candidates:
            sys.exit(f"No JSONL found under {path}")
        p = candidates[-1]
    return [json.loads(l) for l in open(p)], str(p)


def find_results_json(jsonl_path: str) -> dict:
    p = Path(jsonl_path)
    # Try trial results.json
    rj = p.parent.parent / "results.json"
    if rj.exists():
        try:
            d = json.loads(rj.read_text())
            # results.json might be a trial or run-level file
            if "is_resolved" in d:
                return d
            for t in d.get("results", []):
                if t.get("task_id") and Path(jsonl_path).parts[-3].startswith(t["task_id"].split(".")[0]):
                    return t
        except Exception:
            pass
    return {}


# ── Parse events ──────────────────────────────────────────────────────────

def parse_trace(events: list[dict]) -> dict:
    summary = {
        "kinds": dict(Counter(e["kind"] for e in events)),
        "total_events": len(events),
        "hypotheses": [],
        "llm_cost": {"usd": 0.0, "tok_in": 0, "tok_out": 0, "by_role": {}},
        "contract_state": [],
        "obligation": "",
        "escalations": [],
    }

    for e in events:
        p = e.get("payload", {})
        k = e["kind"]
        if k == "llm_usage":
            summary["llm_cost"]["usd"] += p.get("usd", 0) or 0
            summary["llm_cost"]["tok_in"] += p.get("in", 0) or 0
            summary["llm_cost"]["tok_out"] += p.get("out", 0) or 0
            role = p.get("role_tag") or p.get("role", "?")
            summary["llm_cost"]["by_role"][role] = (
                summary["llm_cost"]["by_role"].get(role, 0) + (p.get("usd", 0) or 0)
            )
        if k == "contract_state":
            summary["contract_state"].append({
                "from": p.get("from", "?"),
                "to": p.get("to", "?"),
                "reason": p.get("reason", ""),
            })
        if k == "stipulator_escalation":
            summary["escalations"].append({
                "from": p.get("from", "?").split("/")[-1],
                "to": p.get("to", "?").split("/")[-1],
                "reason": p.get("reason", ""),
                "hypothesis_n": p.get("hypothesis_n"),
            })

    # Build per-hypothesis structure with full react iterations
    current_h = None
    current_iter = None   # one react iteration = one LLM call + its tools
    tool_map = {}         # tc_id → entry
    pending_llm = None    # llm_usage waiting to attach to current iter

    for e in events:
        p = e.get("payload", {})
        k = e["kind"]

        if k == "hypothesis_attempt":
            current_h = {
                "n": p.get("n"),
                "error": p.get("error"),
                "location": p.get("location", ""),
                "change": p.get("change", ""),
                "predicts": p.get("predicts", ""),
                "basis": p.get("basis", ""),
                "planning_model": p.get("planning_model", ""),
                "execution_model": p.get("execution_model", ""),
                "plan": None,
                "iterations": [],   # list of {llm_response, tools, llm_usage}
                "verdict": None,
                "oracle": None,
                "knowledge_update": None,
            }
            summary["hypotheses"].append(current_h)
            current_iter = None
            tool_map = {}

        if k == "performer_plan" and current_h is not None:
            current_h["plan"] = {
                "current_state": p.get("current_state", ""),
                "steps": p.get("steps", []),
                "confidence": p.get("confidence", "?"),
            }

        # LLM usage signals a new react iteration starting
        if k == "llm_usage" and current_h is not None:
            role = p.get("role_tag") or p.get("role", "")
            if role == "performer":
                current_iter = {
                    "llm_text": "",       # assistant text response
                    "tools": [],
                    "llm_usage": {
                        "model": p.get("model", "").split("/")[-1],
                        "in": p.get("in", 0),
                        "out": p.get("out", 0),
                        "usd": p.get("usd", 0),
                    },
                }
                current_h["iterations"].append(current_iter)

        # Assistant message = model's reasoning text before/with tool calls
        if k == "assistant_message" and current_h is not None and current_iter is not None:
            role = p.get("role_tag") or p.get("role", "")
            if role == "performer":
                current_iter["llm_text"] = p.get("text", "") or ""

        if k == "tool_call" and current_h is not None:
            tc_id = p.get("tool_call_id", "") or p.get("tc_id", "")
            entry = {
                "tool": p.get("tool", "?"),
                "args": p.get("args", {}),
                "reason": p.get("reason", "") or (p.get("args", {}) or {}).get("reason", ""),
                "tc_id": tc_id,
                "output": None,
                "is_error": False,
            }
            tool_map[tc_id] = entry
            if current_iter is None:
                current_iter = {"llm_text": "", "tools": [], "llm_usage": None}
                current_h["iterations"].append(current_iter)
            current_iter["tools"].append(entry)

        if k == "tool_result" and current_h is not None:
            tc_id = p.get("tool_call_id", "")
            if tc_id in tool_map:
                tool_map[tc_id]["output"] = p.get("output", "") or ""
                tool_map[tc_id]["is_error"] = p.get("is_error", False)

        if k == "criterion_check" and current_h is not None:
            current_h["oracle"] = {
                "satisfied": p.get("satisfied"),
                "evidence": p.get("evidence", ""),
                "output": p.get("output", "") or "",
            }

        if k == "knowledge_update" and current_h is not None:
            current_h["knowledge_update"] = {
                "verdict": p.get("verdict", "?"),
                "reason": p.get("reason", ""),
                "findings_count": p.get("findings_count", 0),
            }
            current_h["verdict"] = p.get("verdict")

    return summary


# ── HTML ──────────────────────────────────────────────────────────────────

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     font-size:13px;background:#f0f2f5;color:#333;padding:16px}
a{color:#0550ae}
h1{font-size:18px;margin-bottom:8px}
h2{font-size:14px;margin:14px 0 6px;color:#555}
.card{background:white;border:1px solid #ddd;border-radius:8px;
      padding:14px;margin-bottom:12px}
.summary{display:flex;gap:20px;flex-wrap:wrap}
.stat{display:flex;flex-direction:column;gap:2px;min-width:80px}
.stat-label{font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:22px;font-weight:700}
.pass{color:#16a34a}.fail{color:#dc2626}.warn{color:#d97706}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;
       font-size:11px;font-weight:600;margin-left:4px}
.b-pass,.b-completed,.b-fulfilled{background:#dcfce7;color:#166534}
.b-fail,.b-give_up,.b-failed{background:#fee2e2;color:#991b1b}
.b-exhausted,.b-warn{background:#fff7ed;color:#9a3412}
.b-high{background:#dcfce7;color:#166534}
.b-medium{background:#fef9c3;color:#854d0e}
.b-low{background:#fee2e2;color:#991b1b}
.b-?{background:#f3f4f6;color:#6b7280}
/* Hypothesis */
.hyp{border:1px solid #ddd;border-radius:8px;margin-bottom:10px;overflow:hidden}
.hyp.h-pass{border-left:4px solid #16a34a}
.hyp.h-fail{border-left:4px solid #dc2626}
.hyp.h-warn{border-left:4px solid #d97706}
.hyp-hdr{padding:10px 14px;background:#fafafa;cursor:pointer;
         display:flex;align-items:center;gap:10px;user-select:none}
.hyp-hdr:hover{background:#f0f0f0}
.hyp-hdr::before{content:'▶';font-size:10px;color:#9ca3af;flex-shrink:0}
.collapsible.open~* .hyp-hdr::before,.hyp.expanded .hyp-hdr::before{content:'▼'}
.hyp-num{font-size:11px;font-weight:700;color:#888;text-transform:uppercase;
         letter-spacing:.5px;white-space:nowrap}
.hyp-loc{font-family:monospace;font-size:12px;color:#0550ae;flex:1;
         overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hyp-chg{font-size:12px;color:#555;flex:2;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap}
.hyp-body{padding:12px 14px;border-top:1px solid #eee}
.field{margin-bottom:10px}
.fl{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;
    letter-spacing:.5px;margin-bottom:3px}
.fv{color:#333;line-height:1.5}
/* Plan */
.plan-box{background:#f8faff;border:1px solid #c7d7f0;border-radius:6px;
          padding:8px 10px;margin:4px 0}
.plan-step{display:flex;gap:8px;padding:4px 0;border-bottom:1px solid #eef;
           font-size:12px;align-items:flex-start}
.plan-step:last-child{border-bottom:none}
.ps-icon{width:16px;flex-shrink:0;margin-top:1px}
.ps-action{flex:1}
.ps-rat{font-size:11px;color:#888}
/* React iterations */
.iter{border:1px solid #e5e7eb;border-radius:6px;margin-bottom:8px;overflow:hidden}
.iter-hdr{background:#f9fafb;padding:7px 12px;font-size:11px;font-weight:600;
          color:#666;border-bottom:1px solid #e5e7eb;cursor:pointer;
          display:flex;align-items:center;gap:8px}
.iter-hdr:hover{background:#f0f2f5}
.iter-body{padding:10px 12px}
/* LLM response */
.llm-resp{background:#fafff4;border:1px solid #d1fae5;border-radius:5px;
          padding:8px 10px;margin-bottom:8px;font-size:12px;
          white-space:pre-wrap;line-height:1.5;color:#333}
.llm-resp-lbl{font-size:10px;font-weight:700;color:#059669;
              text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
/* Tool calls */
.tc{background:#f8f9fa;border:1px solid #e9ecef;border-radius:5px;
    padding:8px 10px;margin-bottom:6px}
.tc:last-child{margin-bottom:0}
.tc-name{font-family:monospace;font-weight:700;font-size:13px;color:#6f42c1}
.tc-name.err{color:#dc2626}
.tc-args{font-family:monospace;font-size:11px;color:#555;margin:4px 0;
         display:flex;flex-direction:column;gap:3px}
.arg-row{display:flex;gap:6px;align-items:flex-start}
.arg-k{color:#888;flex-shrink:0;min-width:60px}
.arg-v{color:#0550ae;word-break:break-all}
.arg-v.old{color:#b45309}.arg-v.new{color:#16a34a}
.tc-reason{font-size:11px;color:#888;font-style:italic;margin-top:4px;
           padding-top:4px;border-top:1px solid #eee}
/* Output */
.out-box{font-family:monospace;font-size:11px;background:#1e1e2e;color:#cdd6f4;
         border-radius:4px;padding:8px 10px;margin-top:6px;
         white-space:pre-wrap;word-break:break-all;line-height:1.4;
         max-height:300px;overflow-y:auto}
.out-box.err-out{background:#2d1b1b;color:#fca5a5}
.out-lbl{font-size:10px;font-weight:700;color:#888;
         text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
/* Oracle */
.oracle{padding:10px;border-radius:6px;margin-top:8px;font-size:12px}
.oracle.ok{background:#f0fdf4;border:1px solid #86efac}
.oracle.bad{background:#fef2f2;border:1px solid #fca5a5}
.oracle-out{font-family:monospace;font-size:11px;background:#1e1e2e;color:#cdd6f4;
            border-radius:4px;padding:8px;margin-top:6px;
            white-space:pre-wrap;max-height:250px;overflow-y:auto}
/* Escalation */
.esc-chain{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px}
.esc-node{background:#e0e7ff;color:#3730a3;padding:2px 10px;
          border-radius:12px;font-size:12px;font-weight:600}
.esc-arrow{color:#9ca3af;font-size:14px}
/* Collapsible */
details>summary{cursor:pointer;color:#0550ae;font-size:11px;
                list-style:none;display:flex;align-items:center;gap:4px}
details>summary::before{content:'▶';font-size:9px;transition:.15s}
details[open]>summary::before{content:'▼'}
details>summary:hover{text-decoration:underline}
/* Toggle */
.tog{cursor:pointer;user-select:none}
.collapsible{display:none}
.open>.collapsible{display:block}
.iter-wrap .collapsible{display:none}
.iter-wrap.open .collapsible{display:block}
.hyp .hyp-hdr::before{content:'▶ ';font-size:10px;color:#9ca3af}
.hyp.open .hyp-hdr::before{content:'▼ ';color:#555}
.iter-hdr::before{content:'▶ ';font-size:9px;color:#9ca3af}
.iter-wrap.open .iter-hdr::before{content:'▼ ';color:#555}
"""

JS = """
function tog(id){
  var el=document.getElementById(id);
  el.classList.toggle('open');
}
"""

_uid = [0]
def uid():
    _uid[0] += 1
    return f"x{_uid[0]}"


def h(s):
    return html.escape(str(s) if s is not None else "")


def badge(text, extra_cls=""):
    t = str(text).lower().replace(" ", "_")
    return f'<span class="badge b-{h(t)} {h(extra_cls)}">{h(text)}</span>'


def render_arg(key, val, max_full=200):
    """Render one arg key=value with show-more for long values."""
    val = str(val)
    cls = "new" if key == "new_str" else ("old" if key == "old_str" else "")
    if len(val) <= max_full:
        return f'<div class="arg-row"><span class="arg-k">{h(key)}</span><span class="arg-v {cls}">{h(val)}</span></div>'
    short = val[:max_full]
    rest = val[max_full:]
    eid = uid()
    return (
        f'<div class="arg-row"><span class="arg-k">{h(key)}</span>'
        f'<span class="arg-v {cls}">{h(short)}'
        f'<details><summary>…{len(val)} chars total</summary>{h(rest)}</details>'
        f'</span></div>'
    )


def render_output(output, is_error, label="OUTPUT"):
    if not output:
        return ""
    cls = "out-box err-out" if is_error else "out-box"
    short = output[:1000]
    rest = output[1000:]
    eid = uid()
    more = (
        f'<details><summary>…{len(output)} chars total — show rest</summary>'
        f'<span style="color:#a6e3a1">{h(rest)}</span></details>'
    ) if rest else ""
    return f'<div class="out-lbl">{h(label)}</div><div class="{cls}">{h(short)}{more}</div>'


def render_iteration(it, idx):
    eid = uid()
    tools = it.get("tools", [])
    llm_text = it.get("llm_text", "")
    usage = it.get("llm_usage") or {}
    model = usage.get("model", "")
    tok_in = usage.get("in", 0)
    tok_out = usage.get("out", 0)
    usd = usage.get("usd", 0)

    # Header: show tool names + error/ok indicators
    tc_summary = " ".join(
        f'<span style="font-family:monospace;color:{"#dc2626" if tc.get("is_error") else "#6f42c1"}">'
        f'{h(tc.get("tool","?"))}</span>'
        for tc in tools
    )
    meta = f'<span style="color:#9ca3af;font-size:10px">{h(model)} · {tok_in}in/{tok_out}out · ${usd:.4f}</span>' if model else ""

    parts = [
        f'<div class="iter-wrap" id="{eid}">',
        f'<div class="iter-hdr tog" onclick="tog(\'{eid}\')">',
        f'<span style="color:#888;font-size:10px;font-weight:700">ITER {idx}</span>',
        f'<span>{tc_summary}</span>',
        f'<span style="flex:1"></span>',
        meta,
        f'</div>',
        f'<div class="collapsible"><div class="iter-body">',
    ]

    # LLM response text
    if llm_text and llm_text.strip():
        parts.append(
            f'<div class="llm-resp-lbl">Model reasoning</div>'
            f'<div class="llm-resp">{h(llm_text[:2000])}'
            + (f'<details><summary>…show full ({len(llm_text)} chars)</summary>{h(llm_text[2000:])}</details>' if len(llm_text) > 2000 else "")
            + f'</div>'
        )

    # Tool calls
    for tc in tools:
        tool = tc.get("tool", "?")
        is_err = tc.get("is_error", False)
        reason = tc.get("reason", "")
        args = tc.get("args", {}) or {}
        output = tc.get("output", "") or ""

        err_badge = ' <span style="font-size:10px;color:#dc2626;font-weight:700">ERROR</span>' if is_err else ""
        parts.append(f'<div class="tc">')
        parts.append(f'<span class="tc-name{"  err" if is_err else ""}">{h(tool)}</span>{err_badge}')

        if args:
            parts.append('<div class="tc-args">')
            # Don't show 'reason' in args (shown separately)
            for k, v in args.items():
                if k == "reason":
                    continue
                parts.append(render_arg(k, v))
            parts.append('</div>')

        if reason:
            parts.append(f'<div class="tc-reason">Reason: {h(reason[:300])}</div>')

        if output:
            parts.append(render_output(output, is_err))

        parts.append('</div>')

    parts.append('</div></div></div>')  # iter-body, collapsible, iter-wrap
    return "\n".join(parts)


def render_hypothesis(hyp, global_idx):
    n = hyp.get("n", global_idx)
    err = hyp.get("error")
    verdict = hyp.get("verdict") or ("error" if err else "?")
    oracle = hyp.get("oracle") or {}
    ku = hyp.get("knowledge_update") or {}

    v_lower = str(verdict).lower()
    h_cls = "h-pass" if v_lower in ("completed", "fulfilled") else \
            "h-warn" if v_lower in ("exhausted",) else "h-fail"

    eid = uid()
    loc = hyp.get("location", "") or (f"ERROR: {err}" if err else "")
    change = hyp.get("change", "")

    # Header — id on outer .hyp so CSS .hyp.open works
    parts = [
        f'<div class="hyp {h_cls}" id="{eid}">',
        f'<div class="hyp-hdr" onclick="tog(\'{eid}\')">',
        f'<span class="hyp-num">H{n}</span>',
        badge(verdict),
        f'<span class="hyp-loc">{h(loc)}</span>',
        f'<span class="hyp-chg">{h(change[:120])}</span>',
    ]

    # Model tags
    pm = hyp.get("planning_model", "")
    em = hyp.get("execution_model", "")
    if pm:
        parts.append(f'<span style="font-size:10px;color:#6b7280;white-space:nowrap">plan:{h(pm)}</span>')
    if em:
        parts.append(f'<span style="font-size:10px;color:#6b7280;white-space:nowrap">exec:{h(em)}</span>')

    parts.append(f'</div>')  # hyp-hdr
    parts.append(f'<div class="collapsible"><div class="hyp-body">')

    if err:
        parts.append(f'<div style="color:#dc2626;padding:8px;background:#fef2f2;border-radius:4px">{h(err)}</div>')
        parts.append('</div></div>')  # hyp-body + collapsible
        parts.append('</div>')        # hyp
        return "\n".join(parts)

    # Fields
    for label, val in [("Location", hyp.get("location","")),
                       ("Change", hyp.get("change","")),
                       ("Predicts", hyp.get("predicts","")),
                       ("Basis", hyp.get("basis",""))]:
        if val:
            parts.append(f'<div class="field"><div class="fl">{h(label)}</div>'
                         f'<div class="fv">{h(val)}</div></div>')

    # Performer plan (H2+)
    plan = hyp.get("plan")
    if plan:
        steps_html = ""
        for s in plan.get("steps", []):
            status = s.get("status", "pending")
            icon = ("✓" if status == "done" else "✗" if status == "failed" else "○")
            color = "#16a34a" if status == "done" else "#dc2626" if status == "failed" else "#9ca3af"
            steps_html += (
                f'<div class="plan-step">'
                f'<span class="ps-icon" style="color:{color}">{icon}</span>'
                f'<div><div class="ps-action">{h(s.get("action",""))}</div>'
                f'<div class="ps-rat">{h(s.get("rationale","")[:100])}</div></div>'
                f'</div>'
            )
        conf = plan.get("confidence", "?")
        state = plan.get("current_state", "")
        parts.append(
            f'<div class="field"><div class="fl">Performer Plan {badge(conf)}</div>'
            f'<div class="plan-box">'
            + (f'<div style="font-size:11px;color:#888;margin-bottom:6px"><em>State: {h(state)}</em></div>' if state else "")
            + f'{steps_html}</div></div>'
        )

    # React iterations
    iters = hyp.get("iterations", [])
    if iters:
        parts.append(f'<div class="field"><div class="fl">React Loop ({len(iters)} iterations)</div>')
        for i, it in enumerate(iters, 1):
            parts.append(render_iteration(it, i))
        parts.append('</div>')

    # Oracle result
    if oracle:
        sat = oracle.get("satisfied")
        ev = oracle.get("evidence", "")
        out = oracle.get("output", "")
        ok_cls = "oracle ok" if sat else "oracle bad"
        sat_badge = badge("PASS" if sat else "FAIL")
        parts.append(f'<div class="{ok_cls}">')
        parts.append(f'<strong>Oracle</strong> {sat_badge} &nbsp; {h(ev)}')
        if out:
            parts.append(f'<div class="oracle-out">{h(out[:2000])}'
                        + (f'<details><summary>…{len(out)} chars</summary>{h(out[2000:])}</details>' if len(out)>2000 else '')
                        + '</div>')
        parts.append('</div>')

    # Knowledge verdict
    if ku:
        kv = ku.get("verdict","?")
        kr = ku.get("reason","")
        kf = ku.get("findings_count",0)
        kv_cls = "oracle ok" if kv in ("completed","fulfilled") else "oracle bad"
        parts.append(
            f'<div class="{kv_cls}" style="margin-top:6px">'
            f'<strong>Verdict</strong> {badge(kv)} &nbsp;'
            f'<span style="color:#888">findings:{kf}</span> &nbsp;'
            f'{h(kr[:200])}'
            f'</div>'
        )

    parts.append('</div></div></div>')  # hyp-body, collapsible, hyp
    return "\n".join(parts)


def generate_html(jsonl_path: str, results: dict) -> str:
    events, resolved_path = load_trace(jsonl_path) if isinstance(jsonl_path, str) else (jsonl_path, "")
    data = parse_trace(events)

    is_resolved = results.get("is_resolved")
    task_id = results.get("task_id", Path(jsonl_path).parent.parent.parent.name)
    status_cls = "pass" if is_resolved else "fail"
    status_text = "PASS" if is_resolved else "FAIL"

    usd = data["llm_cost"]["usd"]
    tok_in = data["llm_cost"]["tok_in"]
    tok_out = data["llm_cost"]["tok_out"]
    n_hyp = len(data["hypotheses"])
    by_role = data["llm_cost"]["by_role"]

    # State flow
    states = data["contract_state"]
    state_chips = " → ".join(
        f'<span style="background:#e0e7ff;color:#3730a3;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{s["to"]}</span>'
        for s in states
    )

    # Escalation chain
    escs = data["escalations"]
    if escs:
        esc_nodes = ['<span class="esc-node">flash</span>']
        for e in escs:
            esc_nodes.append('<span class="esc-arrow">→</span>')
            esc_nodes.append(f'<span class="esc-node">{h(e["to"])}</span>')
        esc_html = f'<div class="field"><div class="fl">Execution escalation chain</div><div class="esc-chain">{" ".join(esc_nodes)}</div></div>'
    else:
        esc_html = ""

    # Cost by role
    role_rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#555">{h(r)}</td>'
        f'<td style="text-align:right;font-family:monospace;color:#333">${v:.4f}</td></tr>'
        for r, v in sorted(by_role.items(), key=lambda x: -x[1])
    )

    obligation = (results.get("instruction") or "")[:600]

    hyps_html = "\n".join(
        render_hypothesis(hyp, i + 1)
        for i, hyp in enumerate(data["hypotheses"])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CAT Trace — {h(task_id)}</title>
<style>{CSS}</style>
<script>{JS}</script>
</head>
<body>
<h1>CAT v3 Trace</h1>
<h2 style="font-size:15px;color:#444;font-weight:500;margin-bottom:12px">{h(task_id)}</h2>

<div class="card summary">
  <div class="stat">
    <span class="stat-label">Result</span>
    <span class="stat-value {status_cls}">{status_text}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Hypotheses</span>
    <span class="stat-value">{n_hyp}</span>
  </div>
  <div class="stat">
    <span class="stat-label">Tokens</span>
    <span class="stat-value">{(tok_in+tok_out)//1000}K</span>
  </div>
  <div class="stat">
    <span class="stat-label">Cost</span>
    <span class="stat-value">${usd:.4f}</span>
  </div>
  <div class="stat" style="flex:1">
    <span class="stat-label">Contract state</span>
    <div style="margin-top:6px">{state_chips}</div>
  </div>
</div>

<div class="card" style="display:flex;gap:24px;flex-wrap:wrap">
  <div>
    <div style="font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Cost by role</div>
    <table style="border-collapse:collapse;font-size:12px">{role_rows}</table>
  </div>
  {f'<div style="flex:1">{esc_html}</div>' if esc_html else ""}
</div>

{'<div class="card"><div style="font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Obligation (task description)</div><div style="font-size:12px;color:#444;white-space:pre-wrap;line-height:1.6">' + h(obligation) + '</div></div>' if obligation else ''}

<div style="font-size:11px;color:#888;margin-bottom:8px">
  Click a hypothesis header to expand · Click an iteration to show prompt + tool details
</div>

{hyps_html}

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/trace_report.py <path.jsonl|run_dir> [output.html]")
        sys.exit(1)

    input_path = sys.argv[1]
    p = Path(input_path)
    if p.is_dir():
        candidates = sorted(p.glob("**/agent-logs/cat-tb-*.jsonl"))
        if not candidates:
            sys.exit(f"No JSONL found under {input_path}")
        jsonl_path = str(candidates[-1])
    else:
        jsonl_path = input_path

    results = find_results_json(jsonl_path)

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
    else:
        base = Path(jsonl_path).stem
        out_path = str(Path(jsonl_path).parent / f"{base}.html")

    events, _ = load_trace(jsonl_path)
    data = parse_trace(events)
    html_content = generate_html(jsonl_path, results)
    Path(out_path).write_text(html_content, encoding="utf-8")
    print(f"Report written to: {out_path}")
    print(f"Open with: open {out_path}")
