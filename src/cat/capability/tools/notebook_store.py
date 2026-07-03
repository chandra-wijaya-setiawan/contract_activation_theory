"""notebook_store — SQLite-backed persistence for notebook cell state.

Single source of truth for notebook editing. Instead of reading/writing the
.ipynb JSON repeatedly, the agent works with a SQLite store that maps:
  cell_idx → source, cell_type, outputs, execution_count, metadata

The .ipynb file is only read once (on first access) and written back on
explicit .sync() or .save() calls.

Store layout:
  data/agent/<agent_name>/<notebook_name>.sqlite

Schema:
  cells(
    idx         INTEGER PRIMARY KEY,  -- 0-based cell index
    cell_type   TEXT NOT NULL,        -- 'code' or 'markdown'
    source      TEXT NOT NULL,        -- cell source content
    outputs     TEXT,                 -- JSON-serialized outputs list
    exec_count  INTEGER,              -- execution_count
    metadata    TEXT                  -- JSON-serialized metadata dict
  )
  notebook_meta(
    key   TEXT PRIMARY KEY,
    value TEXT
  )
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS cells (
    idx         INTEGER PRIMARY KEY,
    cell_type   TEXT NOT NULL CHECK(cell_type IN ('code', 'markdown')),
    source      TEXT NOT NULL DEFAULT '',
    outputs     TEXT,
    exec_count  INTEGER,
    metadata    TEXT
);

CREATE TABLE IF NOT EXISTS notebook_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_cells_type ON cells(cell_type);
"""


