"""Compare swe30 CAT v14 vs react baseline."""
import json
from pathlib import Path

BASE = Path("data/tb-runs/swe30")

# Load react baseline (combined from two partial runs)
react_results = {}
for run in ["swe30-react", "swe30-react-2", "swe30-react-cont"]:
    rp = BASE / run / "results.json"
    if rp.exists():
        d = json.loads(rp.read_text())
        for r in d.get("results", []):
            tid = r["task_id"]
            if tid not in react_results:
                react_results[tid] = r.get("is_resolved", False) or False

# Load CAT old (pre-fix)
cat_old = {}
rp = BASE / "swe30-cat" / "results.json"
if rp.exists():
    d = json.loads(rp.read_text())
    for r in d.get("results", []):
        tid = r["task_id"]
        cat_old[tid] = r.get("is_resolved", False) or False

# Load CAT v14
cat_v14 = {}
rp = BASE / "swe30-cat-v14" / "results.json"
if rp.exists():
    d = json.loads(rp.read_text())
    for r in d.get("results", []):
        tid = r["task_id"]
        fm = r.get("failure_mode", "")
        cat_v14[tid] = {
            "resolved": r.get("is_resolved") or False,
            "parse_error": fm == "parse_error",
            "tokens_in": r.get("total_input_tokens") or 0,
        }

all_tasks = sorted(set(list(react_results.keys()) + list(cat_old.keys())))

print(f"\n{'Task':<42} {'React':>8} {'CAT v0':>8} {'CAT v14':>9} {'Tokens v14':>12}")
print("-" * 82)

react_pass = cat_old_pass = cat_v14_pass = cat_v14_tokens = 0
for tid in all_tasks:
    short = tid.split("__")[-1][:40]
    r = "PASS" if react_results.get(tid) else "FAIL"
    c0 = "PASS" if cat_old.get(tid) else "FAIL"
    v14_info = cat_v14.get(tid, {})
    if v14_info.get("parse_error"):
        c14 = "ERR?"
    elif v14_info.get("resolved"):
        c14 = "PASS"
        cat_v14_pass += 1
        cat_v14_tokens += v14_info["tokens_in"]
    elif tid in cat_v14:
        c14 = "FAIL"
    else:
        c14 = "..."
    if react_results.get(tid): react_pass += 1
    if cat_old.get(tid): cat_old_pass += 1
    tok = f"{v14_info.get('tokens_in',0)//1000}K" if tid in cat_v14 else ""
    print(f"{short:<42} {r:>8} {c0:>8} {c14:>9} {tok:>12}")

print("-" * 82)
n = len(all_tasks)
print(f"{'TOTALS':<42} {react_pass:>4}/{n:<3}  {cat_old_pass:>4}/{n:<3}  {cat_v14_pass:>4}/{n:<3}")
print(f"\nReact: {react_pass}/{n} = {react_pass/n*100:.1f}%")
print(f"CAT v0: {cat_old_pass}/{n} = {cat_old_pass/n*100:.1f}%")
print(f"CAT v14: {cat_v14_pass}/{len(cat_v14)} done so far")
if cat_v14_pass:
    print(f"CAT v14 token efficiency: {cat_v14_tokens//1000}K total, {cat_v14_tokens//cat_v14_pass//1000}K/solved")
