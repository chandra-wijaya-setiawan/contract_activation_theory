#!/usr/bin/env bash
# Launch full SWE-bench Verified (500 tasks) with CAT R27.
#
# Usage:
#   scripts/launch_swe500.sh [--n-attempts N] [--concurrency N] [--run-id slug]
#
# Defaults: n_attempts=1, concurrency=10, run-id=r27-swe500
# Reads dataset from $HOME/.cache/terminal-bench/swebench-verified/head
# Output → data/runs/swebench/YYYY-MM-DD/HH-MM-SS-<run-id>/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

N_ATTEMPTS=1
CONCURRENCY=10
RUN_ID="r27-swe500"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --n-attempts)  N_ATTEMPTS="$2"; shift 2;;
    --concurrency) CONCURRENCY="$2"; shift 2;;
    --run-id)      RUN_ID="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

DATASET="${TB_DATASET_PATH:-$HOME/.cache/terminal-bench/swebench-verified/head}"

# Gather task IDs from the dataset dir
TASK_ARGS=()
while IFS= read -r task; do
  TASK_ARGS+=(--task-id "$task")
done < <(ls "$DATASET" | sort)

N_TASKS=$(( ${#TASK_ARGS[@]} / 2 ))
echo "── swe500 launch summary ──"
echo "  run-id:       $RUN_ID"
echo "  dataset:      $DATASET"
echo "  n_tasks:      $N_TASKS"
echo "  n_attempts:   $N_ATTEMPTS"
echo "  concurrency:  $CONCURRENCY"
echo "  total trials: $(( N_TASKS * N_ATTEMPTS ))"
echo

# Hand off to run_bench.sh — it sources .env (API keys), prunes orphan
# docker networks, and lays out the structured output dir.
DOCKER_HOST=ssh://cpubox exec scripts/run_bench.sh swebench \
  --run-id "$RUN_ID" \
  --concurrency "$CONCURRENCY" \
  --n-attempts "$N_ATTEMPTS" \
  "${TASK_ARGS[@]}"
