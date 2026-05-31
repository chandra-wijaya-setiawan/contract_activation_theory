"""Unit tests for cat/surprise.py."""
from cat.surprise import compute_surprise, SurpriseVector


def test_no_surprise_when_everything_matches():
    sv = compute_surprise(
        expects={"durable_change": True, "files_modified": ["a.py"], "verifier_outcome": "N/A"},
        actual_durable_change=True,
        actual_files_modified=["a.py"],
    )
    assert not sv.any_surprised


def test_durable_change_mismatch():
    sv = compute_surprise(
        expects={"durable_change": True, "files_modified": []},
        actual_durable_change=False,
    )
    assert sv.any_surprised
    assert not sv.durable_match
    assert "durable" in sv.details


def test_files_match_subset_passes():
    sv = compute_surprise(
        expects={"durable_change": True, "files_modified": ["a.py"]},
        actual_durable_change=True,
        actual_files_modified=["a.py", "b.py"],   # extra file is fine
    )
    assert sv.files_match
    assert not sv.any_surprised


def test_files_match_missing_file_surprises():
    sv = compute_surprise(
        expects={"durable_change": True, "files_modified": ["a.py"]},
        actual_durable_change=True,
        actual_files_modified=["b.py"],
    )
    assert not sv.files_match
    assert sv.any_surprised


def test_verifier_match():
    sv = compute_surprise(
        expects={"verifier_outcome": "PASS"},
        actual_verifier_outcome="FAIL",
    )
    assert not sv.verifier_match
    assert sv.any_surprised


def test_verifier_na_no_check():
    sv = compute_surprise(
        expects={"verifier_outcome": "N/A"},
        actual_verifier_outcome="PASS",
    )
    assert sv.verifier_match


def test_cost_z_under_threshold():
    sv = compute_surprise(
        expects={},
        actual_cost=10000,
        predicted_cost_mean=8000,
        predicted_cost_std=2000,
    )
    # |10000-8000|/2000 = 1.0 < 2.0 → not surprised
    assert not sv.cost_surprised
    assert sv.cost_z == 1.0


def test_cost_z_over_threshold():
    sv = compute_surprise(
        expects={},
        actual_cost=20000,
        predicted_cost_mean=8000,
        predicted_cost_std=2000,
    )
    # |20000-8000|/2000 = 6.0 > 2.0 → surprised
    assert sv.cost_surprised
    assert sv.cost_z >= 2.0


def test_cost_no_prediction_no_surprise():
    sv = compute_surprise(expects={}, actual_cost=10000, predicted_cost_mean=0)
    assert not sv.cost_surprised


def test_state_match():
    sv = compute_surprise(
        expects={},
        state_next="abc",
        predicted_next_state="abc",
    )
    assert sv.state_match
    sv2 = compute_surprise(
        expects={},
        state_next="abc",
        predicted_next_state="def",
    )
    assert not sv2.state_match
    assert sv2.any_surprised


def test_to_dict_structure():
    sv = compute_surprise(
        expects={"durable_change": True, "files_modified": []},
        actual_durable_change=False,
    )
    d = sv.to_dict()
    assert "cost_z" in d
    assert "any_surprised" in d
    assert d["any_surprised"] is True


def test_outcome_match():
    sv = compute_surprise(
        expects={},
        actual_outcome="PASS",
        predicted_outcome="PASS",
    )
    assert sv.outcome_match
    sv2 = compute_surprise(
        expects={},
        actual_outcome="FAIL",
        predicted_outcome="PASS",
    )
    assert not sv2.outcome_match
    assert sv2.any_surprised
