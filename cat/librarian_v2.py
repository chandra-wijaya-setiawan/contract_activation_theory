"""LibrarianV2 — persistent corpus for plan trees + edge stats.

Per docs/iteration.md §7. Four roles:
  1. Cost/path provider (predict_det)
  2. Forward model (predict_det)
  3. Plan-tree template provider (PASS trees)
  4. Anti-template provider (FAIL trees with reflections)

PoC scope: writes only (templates retrieval on later tasks during a
chronological run). No d* / Dijkstra layer yet; that comes when there's
enough data to compute it.

Storage: SQLite, single file. Cheap to copy between runs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

from cat.plan_tree import PlanTree


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_trees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_class TEXT NOT NULL,
    outcome TEXT NOT NULL,             -- "FULFILLED" / "FAILED" / "SUSPENDED"
    task_id TEXT,
    plan_json TEXT NOT NULL,           -- full PlanTree.to_dict() serialization
    n_nodes INTEGER,
    n_complete INTEGER,
    n_pruned INTEGER,
    n_branched INTEGER,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pt_obligation ON plan_trees(obligation_class, outcome);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state_class TEXT NOT NULL,
    action_class TEXT NOT NULL,
    next_state_class TEXT NOT NULL,
    cost INTEGER NOT NULL,             -- token cost
    outcome TEXT,
    task_id TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edge_state ON edges(state_class, action_class);
"""


class LibrarianV2:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── Plan trees (roles 3 + 4) ─────────────────────────────────────────
    def record_plan_tree(self, plan_tree: PlanTree, outcome: str, task_id: str = "") -> None:
        """Persist a plan tree at end of run."""
        # Map ContractState string variants to canonical {FULFILLED, FAILED, SUSPENDED}
        out_upper = (outcome or "").upper()
        canonical = "FAILED"
        for k in ("FULFILLED", "FAILED", "SUSPENDED"):
            if k in out_upper:
                canonical = k
                break
        d = plan_tree.to_dict()
        nodes = d.get("nodes", {})
        n_nodes = len(nodes)
        n_complete = sum(1 for n in nodes.values() if n.get("status") == "complete")
        n_pruned = sum(1 for n in nodes.values() if n.get("status") == "pruned")
        n_branched = sum(1 for n in nodes.values() if n.get("status") == "branched")
        self._conn.execute(
            "INSERT INTO plan_trees (obligation_class, outcome, task_id, plan_json,"
            " n_nodes, n_complete, n_pruned, n_branched, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_tree.obligation_class,
                canonical,
                task_id,
                json.dumps(d),
                n_nodes,
                n_complete,
                n_pruned,
                n_branched,
                time.time(),
            ),
        )
        self._conn.commit()

    def get_plan_templates(self, obligation_class: str, k: int = 3) -> list[dict[str, Any]]:
        """Return up to k PASS plan trees for this obligation_class (most recent first)."""
        rows = self._conn.execute(
            "SELECT plan_json FROM plan_trees WHERE obligation_class=? AND outcome='FULFILLED'"
            " ORDER BY id DESC LIMIT ?",
            (obligation_class, k),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for (pj,) in rows:
            try:
                out.append(json.loads(pj))
            except Exception:
                continue
        return out

    def get_anti_templates(self, obligation_class: str, k: int = 3) -> list[dict[str, Any]]:
        """Return up to k FAIL plan trees for this obligation_class (most recent first)."""
        rows = self._conn.execute(
            "SELECT plan_json FROM plan_trees WHERE obligation_class=? AND outcome IN ('FAILED','SUSPENDED')"
            " ORDER BY id DESC LIMIT ?",
            (obligation_class, k),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for (pj,) in rows:
            try:
                out.append(json.loads(pj))
            except Exception:
                continue
        return out

    # ── Edges (roles 1 + 2) ──────────────────────────────────────────────
    def record_edge(
        self,
        state_class: str,
        action_class: str,
        next_state_class: str,
        cost: int,
        outcome: str = "",
        task_id: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO edges (state_class, action_class, next_state_class, cost, outcome,"
            " task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state_class, action_class, next_state_class, int(cost), outcome, task_id, time.time()),
        )
        self._conn.commit()

    def edge_stats(self, state_class: str) -> list[dict[str, Any]]:
        """Aggregated edge stats from this state. Returns a list of:
        {action_class, count, cost_mean, cost_std, next_state_mode, outcome_mode}.
        """
        rows = self._conn.execute(
            "SELECT action_class, next_state_class, cost, outcome FROM edges WHERE state_class=?",
            (state_class,),
        ).fetchall()
        if not rows:
            return []
        by_action: dict[str, list[tuple[str, int, str]]] = {}
        for ac, ns, cost, outcome in rows:
            by_action.setdefault(ac, []).append((ns, cost, outcome))
        result: list[dict[str, Any]] = []
        for ac, entries in by_action.items():
            costs = [e[1] for e in entries]
            mean = sum(costs) / len(costs)
            var = sum((c - mean) ** 2 for c in costs) / len(costs)
            std = var ** 0.5
            ns_counter: dict[str, int] = {}
            for ns, _, _ in entries:
                ns_counter[ns] = ns_counter.get(ns, 0) + 1
            ns_mode = max(ns_counter.items(), key=lambda x: x[1])[0]
            o_counter: dict[str, int] = {}
            for _, _, o in entries:
                o_counter[o or "?"] = o_counter.get(o or "?", 0) + 1
            o_mode = max(o_counter.items(), key=lambda x: x[1])[0]
            result.append({
                "action_class": ac,
                "count": len(entries),
                "cost_mean": mean,
                "cost_std": std,
                "next_state_mode": ns_mode,
                "outcome_mode": o_mode,
            })
        return result

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Summary helpers (for logging) ────────────────────────────────────
    def summary(self) -> dict[str, int]:
        n_trees = self._conn.execute("SELECT COUNT(*) FROM plan_trees").fetchone()[0]
        n_pass = self._conn.execute(
            "SELECT COUNT(*) FROM plan_trees WHERE outcome='FULFILLED'"
        ).fetchone()[0]
        n_fail = self._conn.execute(
            "SELECT COUNT(*) FROM plan_trees WHERE outcome IN ('FAILED','SUSPENDED')"
        ).fetchone()[0]
        n_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_obligations = self._conn.execute(
            "SELECT COUNT(DISTINCT obligation_class) FROM plan_trees"
        ).fetchone()[0]
        return {
            "plan_trees": n_trees,
            "plan_trees_pass": n_pass,
            "plan_trees_fail": n_fail,
            "edges": n_edges,
            "obligation_classes": n_obligations,
        }