class NotebookStore:
    """SQLite-backed cell store for a single notebook.

    Usage:
        store = NotebookStore("data/agent/aiap", "A8P4.ipynb")
        store.load_from_ipynb(Path("assignment8/A8P4.ipynb"))  # one-time import
        cell = store.read_cell(5)
        store.write_cell(5, "# New content", cell_type="markdown")
        store.insert_cell(3, "# Inserted before 3", cell_type="markdown")
        store.delete_cell(10)
        store.sync_to_ipynb(Path("assignment8/A8P4.ipynb"))  # write back
    """

    def __init__(self, agent_dir: str | Path, notebook_name: str):
        """
        Args:
            agent_dir: Path like 'data/agent/aiap' — where .sqlite files live
            notebook_name: e.g. 'A8P4_Ethics_And_Governance.ipynb'
        """
        self.agent_dir = Path(agent_dir)
        self.agent_dir.mkdir(parents=True, exist_ok=True)

        # Derive sqlite filename from notebook name
        nb_stem = Path(notebook_name).stem
        self.db_path = self.agent_dir / f"{nb_stem}.sqlite"
        self._nb_name = notebook_name

        # Open connection
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_CREATE_TABLES)
        self.conn.commit()

    # ── Load / Save ──────────────────────────────────────────────────────

    def load_from_ipynb(self, ipynb_path: Path | str) -> int:
        """Import all cells from a .ipynb file. Returns cell count."""
        ipynb_path = Path(ipynb_path)
        if not ipynb_path.exists():
            raise FileNotFoundError(f"notebook not found: {ipynb_path}")

        nb = json.load(open(ipynb_path, encoding="utf-8"))
        cells = nb.get("cells", [])

        # Clear existing data
        self.conn.execute("DELETE FROM cells")

        for i, cell in enumerate(cells):
            ctype = cell.get("cell_type", "markdown")
            source = cell.get("source", "")
            if isinstance(source, list):
                source = "".join(source)
            outputs = json.dumps(cell.get("outputs", [])) if cell.get("outputs") else None
            exec_count = cell.get("execution_count")
            metadata = json.dumps(cell.get("metadata", {}))

            self.conn.execute(
                "INSERT INTO cells (idx, cell_type, source, outputs, exec_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (i, ctype, source, outputs, exec_count, metadata),
            )

        # Store notebook-level metadata
        self.conn.execute("DELETE FROM notebook_meta")
        self.conn.execute(
            "INSERT INTO notebook_meta (key, value) VALUES (?, ?)",
            ("source_file", str(ipynb_path)),
        )
        self.conn.execute(
            "INSERT INTO notebook_meta (key, value) VALUES (?, ?)",
            ("total_cells", str(len(cells))),
        )
        self.conn.commit()
        return len(cells)

    def sync_to_ipynb(self, ipynb_path: Path | str) -> int:
        """Write current cell state back to a .ipynb file. Returns cell count."""
        ipynb_path = Path(ipynb_path)

        rows = self.conn.execute(
            "SELECT idx, cell_type, source, outputs, exec_count, metadata "
            "FROM cells ORDER BY idx"
        ).fetchall()

        cells = []
        for row in rows:
            cell = {
                "cell_type": row["cell_type"],
                "metadata": json.loads(row["metadata"] or "{}"),
                "source": row["source"],
            }
            if row["cell_type"] == "code":
                cell["outputs"] = json.loads(row["outputs"] or "[]")
                cell["execution_count"] = row["exec_count"]
            cells.append(cell)

        # Preserve original notebook structure if it exists
        if ipynb_path.exists():
            nb = json.load(open(ipynb_path, encoding="utf-8"))
        else:
            nb = {
                "nbformat": 4,
                "nbformat_minor": 5,
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3"
                    },
                    "language_info": {"name": "python", "version": "3.12.0"}
                },
                "cells": [],
            }
        nb["cells"] = cells

        with open(ipynb_path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)

        return len(cells)

    # ── Cell CRUD ─────────────────────────────────────────────────────────

    def read_cell(self, idx: int) -> dict | None:
        """Read a cell by index. Returns dict or None."""
        row = self.conn.execute(
            "SELECT * FROM cells WHERE idx = ?", (idx,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def write_cell(self, idx: int, source: str, cell_type: str = "code",
                   outputs: list | None = None, exec_count: int | None = None) -> bool:
        """Update an existing cell. Returns True if cell existed."""
        existing = self.conn.execute("SELECT 1 FROM cells WHERE idx = ?", (idx,)).fetchone()
        if existing is None:
            return False

        outputs_json = json.dumps(outputs) if outputs is not None else None
        self.conn.execute(
            "UPDATE cells SET source=?, cell_type=?, outputs=?, exec_count=? WHERE idx=?",
            (source, cell_type, outputs_json, exec_count, idx),
        )
        self.conn.commit()
        return True

    def insert_cell(self, idx: int, source: str, cell_type: str = "code") -> int:
        """Insert a new cell before idx. Shifts all cells at idx and above. Returns new total."""
        # Shift cells up in descending order to avoid unique constraint violations.
        # We fetch the affected rows in DESC order and update one by one.
        rows = self.conn.execute(
            "SELECT idx FROM cells WHERE idx >= ? ORDER BY idx DESC", (idx,)
        ).fetchall()
        for (row_idx,) in rows:
            self.conn.execute(
                "UPDATE cells SET idx = ? WHERE idx = ?", (row_idx + 1, row_idx)
            )
        # Insert new cell
        self.conn.execute(
            "INSERT INTO cells (idx, cell_type, source) VALUES (?, ?, ?)",
            (idx, cell_type, source),
        )
        self.conn.commit()
        return self.cell_count()

    def append_cell(self, source: str, cell_type: str = "code") -> int:
        """Append a cell at the end. Returns the new cell's index."""
        max_idx = self.conn.execute("SELECT COALESCE(MAX(idx), -1) FROM cells").fetchone()[0]
        new_idx = max_idx + 1
        self.conn.execute(
            "INSERT INTO cells (idx, cell_type, source) VALUES (?, ?, ?)",
            (new_idx, cell_type, source),
        )
        self.conn.commit()
        return new_idx

    def delete_cell(self, idx: int) -> bool:
        """Delete a cell and shift subsequent cells down. Returns True if existed."""
        existing = self.conn.execute("SELECT 1 FROM cells WHERE idx = ?", (idx,)).fetchone()
        if existing is None:
            return False
        self.conn.execute("DELETE FROM cells WHERE idx = ?", (idx,))
        self.conn.execute("UPDATE cells SET idx = idx - 1 WHERE idx > ?", (idx,))
        self.conn.commit()
        return True

    # ── Queries ───────────────────────────────────────────────────────────

    def cell_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM cells").fetchone()
        return row[0]

    def max_idx(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(idx), -1) FROM cells").fetchone()
        return row[0]

    def list_cells(self, cell_type: str | None = None) -> list[dict]:
        """List all cells, optionally filtered by type."""
        if cell_type:
            rows = self.conn.execute(
                "SELECT * FROM cells WHERE cell_type = ? ORDER BY idx", (cell_type,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM cells ORDER BY idx").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_cells(self, keyword: str) -> list[dict]:
        """Find cells containing keyword (case-insensitive)."""
        rows = self.conn.execute(
            "SELECT * FROM cells WHERE source LIKE ? ORDER BY idx",
            (f"%{keyword}%",),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_empty_code_cells(self) -> list[int]:
        """Return indices of empty code cells (known AIAP red flag)."""
        rows = self.conn.execute(
            "SELECT idx FROM cells WHERE cell_type = 'code' AND (source = '' OR source IS NULL) ORDER BY idx"
        ).fetchall()
        return [r[0] for r in rows]

    def find_cells_without_output(self) -> list[int]:
        """Return indices of code cells that have no outputs."""
        rows = self.conn.execute(
            "SELECT idx FROM cells WHERE cell_type = 'code' "
            "AND (outputs = '[]' OR outputs IS NULL OR outputs = '') ORDER BY idx"
        ).fetchall()
        return [r[0] for r in rows]

    def find_cells_with_personal_voice(self) -> list[int]:
        """Return indices of cells containing personal voice markers."""
        rows = self.conn.execute(
            "SELECT idx FROM cells WHERE "
            "LOWER(source) LIKE '%my understanding%' OR "
            "LOWER(source) LIKE '%i noticed%' OR "
            "LOWER(source) LIKE '%i tried%' OR "
            "LOWER(source) LIKE '%my intuition%' OR "
            "LOWER(source) LIKE '%i observed%' "
            "ORDER BY idx"
        ).fetchall()
        return [r[0] for r in rows]

    def get_stats(self) -> dict:
        """Return notebook statistics."""
        total = self.cell_count()
        code_count = self.conn.execute(
            "SELECT COUNT(*) FROM cells WHERE cell_type = 'code'"
        ).fetchone()[0]
        md_count = self.conn.execute(
            "SELECT COUNT(*) FROM cells WHERE cell_type = 'markdown'"
        ).fetchone()[0]
        empty_code = len(self.find_empty_code_cells())
        no_output = len(self.find_cells_without_output())
        personal_voice = len(self.find_cells_with_personal_voice())
        return {
            "total_cells": total,
            "code_cells": code_count,
            "markdown_cells": md_count,
            "empty_code_cells": empty_code,
            "code_without_output": no_output,
            "personal_voice_cells": personal_voice,
        }

    # ── Metadata ──────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM notebook_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO notebook_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        if d.get("outputs"):
            d["outputs"] = json.loads(d["outputs"])
        if d.get("metadata"):
            d["metadata"] = json.loads(d["metadata"])
        return d

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Discovery ─────────────────────────────────────────────────────────

    @staticmethod
    def list_stores(agent_dir: str | Path) -> list[str]:
        """List all notebook store names in an agent directory."""
        agent_dir = Path(agent_dir)
        if not agent_dir.exists():
            return []
        return [p.stem for p in agent_dir.glob("*.sqlite")]

    @staticmethod
    def delete_store(agent_dir: str | Path, notebook_name: str):
        """Delete a notebook store."""
        stem = Path(notebook_name).stem
        db_path = Path(agent_dir) / f"{stem}.sqlite"
        if db_path.exists():
            db_path.unlink()
