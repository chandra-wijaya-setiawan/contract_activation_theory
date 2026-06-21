"""Notebook utilities — read/write Jupyter notebooks for experiment analysis."""

import json
from pathlib import Path
from typing import Any


def read_notebook(path: str | Path) -> dict[str, Any]:
    """Read a Jupyter notebook (.ipynb) and return its parsed JSON.

    Args:
        path: Path to the .ipynb file.

    Returns:
        Parsed notebook dict with 'cells', 'metadata', 'nbformat', etc.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Notebook not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_notebook(path: str | Path, nb: dict[str, Any]) -> None:
    """Write a Jupyter notebook (.ipynb) from a parsed dict.

    Args:
        path: Path to the .ipynb file.
        nb: Notebook dict with 'cells', 'metadata', 'nbformat', etc.
    """
    path = Path(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
