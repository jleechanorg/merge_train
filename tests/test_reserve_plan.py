"""Tests for atomic multi-leg reservation: reserve_plan + CLI reserve-plan."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from merge_train.domain_lock import (
    DomainHeldError,
    LockLog,
    PlanItem,
    Registry,
    UnknownPathError,
    release,
    reserve,
    reserve_plan,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


@pytest.fixture
def three_domains(tmp_path: Path) -> tuple[LockLog, Registry]:
    reg = _reg({
        "domains": {
            "rewards-engine": {"paths": ["mvp_site/rewards_engine.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
            "world-logic": {"paths": ["mvp_site/world_logic.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg


# --------------------------------------------------------------------------- #
# reserve_plan happy path
# --------------------------------------------------------------------------- #


def test_reserve_plan_three_legs_all_succeed(three_domains):
    log, reg = three_domains
    plan = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="agents", symbols=("route_action",)),
        PlanItem(domain="world-logic", symbols=("freeze_choices",)),
    ]
    entries = reserve_plan(
        log, reg,
        pr=6926, agent="A", branch="feat/A", plan=plan,
    )
    assert len(entries) == 3
    assert [e.domain for e in entries] == [
        "rewards-engine", "agents", "world-logic",
    ]
    assert len(log.active_all()) == 3


def test_reserve_plan_accepts_dict_items(three_domains):
    log, reg = three_domains
    plan = [
        {"domain": "rewards-engine", "symbols": ["canonicalize_rewards"]},
        {"domain": "agents", "symbols": ["route_action"]},
    ]
    entries = reserve_plan(
        log, reg,
        pr=6926, agent="A", branch="feat/A", plan=plan,
    )
    assert len(entries) == 2


def test_reserve_plan_dict_without_symbols_is_whole_domain(three_domains):
    log, reg = three_domains
    plan = [{"domain": "agents"}]
    entries = reserve_plan(
        log, reg,
        pr=1, agent="A", branch="b", plan=plan,
    )
    assert len(entries) == 1
    assert entries[0].is_whole_domain


def test_reserve_plan_two_workers_disjoint_across_shared_files(three_domains):
    log, reg = three_domains
    plan_a = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="agents", symbols=("route_action",)),
    ]
    reserve_plan(log, reg, pr=6926, agent="A", branch="feat/A", plan=plan_a)

    plan_b = [
        PlanItem(domain="rewards-engine", symbols=("compute_xp_to_level",)),
        PlanItem(domain="agents", symbols=("summarize",)),
    ]
    reserve_plan(log, reg, pr=6927, agent="B", branch="feat/B", plan=plan_b)

    active = log.active_all()
    assert len(active) == 4
    prs = {e.pr for e in active}
    assert prs == {6926, 6927}


# --------------------------------------------------------------------------- #
# reserve_plan rollback semantics
# --------------------------------------------------------------------------- #


def test_reserve_plan_rolls_back_on_third_leg_conflict(three_domains):
    """Worker B asks for symbols [A-free, A-free, A-HELD] => no leg sticks."""
    log, reg = three_domains
    # Worker A holds world-logic::freeze_choices
    reserve(log, reg, domain="world-logic", pr=6926, agent="A", branch="feat/A",
            symbols=["freeze_choices"])

    # Worker B's plan: 1st & 2nd legs are free, 3rd collides on freeze_choices
    plan_b = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="agents", symbols=("route_action",)),
        PlanItem(domain="world-logic", symbols=("freeze_choices",)),
    ]
    with pytest.raises(DomainHeldError):
        reserve_plan(log, reg, pr=6927, agent="B", branch="feat/B", plan=plan_b)

    # Only PR #6926's original lock should remain active.
    active = log.active_all()
    assert len(active) == 1
    assert active[0].pr == 6926
    assert active[0].domain == "world-logic"

    # The two earlier 'active' rows for PR #6927 must have a matching
    # 'released' row with note rollback:reserve_plan.
    rollbacks = [
        e for e in log.entries()
        if e.pr == 6927 and e.status == "released" and e.note == "rollback:reserve_plan"
    ]
    assert len(rollbacks) == 2


def test_reserve_plan_rolls_back_on_unknown_domain(three_domains):
    log, reg = three_domains
    plan = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="does-not-exist", symbols=("foo",)),
    ]
    with pytest.raises(UnknownPathError):
        reserve_plan(log, reg, pr=1, agent="A", branch="b", plan=plan)
    # Nothing remains active.
    assert log.active_all() == []


def test_reserve_plan_after_rollback_can_retry_with_disjoint_plan(three_domains):
    log, reg = three_domains
    reserve(log, reg, domain="world-logic", pr=6926, agent="A", branch="feat/A",
            symbols=["freeze_choices"])

    # First attempt collides on freeze_choices.
    bad_plan = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="world-logic", symbols=("freeze_choices",)),
    ]
    with pytest.raises(DomainHeldError):
        reserve_plan(log, reg, pr=6927, agent="B", branch="feat/B", plan=bad_plan)

    # Retry with a disjoint symbol on world-logic.
    good_plan = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="world-logic", symbols=("apply_action",)),
    ]
    entries = reserve_plan(
        log, reg, pr=6927, agent="B", branch="feat/B", plan=good_plan,
    )
    assert len(entries) == 2
    # Now: A's freeze_choices + B's two new legs = 3 active.
    assert len(log.active_all()) == 3


def test_reserve_plan_release_clears_all_legs(three_domains):
    log, reg = three_domains
    plan = [
        PlanItem(domain="rewards-engine", symbols=("canonicalize_rewards",)),
        PlanItem(domain="agents", symbols=("route_action",)),
    ]
    reserve_plan(log, reg, pr=6926, agent="A", branch="feat/A", plan=plan)
    released = release(log, pr=6926)
    assert len(released) == 2
    assert log.active_all() == []


# --------------------------------------------------------------------------- #
# CLI reserve-plan
# --------------------------------------------------------------------------- #


def _write_registry(tmp_path: Path) -> Path:
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {
            "rewards-engine": {"paths": ["mvp_site/rewards_engine.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
            "world-logic": {"paths": ["mvp_site/world_logic.py"]},
        }
    }))
    return reg


def _run(tmp_path: Path, reg: Path, log: Path, *args: str):
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log), *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_cli_reserve_plan_three_legs(tmp_path: Path):
    reg = _write_registry(tmp_path)
    log = tmp_path / "log.jsonl"
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "rewards-engine", "symbols": ["canonicalize_rewards"]},
            {"domain": "agents", "symbols": ["route_action"]},
            {"domain": "world-logic", "symbols": ["freeze_choices"]},
        ]
    }))
    r = _run(
        tmp_path, reg, log,
        "reserve-plan", "--pr", "6926", "--agent", "A", "--branch", "feat/A",
        "--plan", str(plan_path),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("RESERVED:") == 3


def test_cli_reserve_plan_atomic_rollback(tmp_path: Path):
    reg = _write_registry(tmp_path)
    log = tmp_path / "log.jsonl"
    # Pre-reserve world-logic::freeze_choices for PR #6926
    _run(
        tmp_path, reg, log,
        "reserve", "--domain", "world-logic", "--pr", "6926",
        "--agent", "A", "--branch", "feat/A",
        "--symbols", "freeze_choices",
    )
    # PR #6927's plan: two free legs + one held leg
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "rewards-engine", "symbols": ["canonicalize_rewards"]},
            {"domain": "agents", "symbols": ["route_action"]},
            {"domain": "world-logic", "symbols": ["freeze_choices"]},
        ]
    }))
    r = _run(
        tmp_path, reg, log,
        "reserve-plan", "--pr", "6927", "--agent", "B", "--branch", "feat/B",
        "--plan", str(plan_path),
    )
    assert r.returncode == 1
    assert "DENIED" in r.stderr
    assert "rolled back" in r.stderr
    # list active: only PR #6926's lock should remain
    r2 = _run(tmp_path, reg, log, "list", "--json")
    assert r2.returncode == 0
    locks = json.loads(r2.stdout)
    assert len(locks) == 1
    assert locks[0]["pr"] == 6926


def test_cli_reserve_plan_accepts_reservations_key(tmp_path: Path):
    """Plan file may use 'reservations' instead of 'plan' as the top key."""
    reg = _write_registry(tmp_path)
    log = tmp_path / "log.jsonl"
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump({
        "reservations": [
            {"domain": "rewards-engine", "symbols": ["canonicalize_rewards"]},
        ]
    }))
    r = _run(
        tmp_path, reg, log,
        "reserve-plan", "--pr", "1", "--agent", "A", "--branch", "b",
        "--plan", str(plan_path),
    )
    assert r.returncode == 0, r.stderr


def test_cli_reserve_plan_empty_plan_error(tmp_path: Path):
    reg = _write_registry(tmp_path)
    log = tmp_path / "log.jsonl"
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump({"plan": []}))
    r = _run(
        tmp_path, reg, log,
        "reserve-plan", "--pr", "1", "--agent", "A", "--branch", "b",
        "--plan", str(plan_path),
    )
    assert r.returncode == 2


def test_cli_reserve_plan_missing_file_error(tmp_path: Path):
    reg = _write_registry(tmp_path)
    log = tmp_path / "log.jsonl"
    r = _run(
        tmp_path, reg, log,
        "reserve-plan", "--pr", "1", "--agent", "A", "--branch", "b",
        "--plan", str(tmp_path / "nope.yaml"),
    )
    assert r.returncode == 2
