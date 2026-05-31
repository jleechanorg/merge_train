"""Tests for branch-awareness mode — reserving with --branch and no --pr.

A unified "claim identity" keys all conflict partitioning and idempotency:

    claim = pr if pr is not None else f"branch:{branch}"

PR-keyed reservations keep working byte-for-byte when ``pr`` is supplied
(backward compat). Branch-keyed reservations let agents lock before a PR
exists. The intra-PR agent-aware refinement composes uniformly on top of the
claim identity for both PR- and branch-keyed claims.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from merge_train.domain_lock import (
    DomainHeldError,
    LockEntry,
    LockLog,
    Registry,
    release,
    reserve,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


@pytest.fixture
def shared(tmp_path: Path) -> tuple[LockLog, Registry]:
    reg = _reg({
        "domains": {
            "shared": {"paths": ["shared.py"]},
            "guarded": {"paths": ["guarded.py"], "intra_pr_exclusive": True},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg


# --------------------------------------------------------------------------- #
# (a) reserve by --branch, no --pr, succeeds
# --------------------------------------------------------------------------- #
def test_branch_only_reserve_succeeds(shared):
    log, reg = shared
    e = reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
                symbols=["foo"])
    assert e.pr is None
    assert e.branch == "feat/x"
    assert e.symbols == ("foo",)
    assert len(log.active_all()) == 1


# --------------------------------------------------------------------------- #
# (b) two DIFFERENT branches (no PR) overlapping symbols -> conflict
# --------------------------------------------------------------------------- #
def test_two_different_branches_overlapping_symbols_conflict(shared):
    log, reg = shared
    reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
            symbols=["foo"])
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="shared", pr=None, agent="a2", branch="feat/y",
                symbols=["foo", "bar"])
    assert "foo" in str(exc.value)


def test_two_different_branches_disjoint_symbols_coexist(shared):
    log, reg = shared
    reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
            symbols=["foo"])
    reserve(log, reg, domain="shared", pr=None, agent="a2", branch="feat/y",
            symbols=["bar"])
    assert len(log.active_all()) == 2


# --------------------------------------------------------------------------- #
# (c) same branch re-reserve is idempotent
# --------------------------------------------------------------------------- #
def test_same_branch_re_reserve_idempotent(shared):
    log, reg = shared
    e1 = reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
                 symbols=["foo"])
    e2 = reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
                 symbols=["foo"])
    assert e1 == e2
    assert len(log.active_all()) == 1


# --------------------------------------------------------------------------- #
# (d) PR-keyed path unchanged when --pr given (back-compat)
# --------------------------------------------------------------------------- #
def test_pr_keyed_path_unchanged(shared):
    log, reg = shared
    e = reserve(log, reg, domain="shared", pr=42, agent="a", branch="feat/x",
                symbols=["foo"])
    assert e.pr == 42
    # Cross-PR overlap still conflicts.
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="shared", pr=43, agent="a2", branch="feat/y",
                symbols=["foo"])


def test_pr_and_branch_claims_are_distinct(shared):
    """A PR-keyed claim and a branch-keyed claim are different identities
    and do not idempotently merge — overlapping symbols conflict across them."""
    log, reg = shared
    reserve(log, reg, domain="shared", pr=7, agent="a", branch="feat/x",
            symbols=["foo"])
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
                symbols=["foo"])


# --------------------------------------------------------------------------- #
# (e) composition: branch-keyed + intra-PR-agent-aware ON
# --------------------------------------------------------------------------- #
def test_branch_keyed_intra_pr_exclusive_two_agents_overlap_conflict(shared):
    log, reg = shared
    reserve(log, reg, domain="guarded", pr=None, agent="agentA", branch="feat/x",
            symbols=["foo"])
    with pytest.raises(DomainHeldError):
        # same branch claim, different agent, overlapping symbol -> conflict
        reserve(log, reg, domain="guarded", pr=None, agent="agentB", branch="feat/x",
                symbols=["foo"])


def test_branch_keyed_intra_pr_exclusive_two_agents_disjoint_succeed(shared):
    log, reg = shared
    reserve(log, reg, domain="guarded", pr=None, agent="agentA", branch="feat/x",
            symbols=["foo"])
    reserve(log, reg, domain="guarded", pr=None, agent="agentB", branch="feat/x",
            symbols=["bar"])
    assert len(log.active_all()) == 2


def test_branch_keyed_intra_pr_exclusive_off_default_permissive(shared):
    """Branch-keyed, non-guarded domain: same claim, different agent, same
    symbol is permissive (default mode) — idempotent on the claim identity."""
    log, reg = shared
    e1 = reserve(log, reg, domain="shared", pr=None, agent="agentA", branch="feat/x",
                 symbols=["foo"])
    e2 = reserve(log, reg, domain="shared", pr=None, agent="agentB", branch="feat/x",
                 symbols=["foo"])
    assert e1.symbols == ("foo",)
    assert e2.symbols == ("foo",)
    assert len(log.active_all()) == 1


# --------------------------------------------------------------------------- #
# release / list match branch-keyed claims
# --------------------------------------------------------------------------- #
def test_release_branch_keyed_claim(shared):
    log, reg = shared
    reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/x",
            symbols=["foo"])
    released = release(log, branch="feat/x")
    assert len(released) == 1
    assert released[0].branch == "feat/x"
    assert log.active_all() == []


def test_release_branch_does_not_touch_pr_claim(shared):
    log, reg = shared
    reserve(log, reg, domain="shared", pr=10, agent="a", branch="feat/x",
            symbols=["foo"])
    reserve(log, reg, domain="shared", pr=None, agent="a", branch="feat/y",
            symbols=["bar"])
    released = release(log, branch="feat/y")
    assert len(released) == 1
    assert released[0].pr is None
    remaining = log.active_all()
    assert len(remaining) == 1
    assert remaining[0].pr == 10


# --------------------------------------------------------------------------- #
# LockEntry serialization with pr=None round-trips and stays back-compat.
# --------------------------------------------------------------------------- #
def test_lock_entry_pr_none_round_trip():
    e = LockEntry(
        domain="d", pr=None, agent="a", branch="feat/x",
        opened_at="t", status="active", symbols=("foo",),
    )
    s = e.to_json()
    e2 = LockEntry.from_json(s)
    assert e2.pr is None
    assert e2 == e


def test_lock_entry_legacy_int_pr_still_parses():
    legacy = '{"domain":"d","pr":1,"agent":"a","branch":"b","opened_at":"t","status":"active"}'
    e = LockEntry.from_json(legacy)
    assert e.pr == 1


# --------------------------------------------------------------------------- #
# CLI: reserve with --branch and no --pr; both missing is an error.
# --------------------------------------------------------------------------- #
def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    reg = tmp_path / "reg.yaml"
    log = tmp_path / "log.jsonl"
    if not reg.exists():
        reg.write_text(yaml.safe_dump({
            "domains": {"shared": {"paths": ["shared.py"]}}
        }))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log), *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_cli_reserve_branch_only_succeeds(tmp_path: Path):
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared",
        "--agent", "a", "--branch", "feat/x",
        "--symbols", "foo",
    )
    assert r.returncode == 0, r.stderr
    assert "RESERVED" in r.stdout


def test_cli_reserve_requires_pr_or_branch(tmp_path: Path):
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared",
        "--agent", "a",
        "--symbols", "foo",
    )
    assert r.returncode == 2
    assert "pr" in r.stderr.lower() or "branch" in r.stderr.lower()


def test_cli_release_by_branch(tmp_path: Path):
    r1 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared",
        "--agent", "a", "--branch", "feat/x",
        "--symbols", "foo",
    )
    assert r1.returncode == 0, r1.stderr
    r2 = _run_cli(tmp_path, "release", "--branch", "feat/x", "--force")
    assert r2.returncode == 0, r2.stderr
    assert "RELEASED" in r2.stdout
