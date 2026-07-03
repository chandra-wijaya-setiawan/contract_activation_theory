"""CLI trace viewer — compact human-readable decision trace.

Usage:
    python -m tooling.trace.view <path/to/run.jsonl>
    python -m tooling.trace.view <path/to/run.jsonl> --full
"""
from __future__ import annotations

import argparse


def main():
    parser = argparse.ArgumentParser(description="View CAT trace")
    parser.add_argument("trace", help="Path to run.jsonl")
    parser.add_argument("--full", action="store_true", help="Full tool output")
    parser.add_argument("--width", type=int, default=100)
    args = parser.parse_args()  # noqa: F841
    ...


if __name__ == "__main__":
    main()
