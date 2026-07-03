"""Pull narrative excerpts from a trace JSONL for the comparison report.

Usage:
    python scripts/extract_excerpts.py data/runs/<run-dir>

Prints:
    - planner decomposition (CAT only)
    - tool-call sequence (tool + args summary)
    - criterion checks with verdicts
    - final state
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def main(run_dir: Path) -> int:
    jsonls = list(run_dir.glob("*.jsonl"))
    if not jsonls:
        print(f"no jsonl in {run_dir}", file=sys.stderr)
        return 1
    events = [json.loads(l) for l in jsonls[0].open(encoding="utf-8") if l.strip()]
    print(f"# {run_dir.name}")
    print(f"({len(events)} events)\n")

    # Decompose
    for e in events:
        if e["kind"] == "contract_decompose":
            steps = e["payload"].get("steps", [])
            print("## Planner decomposition")
            for s in steps:
                print(f"  - {s['id']}: {s['description']}")
            print()
            break

    # Tool sequence
    print("## Tool sequence")
    i = 0
    for e in events:
        if e["kind"] == "tool_call":
            i += 1
            t = e["payload"]
            args = t.get("args", {})
            arg_summary = ""
            if t.get("tool") == "bash":
                arg_summary = f"command={args.get('command', '')[:80]!r}"
            elif t.get("tool") == "write":
                arg_summary = f"path={args.get('path')!r} bytes={len(args.get('content', ''))}"
            elif t.get("tool") == "read":
                arg_summary = f"path={args.get('path')!r}"
            elif t.get("tool") == "list":
                arg_summary = f"path={args.get('path', '.')!r}"
            else:
                arg_summary = json.dumps(args)[:80]
            print(f"  {i:2d}. {t.get('tool')}: {arg_summary}")
    print()

    # Criterion checks (CAT only)
    cc = [e for e in events if e["kind"] == "criterion_check"]
    if cc:
        print(f"## Criterion / judge checks ({len(cc)})")
        for c in cc:
            p = c["payload"]
            tag = "judge" if p.get("judge") else "deterministic"
            verdict = p.get("satisfied", p.get("complete"))
            label = (p.get("description") or p.get("step_id") or "")[:80]
            reason = (p.get("evidence") or p.get("reason") or "")[:120]
            print(f"  - [{tag}] iter={p.get('iteration')} {label!r} -> {verdict}  ({reason})")
        print()

    # State transitions
    st = [e for e in events if e["kind"] == "contract_state" and not e["payload"].get("rejected")]
    if st:
        print("## State transitions")
        for s in st:
            p = s["payload"]
            print(f"  - {p.get('from')} -> {p.get('to')}  ({p.get('reason')})")
        print()

    # Final
    last = events[-1]
    print(f"## Final event: {last['kind']}")
    print(f"  payload: {json.dumps(last['payload'], indent=2)[:600]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
