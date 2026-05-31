"""Unit tests for LibrarianV2."""
import os
import tempfile

from cat.librarian import LibrarianV2
from cat.plan_tree import PlanTree, TreeEditOp


SAMPLE_PLAN = {
    "phases": [
        {"name": "MAP", "steps": [
            {"description": "read x", "intent": "investigate", "expects": {"durable_change": False}}
        ]},
        {"name": "IMPLEMENT", "steps": [
            {"description": "edit y", "intent": "mutate", "expects": {"durable_change": True}}
        ]},
    ]
}


def test_record_and_get_plan_template():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        lib = LibrarianV2(db_path)
        tree = PlanTree.from_phased_plan(SAMPLE_PLAN, obligation_class="abc")
        tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": "done"}, "ref"))
        lib.record_plan_tree(tree, "FULFILLED", task_id="t1")
        templates = lib.get_plan_templates("abc")
        assert len(templates) == 1
        assert templates[0]["obligation_class"] == "abc"
        lib.close()
    finally:
        os.unlink(db_path)


def test_get_anti_template_for_fail():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        lib = LibrarianV2(db_path)
        tree = PlanTree.from_phased_plan(SAMPLE_PLAN, obligation_class="abc")
        lib.record_plan_tree(tree, "FAILED", task_id="t1")
        anti = lib.get_anti_templates("abc")
        assert len(anti) == 1
        pass_templates = lib.get_plan_templates("abc")
        assert len(pass_templates) == 0
        lib.close()
    finally:
        os.unlink(db_path)


def test_record_edge_and_stats():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        lib = LibrarianV2(db_path)
        lib.record_edge("S1", "A1", "S2", 1000, "PASS", "t1")
        lib.record_edge("S1", "A1", "S2", 2000, "PASS", "t2")
        lib.record_edge("S1", "A1", "S3", 1500, "FAIL", "t3")
        stats = lib.edge_stats("S1")
        assert len(stats) == 1  # one action class
        a1 = stats[0]
        assert a1["action_class"] == "A1"
        assert a1["count"] == 3
        assert a1["cost_mean"] == 1500
        assert a1["next_state_mode"] == "S2"  # appears 2/3 times
        lib.close()
    finally:
        os.unlink(db_path)


def test_summary():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        lib = LibrarianV2(db_path)
        tree = PlanTree.from_phased_plan(SAMPLE_PLAN, obligation_class="abc")
        lib.record_plan_tree(tree, "FULFILLED")
        lib.record_plan_tree(tree, "FAILED")
        lib.record_edge("S1", "A1", "S2", 1000)
        s = lib.summary()
        assert s["plan_trees"] == 2
        assert s["plan_trees_pass"] == 1
        assert s["plan_trees_fail"] == 1
        assert s["edges"] == 1
        assert s["obligation_classes"] == 1
        lib.close()
    finally:
        os.unlink(db_path)


def test_get_templates_empty():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        lib = LibrarianV2(db_path)
        assert lib.get_plan_templates("unknown") == []
        assert lib.get_anti_templates("unknown") == []
        lib.close()
    finally:
        os.unlink(db_path)
