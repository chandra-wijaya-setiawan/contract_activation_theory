"""Unit tests for cat/plan_tree.py."""
from cat.plan_tree import PlanTree, PlanNode, TreeEditOp


SAMPLE_PLAN = {
    "phases": [
        {
            "name": "MAP",
            "steps": [
                {"description": "read client.py", "intent": "investigate",
                 "expects": {"durable_change": False, "files_modified": [], "verifier_outcome": "N/A"}},
                {"description": "read test_postgresql.py", "intent": "investigate",
                 "expects": {"durable_change": False, "files_modified": [], "verifier_outcome": "N/A"}},
            ],
        },
        {
            "name": "IMPLEMENT",
            "steps": [
                {"description": "edit client.py", "intent": "mutate",
                 "expects": {"durable_change": True, "files_modified": ["client.py"], "verifier_outcome": "N/A"}},
            ],
        },
        {
            "name": "VERIFY",
            "steps": [
                {"description": "run test", "intent": "verify",
                 "expects": {"durable_change": False, "files_modified": [], "verifier_outcome": "PASS"}},
            ],
        },
    ]
}


def test_from_phased_plan_basic():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN, obligation_class="abc123")
    assert tree.obligation_class == "abc123"
    assert tree.phase_order == ["A", "B", "C"]
    assert tree.nodes["A"].phase_name == "MAP"
    assert tree.nodes["B"].phase_name == "IMPLEMENT"
    assert tree.nodes["C"].phase_name == "VERIFY"
    assert tree.nodes["A1"].description == "read client.py"
    assert tree.nodes["A1"].intent == "investigate"
    assert tree.nodes["B1"].expects["durable_change"] is True


def test_attention_starts_at_first_leaf():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    assert tree.attention_id == "A1"
    assert tree.nodes["A1"].status == "active"


def test_commit_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": "done"}, "reflection"), iter_n=1)
    assert ok, err
    assert tree.nodes["A1"].status == "complete"
    assert tree.nodes["A1"].completed_at_iter == 1
    assert tree.nodes["A1"].completion_reason == "done"


def test_commit_unknown_node():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("COMMIT", {"node_id": "ZZ"}, ""))
    assert not ok
    assert "unknown" in err


def test_checkout_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    # commit A1 first
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    ok, err = tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "A2"}, ""))
    assert ok, err
    assert tree.attention_id == "A2"
    assert tree.nodes["A2"].status == "active"


def test_drop_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("DROP", {"node_id": "A2", "reason": "not needed"}, ""))
    assert ok, err
    assert tree.nodes["A2"].status == "pruned"


def test_commit_new_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("COMMIT_NEW", {
        "parent_id": "B",
        "new_step": {
            "description": "edit base/client.py",
            "intent": "mutate",
            "expects": {"durable_change": True, "files_modified": ["base/client.py"], "verifier_outcome": "N/A"},
        },
    }, "discovered base/client.py also needs updating"))
    assert ok, err
    # new step id should be B2
    assert "B2" in tree.nodes
    assert tree.nodes["B2"].description == "edit base/client.py"
    assert "B2" in tree.nodes["B"].children


def test_refine_op_shifts_attention():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    # checkout B1 first
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "B1"}, ""))
    assert tree.attention_id == "B1"
    ok, err = tree.apply_op(TreeEditOp("REFINE", {
        "node_id": "B1",
        "substeps": [
            {"description": "build env dict", "intent": "mutate", "expects": {"durable_change": True}},
            {"description": "change return type", "intent": "mutate", "expects": {"durable_change": True}},
        ],
    }, "edit has 2 sub-edits"))
    assert ok, err
    assert "B1.1" in tree.nodes
    assert "B1.2" in tree.nodes
    assert tree.attention_id == "B1.1"
    assert tree.nodes["B1.1"].status == "active"


def test_refine_rejects_mutate_into_all_investigate():
    """R17: REFINE of a mutate-intent parent must keep at least one mutate substep.
    Otherwise the original mutation requirement is dropped (django-11239 pattern)."""
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "B1"}, ""))
    # parent B1 has intent=mutate (from SAMPLE_PLAN). Refine into all-investigate:
    ok, err = tree.apply_op(TreeEditOp("REFINE", {
        "node_id": "B1",
        "substeps": [
            {"description": "read file again", "intent": "investigate"},
            {"description": "search for callers", "intent": "investigate"},
        ],
    }, "trying to learn more first"))
    assert not ok
    assert "mutate" in err.lower()
    # The substeps must not have been created
    assert "B1.1" not in tree.nodes
    assert tree.attention_id == "B1"  # unchanged


def test_refine_accepts_mutate_with_at_least_one_mutate_substep():
    """R17 happy path: at least one mutate substep → accept."""
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "B1"}, ""))
    ok, err = tree.apply_op(TreeEditOp("REFINE", {
        "node_id": "B1",
        "substeps": [
            {"description": "investigate the method", "intent": "investigate"},
            {"description": "apply the str_replace edit", "intent": "mutate",
             "expects": {"durable_change": True}},
        ],
    }, "split investigate + edit"))
    assert ok, err
    assert tree.nodes["B1.1"].intent == "investigate"
    assert tree.nodes["B1.2"].intent == "mutate"


