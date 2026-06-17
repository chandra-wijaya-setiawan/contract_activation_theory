"""Benchmark runner.

Usage:
    python -m cli.bench --bench swe-bench --tasks <list> --model <model>
    python -m cli.bench --bench terminal-bench --tasks <list> --model <model> --v3
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="CAT benchmark runner")
    parser.add_argument("--bench", required=True, choices=["swe-bench", "terminal-bench", "synthetic"])
    parser.add_argument("--tasks", required=True, help="Comma-separated task IDs or 'all'")
    parser.add_argument("--model", required=True, help="Model ID")
    parser.add_argument("--v3", action="store_true", help="Use v3 hypothesis loop")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--n-attempts", type=int, default=1)
    args = parser.parse_args()
    ...


if __name__ == "__main__":
    main()
