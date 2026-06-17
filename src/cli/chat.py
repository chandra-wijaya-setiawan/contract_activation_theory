"""Interactive chat REPL.

Usage:
    python -m cli.chat --model openrouter/deepseek/deepseek-v4-flash
    python -m cli.chat --model ... --workdir /path/to/project
    python -m cli.chat --model ... --checkpoint git

Flow:
    1. Load model + workdir
    2. Print banner (model, workdir, available tools)
    3. REPL loop:
       a. Read user input (task description)
       b. Build Contract(obligation=user_input, criteria=[...])
       c. Run HypothesisLoopActivator.run(contract, state)
       d. Print result (PASS/FAIL, iterations, cost, key findings)
       e. Ask: "Another task? (y/n)"
    4. Print session summary
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="CAT interactive chat")
    parser.add_argument("--model", required=True, help="Model ID (e.g., openrouter/deepseek/deepseek-v4-flash)")
    parser.add_argument("--workdir", default=".", help="Working directory")
    parser.add_argument("--checkpoint", choices=["git", "none"], default="none")
    args = parser.parse_args()
    ...


if __name__ == "__main__":
    main()
