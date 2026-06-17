"""Report generator CLI.

Usage:
    python -m cli.report <run_dir>
"""
from __future__ import annotations

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Generate CAT run report")
    parser.add_argument("run_dir", help="Path to run output directory")
    args = parser.parse_args()
    ...


if __name__ == "__main__":
    main()
