#!/usr/bin/env bash
# scripts/run_bench.sh — launch a benchmark run with the structured output layout (PRD 1.6.4).
#
# Output lands at:
#   data/runs/<bench>/<YYYY-MM-DD>/<HH-MM-SS>-<run-id>/
# with manifest.json written at start and KPI report + INDEX regenerated on exit.
#
# Usage:
#   scripts/run_bench.sh <bench> --run-id <id> --task-id <t1> [--task-id <t2> ...] \
#       [--agent-import-path benchmarks.swe_bench:TbSweAgent] \
#       [--concurrency 3] \
#       [--no-llm-attribution]
#
# Example:
#   scripts/run_bench.sh swebench --run-id smoke-sklearn \
#       --task-id scikit-learn__scikit-learn-14141 \
#       --concurrency 1
#
# Conventions:
#   <bench>           swebench | termbench | taubench | synthetic | webarena | gaia
#   <id>              short slug for this run (a-z, 0-9, dashes)
#   default agent     benchmarks.swe_bench:TbSweAgent
#   default dataset   ~/.cache/terminal-bench/swebench-verified/head (overridable via $TB_DATASET_PATH)
#   default model     openrouter/deepseek/deepseek-v4-flash

set -euo pipefail

# ── Argument parsing ─────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <bench> --run-id <id> --task-id <t> [--task-id <t> ...] [opts]" >&2
  exit 2
fi

BENCH="$1"; shift
RUN_ID=""
TASK_IDS=()
AGENT_PATH="benchmarks.swe_bench:TbSweAgent"
CONCURRENCY=3
NO_LLM=0
DATASET_PATH="${TB_DATASET_PATH:-$HOME/.cache/terminal-bench/swebench-verified/head}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)        RUN_ID="$2"; shift 2;;
    --task-id)       TASK_IDS+=("$2"); shift 2;;
    --agent-import-path|--agent) AGENT_PATH="$2"; shift 2;;
    --concurrency)   CONCURRENCY="$2"; shift 2;;
    --dataset-path)  DATASET_PATH="$2"; shift 2;;
    --no-llm-attribution) NO_LLM=1; shift;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

if [[ -z "$RUN_ID" ]]; then
  echo "ERROR: --run-id required" >&2; exit 2