def test_refine_allows_non_mutate_parent_into_anything():
    """R17 doesn't restrict refinement of investigate/design parents."""
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    # A1 is investigate intent in SAMPLE_PLAN; refining to all-design is fine.
    ok, err = tree.apply_op(TreeEditOp("REFINE", {
        "node_id": "A1",
        "substeps": [
            {"description": "decide approach", "intent": "design"},
            {"description": "consider alternatives", "intent": "design"},
        ],
    }, "redirect to design"))
    assert ok, err


def test_branch_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "B1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "C1"}, ""))
    ok, err = tree.apply_op(TreeEditOp("BRANCH", {
        "node_id": "C1",
        "alt_steps": [
            {"description": "run django test instead", "intent": "verify",
             "expects": {"durable_change": False, "verifier_outcome": "PASS"}},
        ],
    }, "pytest failed; trying django test runner"))
    assert ok, err
    assert tree.nodes["C1"].status == "branched"
    assert "C1-alt-1" in tree.nodes
    assert tree.nodes["C1-alt-1"].branch_of == "C1"
    assert tree.attention_id == "C1-alt-1.1"


def test_merge_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "B1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "C1"}, ""))
    tree.apply_op(TreeEditOp("BRANCH", {
        "node_id": "C1",
        "alt_steps": [{"description": "alt", "intent": "verify", "expects": {}}],
    }, ""))
    # mark the alt step done
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "C1-alt-1.1", "reason": "alt worked"}, ""))
    ok, err = tree.apply_op(TreeEditOp("MERGE", {
        "branch_id": "C1-alt-1",
        "target_id": "C1",
    }, "alt succeeded; folding back"))
    assert ok, err
    assert tree.nodes["C1-alt-1"].status == "complete"
    assert tree.nodes["C1"].status == "pruned"


def test_advance_attention():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    # A1 active
    assert tree.attention_id == "A1"
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    nxt = tree.advance_attention()
    assert nxt == "A2"
    assert tree.nodes["A2"].status == "active"
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    nxt = tree.advance_attention()
    assert nxt == "B1"
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "B1", "reason": ""}, ""))
    nxt = tree.advance_attention()
    assert nxt == "C1"


def test_all_complete():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    assert not tree.all_complete()
    for nid in ["A1", "A2", "B1", "C1"]:
        tree.apply_op(TreeEditOp("COMMIT", {"node_id": nid, "reason": ""}, ""))
    assert tree.all_complete()


def test_serialization_roundtrip():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN, obligation_class="x")
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": "done"}, "ref"))
    d = tree.to_dict()
    tree2 = PlanTree.from_dict(d)
    assert tree2.obligation_class == "x"
    assert tree2.attention_id == tree.attention_id
    assert tree2.nodes["A1"].status == "complete"
    assert tree2.nodes["A1"].completion_reason == "done"


def test_render_outline_smoke():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    out = tree.render_outline()
    assert "A1" in out
    assert "MAP" in out
    assert "read client.py" in out


def test_lookahead():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    upcoming = tree.lookahead(max_steps=3)
    # current attention A1 included, then A2, B1
    ids = [n.id for n in upcoming]
    assert "A1" in ids
    assert "A2" in ids


def test_unknown_op():
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("WHIMSY", {}, ""))
    assert not ok
    assert "unknown" in err


def test_malformed_expects_string_coerced():
    """Regression: LLM may emit `expects` as a string. Should coerce to {}."""
    bad_plan = {
        "phases": [{"name": "MAP", "steps": [
            {"description": "x", "intent": "investigate", "expects": "not-a-dict"}
        ]}]
    }
    tree = PlanTree.from_phased_plan(bad_plan)
    assert tree.nodes["A1"].expects == {}


def test_refine_with_string_substep_skipped():
    """Regression: REFINE with substeps that are strings (not dicts) should not crash."""
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("REFINE", {
        "node_id": "A1",
        "substeps": ["string_not_dict", {"description": "real", "intent": "investigate"}],
    }, "test"))
    assert ok, err
    # Only the valid substep should be added
    assert "A1.1" in tree.nodes
    assert tree.nodes["A1.1"].description == "real"
    assert "A1.2" not in tree.nodes


def test_branch_with_string_alt_step_filtered():
    """Regression: BRANCH with non-dict alt_step items should be filtered."""
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    # Need A1 to not be branched at root; checkout to a leaf first
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A1", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("COMMIT", {"node_id": "A2", "reason": ""}, ""))
    tree.apply_op(TreeEditOp("CHECKOUT", {"node_id": "B1"}, ""))
    ok, err = tree.apply_op(TreeEditOp("BRANCH", {
        "node_id": "B1",
        "alt_steps": ["bad", {"description": "good alt", "intent": "mutate"}],
    }, "test"))
    assert ok, err


def test_commit_new_rejects_string_new_step():
    """Regression: COMMIT_NEW with new_step as string should reject, not crash."""
    tree = PlanTree.from_phased_plan(SAMPLE_PLAN)
    ok, err = tree.apply_op(TreeEditOp("COMMIT_NEW", {
        "parent_id": "A",
        "new_step": "string-not-dict",
    }, "test"))
    assert not ok
    assert "dict" in err.lower()
