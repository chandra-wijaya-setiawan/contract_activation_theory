"""Compare swe10 run results: v12 (baseline), v13 (oracle fix), v14 (all fixes), react."""
import json
from pathlib import Path

BASE = Path("data/tb-runs/swe10")
REACT = BASE / "swe10-react"

RUNS = [
    ("react", REACT),
    ("v12 (old oracle)", BASE / "swe10-cat-v12"),
    ("v13 (oracle+init)", BASE / "swe10-cat-v13"),
    ("v14 (all fixes)", BASE / "swe10-cat-v14"),
]

TASK_NAMES = [
    "psf__requests-2931",
    "astropy__astropy-8872",
    "pytest-dev__pytest-6202",
    "django__django-12039",
    "sympy__sympy-12489",
    "scikit-learn__scikit-learn-14141",
    "matplotlib__matplotlib-26342",
    "pydata__xarray-4966",
    "sphinx-doc__sphinx-10323",
    "sympy__sympy-15875",
]

def load_results(run_dir: Path) -> dict:
    rj = run_dir / "results.json"
    if not rj.exists():
        return {}
    d = json.loads(rj.read_text())
    out = {}
    for r in d.get("results", []):
        task_id = r["task_id"]
        is_resolved = r.get("is_resolved")
        failure_mode = r.get("failure_mode", "")
        if is_resolved is True:
            out[task_id] = ("PASS", r.get("total_input_tokens") or 0)
        elif failure_mode == "parse_error":
            out[task_id] = ("ERR?", r.get("total_input_tokens") or 0)
        elif is_resolved is False:
            out[task_id] = ("FAIL", r.get("total_input_tokens") or 0)
        else:
            out[task_id] = ("??", 0)
    return out

data = {}
for name, run_dir in RUNS:
    data[name] = load_results(run_dir)

# Header
print(f"\n{'Task':<40}", end="")
for name, _ in RUNS:
    print(f"  {name[:15]:>15}", end="")
print()
print("-" * (40 + 18 * len(RUNS)))

# Per task
for task in TASK_NAMES:
    short = task.split("__")[-1][:38]
    print(f"{short:<40}", end="")
    for name, _ in RUNS:
        result, tokens = data[name].get(task, ("...", 0))
        print(f"  {result:>15}", end="")
    print()

print("-" * (40 + 18 * len(RUNS)))

# Summary row
print(f"{'PASS count':<40}", end="")
for name, _ in RUNS:
    n = sum(1 for v in data[name].values() if v[0] == "PASS")
    total = len(data[name])
    print(f"  {n}/{total:>13}", end="")
print()

# Token efficiency
print(f"\n{'Token efficiency (PASS tasks)':}")
for name, _ in RUNS:
    passes = [(k, v[1]) for k, v in data[name].items() if v[0] == "PASS"]
    if passes:
        total_tok = sum(t for _, t in passes)
        per_solve = total_tok // len(passes) if passes else 0
        print(f"  {name}: {len(passes)} PASS, {total_tok:,} total tokens, {per_solve:,} per solved task")
