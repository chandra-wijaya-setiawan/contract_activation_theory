"""Unit tests for Contract, state machine, Constraints, Criterion."""
from __future__ import annotations
from pathlib import Path

import pytest

from cat.contracts import (
    Constraints,
    Contract,
    ContractState,
    Cost,
    Criterion,
    Evidence,
    State,
    deterministic_criterion,
)


# ── State machine ───────────────────────────────────────────────────────────


def test_initial_state_is_latent():
    c = Contract.new(obligation="anything", criteria=[])
    assert c.state == ContractState.LATENT


def test_allowed_transitions():
    c = Contract.new(obligation="x", criteria=[])
    assert c.can_transition(ContractState.ELIGIBLE)
    assert c.can_transition(ContractState.FAILED)
    assert not c.can_transition(ContractState.FULFILLED)  # cannot skip ACTIVE
    assert not c.can_transition(ContractState.ACTIVE)     # must go through ELIGIBLE


def test_terminal_states_disallow_further_transitions():
    c = Contract.new(obligation="x", criteria=[])
    c.state = ContractState.FULFILLED
    for s in ContractState:
        assert not c.can_transition(s)


# ── Cost ────────────────────────────────────────────────────────────────────


def test_cost_add_accumulates_all_fields():
    a = Cost(tokens_in=10, tokens_out=20, tool_calls=1, usd=0.01, wall_s=0.5, llm_calls=1)
    b = Cost(tokens_in=5, tokens_out=15, tool_calls=2, usd=0.02, wall_s=1.0, llm_calls=3)
    a.add(b)
    assert a.tokens_in == 15
    assert a.tokens_out == 35
    assert a.tokens_total == 50
    assert a.tool_calls == 3
    assert a.usd == pytest.approx(0.03)
    assert a.wall_s == pytest.approx(1.5)
    assert a.llm_calls == 4


# ── Constraints ─────────────────────────────────────────────────────────────


def test_constraints_passes_under_limits():
    cons = Constraints(max_tokens_total=100, max_tool_calls=5, max_usd=1.0)
    assert cons.check(Cost(tokens_in=10, tokens_out=10, tool_calls=2, usd=0.5), depth=0, now_ts=0) is None


def test_constraints_flags_token_overrun():
    cons = Constraints(max_tokens_total=50)
    msg = cons.check(Cost(tokens_in=30, tokens_out=30), depth=0, now_ts=0)
    assert msg is not None
    assert "tokens_total" in msg


def test_constraints_flags_depth():
    cons = Constraints(max_depth=2)
    assert cons.check(Cost(), depth=3, now_ts=0) is not None
    assert cons.check(Cost(), depth=2, now_ts=0) is None


def test_constraints_flags_deadline():
    cons = Constraints(deadline_ts=100.0)
    assert cons.check(Cost(), depth=0, now_ts=101.0) is not None
    assert cons.check(Cost(), depth=0, now_ts=99.0) is None


# ── Criterion ───────────────────────────────────────────────────────────────


def test_deterministic_criterion_pass(tmp_path: Path):
    crit = deterministic_criterion(
        id="exists",
        description="file exists",
        predicate=lambda s: (s.workdir / "x.txt").exists(),
    )
    state = State(workdir=tmp_path)
    (tmp_path / "x.txt").write_text("hi")
    assert crit.check(state) is True
    assert crit.satisfied
    assert crit.last_evidence is not None and "PASS" in crit.last_evidence.summary


def test_deterministic_criterion_fail(tmp_path: Path):
    crit = deterministic_criterion(
        id="exists",
        description="file exists",
        predicate=lambda s: (s.workdir / "missing.txt").exists(),
    )
    state = State(workdir=tmp_path)
    assert crit.check(state) is False
    assert not crit.satisfied


def test_contract_all_criteria_met_requires_all():
    state = State(workdir=Path("/tmp/whatever"))
    c1 = Criterion(id="c1", description="d", predicate=lambda s: (True, Evidence(summary="ok")))
    c2 = Criterion(id="c2", description="d", predicate=lambda s: (False, Evidence(summary="no")))
    ctr = Contract.new(obligation="x", criteria=[c1, c2])
    c1.check(state)
    c2.check(state)
    assert not ctr.all_criteria_met()
    # Flip c2
    ctr.criteria[1] = Criterion(id="c2b", description="d", predicate=lambda s: (True, Evidence(summary="ok")))
    ctr.criteria[1].check(state)
    assert ctr.all_criteria_met()