fi
if [[ ${#TASK_IDS[@]} -eq 0 ]]; then
  echo "ERROR: at least one --task-id required" >&2; exit 2
fi

# ── Compute path ─────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOW_DATE=$(date -u +%Y-%m-%d)
NOW_TIME=$(date -u +%H-%M-%S)
RUN_DIR="$REPO_ROOT/data/runs/$BENCH/$NOW_DATE/${NOW_TIME}-${RUN_ID}"
mkdir -p "$RUN_DIR"

# ── Write initial manifest ───────────────────────────────────────────────────

GIT_SHA=$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
GIT_BRANCH=$(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

python3 -c "
import json, sys
manifest = {
    'bench': '$BENCH',
    'run_id': '$RUN_ID',
    'started_at': '$STARTED_AT',
    'ended_at': None,
    'git_sha': '$GIT_SHA',
    'git_branch': '$GIT_BRANCH',
    'agent_import_path': '$AGENT_PATH',
    'task_ids': $(python3 -c "import json,sys;print(json.dumps(${TASK_IDS[@]@Q} if isinstance(${TASK_IDS[@]@Q}, list) else list(${TASK_IDS[@]@Q}))" 2>/dev/null || echo "[]"),
    'n_concurrent_trials': $CONCURRENCY,
    'dataset_path': '$DATASET_PATH',
    'pass_rate': None,
    'n_resolved': None,
    'n_tasks': ${#TASK_IDS[@]},
}
import sys
print(json.dumps(manifest, indent=2))
" > "$RUN_DIR/manifest.json" 2>/dev/null || \
python3 <<EOF > "$RUN_DIR/manifest.json"
import json
manifest = {
    "bench": "$BENCH",
    "run_id": "$RUN_ID",
    "started_at": "$STARTED_AT",
    "ended_at": None,
    "git_sha": "$GIT_SHA",
    "git_branch": "$GIT_BRANCH",
    "agent_import_path": "$AGENT_PATH",
    "task_ids": [${TASK_IDS[@]/#/$'\n    "'}${TASK_IDS[@]/%/$'",'}],
    "n_concurrent_trials": $CONCURRENCY,
    "dataset_path": "$DATASET_PATH",
    "n_tasks": ${#TASK_IDS[@]},
}
print(json.dumps(manifest, indent=2))
EOF

# Robust manifest writer (the heredoc above can be flaky; use python with argv):
python3 - "$RUN_DIR/manifest.json" "$BENCH" "$RUN_ID" "$STARTED_AT" "$GIT_SHA" "$GIT_BRANCH" \
        "$AGENT_PATH" "$CONCURRENCY" "$DATASET_PATH" "${TASK_IDS[@]}" <<'PYEOF'
import json, sys
out_path, bench, run_id, started_at, git_sha, git_branch, agent_path, concurrency, dataset_path, *task_ids = sys.argv[1:]
manifest = {
    "bench": bench,
    "run_id": run_id,
    "started_at": started_at,
    "ended_at": None,
    "git_sha": git_sha,
    "git_branch": git_branch,
    "agent_import_path": agent_path,
    "n_concurrent_trials": int(concurrency),
    "dataset_path": dataset_path,
    "task_ids": task_ids,
    "n_tasks": len(task_ids),
    "n_resolved": None,
    "pass_rate": None,
}
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2)
PYEOF

echo "── CAT run launcher ─────────────────────────────────────────────"
echo "  bench:        $BENCH"
echo "  run_id:       $RUN_ID"
echo "  tasks:        ${#TASK_IDS[@]}"
echo "  concurrency:  $CONCURRENCY"
echo "  output:       $RUN_DIR"
echo "  agent:        $AGENT_PATH"
echo "  git:          $GIT_SHA  ($GIT_BRANCH)"
echo "─────────────────────────────────────────────────────────────────"

# ── Source .env if present ──────────────────────────────────────────────────

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; source "$REPO_ROOT/.env"; set +a
fi

# Default to the remote docker daemon if running on this dev box (cpubox).
# The Python docker SDK reads DOCKER_HOST env, not the CLI context — so set it.
: "${DOCKER_HOST:=ssh://cpubox}"
export DOCKER_HOST
echo "  docker:       $DOCKER_HOST"

# ── Build & run tb runs create ──────────────────────────────────────────────

TB_ARGS=(
  --dataset-path "$DATASET_PATH"
  --agent-import-path "$AGENT_PATH"
  --n-attempts 1
  --output-path "$RUN_DIR"
  --run-id "$RUN_ID"
  --n-concurrent-trials "$CONCURRENCY"
)
for t in "${TASK_IDS[@]}"; do
  TB_ARGS+=(--task-id "$t")
done

set +e
tb runs create "${TB_ARGS[@]}" 2>&1 | tee "$RUN_DIR/launcher.log"
TB_RC=$?
set -e

# ── Backfill manifest + emit report ─────────────────────────────────────────

ENDED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# tb runs writes results.json + a sub-run directory keyed on RUN_ID. Promote
# the run subdirectory contents to the launcher's RUN_DIR for a cleaner layout.
INNER="$RUN_DIR/$RUN_ID"
if [[ -d "$INNER" ]]; then
  # Move all contents up one level, then remove the now-empty inner dir.
  shopt -s dotglob nullglob
  mv "$INNER"/* "$RUN_DIR/" 2>/dev/null || true
  rmdir "$INNER" 2>/dev/null || true
  shopt -u dotglob nullglob
fi

# Compute pass_rate if results.json exists
PASS_RATE_PY=$(python3 - "$RUN_DIR/results.json" <<'PYEOF'
import json, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print("null,null,null")
    sys.exit(0)
r = json.load(open(p))
results = r.get("results", [])
n = len(results)
res = sum(1 for t in results if t.get("is_resolved"))
rate = (res / n) if n else 0.0
print(f"{res},{n},{rate:.4f}")
PYEOF
)
IFS=',' read -r N_RESOLVED N_TASKS PASS_RATE <<< "$PASS_RATE_PY"

# Update manifest with end_at + pass_rate
python3 - "$RUN_DIR/manifest.json" "$ENDED_AT" "$N_RESOLVED" "$N_TASKS" "$PASS_RATE" <<'PYEOF'
import json, sys
out_path, ended_at, n_res, n_tasks, rate = sys.argv[1:]
with open(out_path) as f:
    m = json.load(f)
m["ended_at"] = ended_at
m["n_resolved"] = None if n_res == "null" else int(n_res)
m["n_tasks_actual"] = None if n_tasks == "null" else int(n_tasks)
m["pass_rate"] = None if rate == "null" else float(rate)
with open(out_path, "w") as f:
    json.dump(m, f, indent=2)
PYEOF

# Emit KPI report (HTML + JSON + per-task attribution)
REPORT_FLAGS=()
if [[ $NO_LLM -eq 1 ]]; then
  REPORT_FLAGS+=(--no-llm)
fi
if [[ -f "$RUN_DIR/results.json" ]]; then
  echo ""
  echo "── Generating KPI report (this may take a moment for LLM attribution)"
  cd "$REPO_ROOT" && python3 -m cat.run_report "$RUN_DIR" "${REPORT_FLAGS[@]}" || true

  # 1.7 Phase A.1 — corpus writeback. Ingest this run's traces so the
  # LIBRARIAN has more samples next time. Cold-start safe.
  CORPUS_DB="${CAT_CORPUS_DB:-$REPO_ROOT/data/corpus/cat_corpus.db}"
  echo ""
  echo "── Writing samples to corpus: $CORPUS_DB"
  cd "$REPO_ROOT" && python3 scripts/seed_corpus.py "$CORPUS_DB" "$RUN_DIR" || true
fi

# Regenerate top-level INDEX
echo ""
echo "── Regenerating INDEX.html"
cd "$REPO_ROOT" && python3 -m cat.run_report --index data/runs/ || true

echo ""
echo "── DONE ($ENDED_AT)"
echo "  $RUN_DIR/_kpi_report.html"
echo "  data/runs/INDEX.html"
exit $TB_RC
