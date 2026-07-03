"""End-to-end test: AIAP agent tools — notebook_editor + internet_research.

Tests the SQLite-backed notebook_editor tool and internet_research tool
against a real notebook (A8P4) copied into data/aiap-profile/.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "aiap-profile"
STORE_DIR = DATA_DIR / "data" / "agent" / "aiap"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cleanup_store():
    """Remove store files before/after each test for isolation."""
    # Clean before
    if STORE_DIR.exists():
        for f in STORE_DIR.glob("*.sqlite"):
            f.unlink()
    yield
    # Clean after
    if STORE_DIR.exists():
        for f in STORE_DIR.glob("*.sqlite"):
            f.unlink()


def _nb_path() -> Path:
    return DATA_DIR / "test_notebook.ipynb"


# ── NotebookStore tests ──────────────────────────────────────────────────────

class TestNotebookStore:
    """Test the SQLite persistence layer directly."""

    def test_create_and_load(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        count = store.load_from_ipynb(_nb_path())
        assert count > 0
        assert store.cell_count() == count
        store.close()

    def test_read_cell(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        cell = store.read_cell(5)
        assert cell is not None
        assert cell["cell_type"] == "markdown"
        assert "Ethics" in cell["source"] or "ethics" in cell["source"].lower()
        store.close()

    def test_write_cell(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        # Write to an empty code cell
        store.write_cell(38, "# Test\nprint('hello')", cell_type="code")
        cell = store.read_cell(38)
        assert cell["source"] == "# Test\nprint('hello')"
        assert cell["cell_type"] == "code"

        # Restore
        store.write_cell(38, "", cell_type="code")
        store.close()

    def test_insert_cell(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())
        original_count = store.cell_count()

        store.insert_cell(0, "# New first cell", cell_type="markdown")
        assert store.cell_count() == original_count + 1
        cell = store.read_cell(0)
        assert cell["source"] == "# New first cell"
        # Old cell 0 is now at index 1
        assert store.read_cell(1) is not None
        store.close()

    def test_append_cell(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        idx = store.append_cell("# Appended cell", cell_type="markdown")
        assert idx == store.max_idx()
        cell = store.read_cell(idx)
        assert cell["source"] == "# Appended cell"
        store.close()

    def test_delete_cell(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())
        original_count = store.cell_count()

        # Append then delete
        idx = store.append_cell("# To delete", cell_type="markdown")
        assert store.cell_count() == original_count + 1
        store.delete_cell(idx)
        assert store.cell_count() == original_count
        store.close()

    def test_find_empty_code_cells(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        empty = store.find_empty_code_cells()
        assert len(empty) > 0, "A8P4 should have empty code cells"
        # All returned indices should actually be empty code cells
        for idx in empty:
            cell = store.read_cell(idx)
            assert cell["cell_type"] == "code"
            assert cell["source"] == ""
        store.close()

    def test_find_personal_voice(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        cells = store.find_cells_with_personal_voice()
        # A8P4 is known to have very little personal voice
        assert len(cells) < 5
        store.close()

    def test_get_stats(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        stats = store.get_stats()
        assert stats["total_cells"] > 0
        assert stats["code_cells"] > 0
        assert stats["markdown_cells"] > 0
        assert stats["empty_code_cells"] > 0  # Known issue in A8P4
        assert stats["personal_voice_cells"] < 5  # Known issue
        store.close()

    def test_find_cells(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        # A8P4 covers ethics & prompt injection
        results = store.find_cells("prompt injection")
        assert len(results) > 0

        results = store.find_cells("zzzznonexistent12345")
        assert len(results) == 0
        store.close()

    def test_sync_to_ipynb(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())

        # Modify a cell
        store.write_cell(38, "# Filled by test", cell_type="code")

        # Sync to a new file
        out_path = DATA_DIR / "test_output.ipynb"
        count = store.sync_to_ipynb(out_path)
        assert count > 0
        assert out_path.exists()

        # Verify the written file
        nb = json.load(open(out_path))
        assert len(nb["cells"]) == count

        # Restore original
        store.write_cell(38, "", cell_type="code")
        out_path.unlink(missing_ok=True)
        store.close()

    def test_list_stores(self):
        from cat.capability.tools.notebook_store import NotebookStore

        store = NotebookStore(STORE_DIR, "test.ipynb")
        store.load_from_ipynb(_nb_path())
        store.close()

        stores = NotebookStore.list_stores(STORE_DIR)
        assert "test" in stores


# ── notebook_editor tool tests ───────────────────────────────────────────────

class TestNotebookEditorTool:
    """Test the notebook_editor CAT tool (uses store internally)."""

    def _reg(self):
        from cat.capability.full_registry import build_default_registry
        return build_default_registry()

    def _state(self):
        from cat.contract.model import State
        return State(workdir=DATA_DIR)

    def test_store_load_and_list(self):
        reg, state = self._reg(), self._state()

        # Load from .ipynb into store
        result = reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)
        assert not result.is_error, f"load failed: {result.output}"
        assert "Imported" in result.output

        # List cells
        result = reg.invoke("notebook_editor", {
            "command": "list", "notebook": "test_notebook.ipynb",
        }, state)
        assert not result.is_error
        assert "[  0]" in result.output

    def test_store_stats(self):
        reg, state = self._reg(), self._state()

        # Load first
        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        result = reg.invoke("notebook_editor", {
            "command": "stats", "notebook": "test_notebook.ipynb",
        }, state)
        assert not result.is_error
        assert "total_cells" in result.output or "Cells:" in result.output

    def test_read_cell(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        result = reg.invoke("notebook_editor", {
            "command": "read", "notebook": "test_notebook.ipynb",
            "cell_idx": 5,
        }, state)
        assert not result.is_error
        assert "Ethics" in result.output or "ethics" in result.output.lower()

    def test_write_and_read_back(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        # Write to empty code cell 38
        result = reg.invoke("notebook_editor", {
            "command": "write", "notebook": "test_notebook.ipynb",
            "cell_idx": 38, "content": "# Test\nprint('hello')", "cell_type": "code",
        }, state)
        assert not result.is_error

        # Read back via store
        result = reg.invoke("notebook_editor", {
            "command": "read", "notebook": "test_notebook.ipynb",
            "cell_idx": 38,
        }, state)
        assert not result.is_error
        assert "print('hello')" in result.output

        # Restore
        reg.invoke("notebook_editor", {
            "command": "write", "notebook": "test_notebook.ipynb",
            "cell_idx": 38, "content": "", "cell_type": "code",
        }, state)

    def test_answer_green_box(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        content = (
            "**Formal definition:** A prompt injection is an attack where "
            "an attacker manipulates an LLM via crafted input.\n\n"
            "*My understanding:* Think of it like SQL injection but for LLMs — "
            "instead of injecting SQL commands, you inject natural language "
            "that tricks the model into doing something unintended."
        )
        result = reg.invoke("notebook_editor", {
            "command": "answer", "notebook": "test_notebook.ipynb",
            "cell_idx": 5, "content": content,
        }, state)
        assert not result.is_error, f"answer failed: {result.output}"

        # Read back and verify green-box wrapper
        result = reg.invoke("notebook_editor", {
            "command": "read", "notebook": "test_notebook.ipynb",
            "cell_idx": 5,
        }, state)
        assert "background-color: #f0fff4" in result.output
        assert "border-left: 4px solid #4CAF50" in result.output
        assert "Formal definition" in result.output
        assert "My understanding" in result.output

        # Clean up: delete the inserted cell
        reg.invoke("notebook_editor", {
            "command": "delete", "notebook": "test_notebook.ipynb",
            "cell_idx": 5,
        }, state)

    def test_find_keyword(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        result = reg.invoke("notebook_editor", {
            "command": "find", "notebook": "test_notebook.ipynb",
            "keyword": "prompt injection",
        }, state)
        assert not result.is_error
        assert "prompt injection" in result.output.lower() or "[" in result.output

    def test_find_no_match(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        result = reg.invoke("notebook_editor", {
            "command": "find", "notebook": "test_notebook.ipynb",
            "keyword": "zzzznonexistent12345",
        }, state)
        assert not result.is_error
        assert "no" in result.output.lower()

    def test_append_and_delete(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        # Append
        result = reg.invoke("notebook_editor", {
            "command": "append", "notebook": "test_notebook.ipynb",
            "content": "# Test append", "cell_type": "markdown",
        }, state)
        assert not result.is_error

        # Delete last cell
        from cat.capability.tools.notebook_store import NotebookStore
        store = NotebookStore(STORE_DIR, "test_notebook.ipynb")
        last_idx = store.max_idx()
        store.close()

        result = reg.invoke("notebook_editor", {
            "command": "delete", "notebook": "test_notebook.ipynb",
            "cell_idx": last_idx,
        }, state)
        assert not result.is_error

    def test_store_sync(self):
        reg, state = self._reg(), self._state()

        reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "load",
        }, state)

        # Sync to a new file
        result = reg.invoke("notebook_editor", {
            "command": "store", "notebook": "test_notebook.ipynb",
            "store_action": "sync",
        }, state)
        assert not result.is_error
        assert "Synced" in result.output


# ── internet_research tool tests ──────────────────────────────────────────────

class TestInternetResearchTool:
    """Test the internet_research native tool."""

    pytestmark = pytest.mark.skipif(
        subprocess.run(["curl", "-s", "--max-time", "5", "https://en.wikipedia.org/w/api.php"],
                       capture_output=True, timeout=8).returncode != 0,
        reason="no network",
    )

    def _reg(self):
        from cat.capability.full_registry import build_default_registry
        return build_default_registry()

    def _state(self):
        from cat.contract.model import State
        return State(workdir=DATA_DIR)

    def test_basic_research(self):
        reg, state = self._reg(), self._state()
        result = reg.invoke("internet_research", {
            "query": "Jupyter notebook best practices", "n_pages": 2,
        }, state)
        assert not result.is_error
        assert "Raw Research Results" in result.output or "Research" in result.output

    def test_research_with_focus(self):
        reg, state = self._reg(), self._state()
        result = reg.invoke("internet_research", {
            "query": "machine learning evaluation metrics",
            "focus": "classification", "n_pages": 2,
        }, state)
        assert not result.is_error

    def test_missing_topic(self):
        reg, state = self._reg(), self._state()
        result = reg.invoke("internet_research", {"query": ""}, state)
        assert result.is_error


# ── Profile loading tests ────────────────────────────────────────────────────

class TestAIAPProfiles:
    """Test that all AIAP profiles load correctly."""

    def test_aiap_profile_loads(self):
        from cat.agent.registry import AgentRegistry
        skill = AgentRegistry().load("aiap")
        assert skill.name == "aiap"
        keys = [r.get("key", "") for r in skill.represent]
        assert "conversation_mode" in keys
        assert "persona" in keys
        assert "intent_guide" in keys
        assert "aiap_criteria" in keys

    def test_evaluator_profile_loads(self):
        from cat.agent.registry import AgentRegistry
        skill = AgentRegistry().load("aiap-evaluator")
        assert skill.name == "aiap-evaluator"
        keys = [r.get("key", "") for r in skill.represent]
        assert "eval_criteria" in keys
        assert "output_format" in keys

    def test_chat_profile_loads(self):
        from cat.agent.registry import AgentRegistry
        skill = AgentRegistry().load("chat")
        assert skill.name == "chat"


# ── Notebook audit metrics ───────────────────────────────────────────────────

class TestNotebookAuditMetrics:
    """Audit A8P4 for known issues."""

    def test_empty_code_cells_exist(self):
        """A8P4 has empty code cells (known red flag)."""
        nb = json.load(open(_nb_path()))
        empty = [i for i, c in enumerate(nb["cells"])
                 if c.get("cell_type") == "code" and not "".join(c.get("source", "")).strip()]
        print(f"\nA8P4: {len(nb['cells'])} cells, {len(empty)} empty code cells at {empty}")
        assert len(empty) > 0

    def test_low_personal_voice(self):
        """A8P4 has very little personal voice."""
        nb = json.load(open(_nb_path()))
        personal = []
        for i, c in enumerate(nb["cells"]):
            src = "".join(c.get("source", "")) if isinstance(c.get("source"), list) else c.get("source", "")
            if any(kw in src.lower() for kw in ["my understanding", "i noticed", "i tried"]):
                personal.append(i)
        print(f"\nA8P4: {len(personal)} personal voice cells at {personal}")
        assert len(personal) < 5
