"""Tests for obligation_class + state_class v2."""
from dataclasses import dataclass

from cat.obligation_class import compute_obligation_class, compute_obligation_class_features
from cat.state_class_v2 import compute_state_class, RunCounts, _bucket


@dataclass
class FakeRep:
    key: str
    evidence: str


def test_obligation_class_from_task_id():
    h1 = compute_obligation_class("any obligation text", reps_or_none(), task_id="django__django-11239")
    h2 = compute_obligation_class("different text", reps_or_none(), task_id="django__django-12039")
    # Same framework, same subsystem extraction context → same hash WHEN reps are same.
    # With no reps, subsystem is "unknown" → both default to (django, unknown)
    assert h1 == h2  # both django + unknown subsystem


def test_obligation_class_features_visible():
    feats = compute_obligation_class_features(
        "fix the db backend SSL handling",
        representations=[FakeRep("files", "edit django/db/backends/postgresql/client.py and django/db/backends/base/client.py")],
        task_id="django__django-11239",
    )
    assert feats["framework"] == "django"
    assert "db.backends" in feats["subsystem"]


def test_obligation_class_diff_framework_diff_hash():
    h_django = compute_obligation_class("x", task_id="django__django-1")
    h_sphinx = compute_obligation_class("x", task_id="sphinx-doc__sphinx-1")
    assert h_django != h_sphinx


def test_obligation_class_diff_subsystem_diff_hash():
    h_db = compute_obligation_class("x", representations=[FakeRep("k", "django/db/backends/x.py")])
    h_http = compute_obligation_class("x", representations=[FakeRep("k", "django/http/handlers/y.py")])
    assert h_db != h_http


def test_obligation_class_same_subsystem_collides():
    """Different DB backends should hash to the same obligation_class."""
    h_pg = compute_obligation_class("x", representations=[
        FakeRep("k", "edit django/db/backends/postgresql/client.py and django/db/backends/base/client.py")
    ])
    h_mysql = compute_obligation_class("x", representations=[
        FakeRep("k", "edit django/db/backends/mysql/client.py and django/db/backends/base/client.py")
    ])
    assert h_pg == h_mysql  # the whole point: cross-backend transfer


def test_state_class_deterministic():
    counts = RunCounts(investigate_count=3, mutate_count=1)
    h1 = compute_state_class("oc1", "IMPLEMENT", "mutate", counts)
    h2 = compute_state_class("oc1", "IMPLEMENT", "mutate", counts)
    assert h1 == h2


def test_state_class_different_obligation_different_hash():
    counts = RunCounts(investigate_count=3)
    h1 = compute_state_class("oc1", "MAP", "investigate", counts)
    h2 = compute_state_class("oc2", "MAP", "investigate", counts)
    assert h1 != h2


def test_state_class_bucketing_collides():
    """Counts in same bucket should collide."""
    c1 = RunCounts(investigate_count=3)
    c2 = RunCounts(investigate_count=5)  # both in bucket [3-5]
    h1 = compute_state_class("oc", "MAP", "investigate", c1)
    h2 = compute_state_class("oc", "MAP", "investigate", c2)
    assert h1 == h2


def test_state_class_bucketing_separates():
    """Counts in different buckets should diverge."""
    c1 = RunCounts(investigate_count=2)   # bucket [1-2]
    c2 = RunCounts(investigate_count=5)   # bucket [3-5]
    h1 = compute_state_class("oc", "MAP", "investigate", c1)
    h2 = compute_state_class("oc", "MAP", "investigate", c2)
    assert h1 != h2


def test_bucket_edges():
    assert _bucket(0, [0, 1, 3, 6, 11]) == 0
    assert _bucket(1, [0, 1, 3, 6, 11]) == 1
    assert _bucket(2, [0, 1, 3, 6, 11]) == 1
    assert _bucket(3, [0, 1, 3, 6, 11]) == 2
    assert _bucket(5, [0, 1, 3, 6, 11]) == 2
    assert _bucket(10, [0, 1, 3, 6, 11]) == 3
    assert _bucket(11, [0, 1, 3, 6, 11]) == 4
    assert _bucket(99, [0, 1, 3, 6, 11]) == 4


def test_run_counts_bump():
    c = RunCounts()
    c.bump("investigate")
    c.bump("investigate")
    c.bump("mutate")
    c.bump("verify")
    c.bump("other")
    assert c.investigate_count == 2
    assert c.mutate_count == 1
    assert c.verify_count == 1


def test_run_counts_durable():
    c = RunCounts()
    c.tick_no_change()
    c.tick_no_change()
    assert c.iter_since_durable_change == 2
    c.mark_durable_change()
    assert c.durable_change_count == 1
    assert c.iter_since_durable_change == 0


def reps_or_none():
    return None
