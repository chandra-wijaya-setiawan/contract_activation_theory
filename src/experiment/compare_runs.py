"""Side-by-side compare of multiple runs on the same task set.

Usage:
    python scripts/compare_runs.py --out compare_report.html \\
        data/runs/swebench/2026-05-27/_v1.5-final  \\
        data/runs/swebench/2026-05-28/07-23-59-swe10-v1.6.6

Produces an HTML table: rows = tasks (union of all runs), columns = runs.
Each cell is the verdict (PASS/FAIL/missing). Headers show per-run summary.
Useful for tracking solve-rate progression across infrastructure changes.
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path


def _load(run_dir: Path) -> dict:
    rp = run_dir / "results.json"
    if not rp.exists():
        return {"results": [], "label": run_dir.name}
    data = json.loads(rp.read_text())
    # Pick a short label
    mp = run_dir / "manifest.json"
    label = run_dir.name
    if mp.exists():
        try:
            m = json.loads(mp.read_text())
            label = m.get("run_id") or label
        except Exception:
            pass
    data["label"] = label
    data["dir"] = str(run_dir)
    return data


def render(runs: list[dict], out_path: Path) -> None:
    # Union of task IDs across runs
    all_tasks = sorted({t["task_id"] for r in runs for t in r.get("results", [])})

    # Per-run summary
    summaries = []
    for r in runs:
        n = len(r.get("results", []))
        passed = sum(1 for t in r["results"] if t.get("is_resolved"))
        summaries.append({
            "label": r["label"],
            "dir": r.get("dir", ""),
            "n": n,
            "passed": passed,
            "rate": (passed / n) if n else 0.0,
        })

    # Lookup: (label, tid) → result dict
    lookup: dict[tuple[str, str], dict] = {}
    for r in runs:
        for t in r["results"]:
            lookup[(r["label"], t["task_id"])] = t

    def _short(tid: str) -> str:
        return tid.split("__")[-1] if "__" in tid else tid

    parts = ['''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>CAT run comparison</title>
<style>
  body { font-family: ui-monospace, monospace; max-width: 1280px; margin: 20px auto;
         padding: 0 16px; background: #fafafa; color: #1a1a1a; }
  h1 { font-size: 20px; }
  table { width: 100%; border-collapse: collapse; background: white;
          border: 1px solid #e5e5e5; border-radius: 4px; font-size: 12px; }
  th { background: #f5f5f5; padding: 8px 10px; text-align: left;
       font-weight: 600; border-bottom: 1px solid #e5e5e5; }
  td { padding: 6px 10px; border-bottom: 1px solid #f0f0f0; }
  td.pass { background: #d1fae5; color: #065f46; font-weight: 600; text-align: center; }
  td.fail { background: #fee2e2; color: #991b1b; font-weight: 600; text-align: center; }
  td.infra { background: #fef3c7; color: #92400e; font-weight: 600; text-align: center; }
  td.miss { background: #f3f4f6; color: #6b7280; text-align: center; }
  .summary-row { background: #eff6ff; font-weight: 600; }
  .meta { color: #888; font-size: 11px; margin: 6px 0 16px; }
</style></head><body>''']
    parts.append(f"<h1>CAT runs — side-by-side</h1>")
    parts.append(
        f'<div class="meta">Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")} · '
        f'{len(all_tasks)} unique tasks · {len(runs)} runs</div>'
    )

    # Header summary table
    parts.append("<table style='margin-bottom:24px;'><tr><th>run</th><th>passed</th><th>rate</th><th>path</th></tr>")
    for s in summaries:
        bar = ""
        if s["n"]:
            pct = round(100 * s["rate"])
            bar = (
                f'<div style="background:#e5e5e5;height:6px;width:120px;border-radius:3px;'
                f'overflow:hidden;display:inline-block;margin-left:8px;"><div style="background:#3b82f6;'
                f'height:100%;width:{pct}%;"></div></div>'
            )
        parts.append(
            f"<tr><td>{html.escape(s['label'])}</td>"
            f"<td>{s['passed']} / {s['n']}</td>"
            f"<td>{s['rate']*100:.0f}% {bar}</td>"
            f"<td style='color:#888;'>{html.escape(s['dir'])}</td></tr>"
        )
    parts.append("</table>")

    # Main grid
    parts.append("<table><tr><th>task</th>")
    for s in summaries:
        parts.append(f"<th>{html.escape(s['label'])}</th>")
    parts.append("</tr>")

    for tid in all_tasks:
        parts.append(f"<tr><td>{html.escape(_short(tid))}</td>")
        for s in summaries:
            row = lookup.get((s["label"], tid))
            if row is None:
                cell = '<td class="miss">—</td>'
            elif row.get("is_resolved"):
                cell = '<td class="pass">PASS</td>'
            elif row.get("failure_mode") == "unknown_agent_error":
                cell = '<td class="infra">INFRA</td>'
            else:
                cell = '<td class="fail">FAIL</td>'
            parts.append(cell)
        parts.append("</tr>")
    parts.append("</table>")

    # Win/loss diff (if exactly 2 runs)
    if len(runs) == 2:
        a, b = summaries[0], summaries[1]
        deltas = {"flipped_to_pass": [], "regressed_to_fail": [], "same_pass": [], "same_fail": []}
        for tid in all_tasks:
            ra = lookup.get((a["label"], tid))
            rb = lookup.get((b["label"], tid))
            if ra is None or rb is None:
                continue
            pa = bool(ra.get("is_resolved"))
            pb = bool(rb.get("is_resolved"))
            if pa and pb:
                deltas["same_pass"].append(tid)
            elif not pa and not pb:
                deltas["same_fail"].append(tid)
            elif pb and not pa:
                deltas["flipped_to_pass"].append(tid)
            else:
                deltas["regressed_to_fail"].append(tid)
        parts.append("<h2>Diff (left → right)</h2>")
        parts.append("<table>")
        for k in ("flipped_to_pass", "regressed_to_fail", "same_pass", "same_fail"):
            tids = deltas[k]
            color = {
                "flipped_to_pass": "#0a7c2f",
                "regressed_to_fail": "#c0392b",
                "same_pass": "#065f46",
                "same_fail": "#991b1b",
            }[k]
            parts.append(
                f"<tr><td style='color:{color};font-weight:600;width:200px;'>{k} ({len(tids)})</td>"
                f"<td>{html.escape(', '.join(_short(t) for t in tids)) or '(none)'}</td></tr>"
            )
        parts.append("</table>")

    parts.append("</body></html>")
    out_path.write_text("\n".join(parts))
    print(f"Wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/runs/_compare.html"))
    args = ap.parse_args()
    runs = [_load(d) for d in args.run_dirs]
    render(runs, args.out)
    # Print headline
    print()
    for r in runs:
        n = len(r.get("results", []))
        passed = sum(1 for t in r["results"] if t.get("is_resolved"))
        print(f"  {r['label']:40s}  {passed}/{n}  ({100*passed/max(n,1):.0f}%)")


if __name__ == "__main__":
    main()
