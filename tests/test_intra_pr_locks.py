"""Tests for the opt-in intra-PR agent-aware locking mode.

DEFAULT (mode OFF): two different agents reserving under the SAME ``pr`` are
invisible to each other's conflict check (today's PR-ownership model). This is
backward-compatible and must never break.

OPT-IN (mode ON, ``intra_pr_exclusive: true`` on the domain, or the
``--intra-pr-exclusive`` CLI flag): two *different* agents on the SAME PR with
overlapping symbols (or a whole-domain request over a sibling agent's symbols)
MUST conflict, exactly like cross-PR symbol overlap. The SAME agent on the SAME
PR stays idempotent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from merge_train.domain_lock import (
    DomainHeldError,
    LockLog,
    Registry,
    reserve,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


@pytest.fixture
def exclusive_domain(tmp_path: Path) -> tuple[LockLog, Registry]:
    """A domain with intra_pr_exclusive ON and a permissive sibling domain OFF."""
    reg = _reg({
        "domains": {
            "guarded": {"paths": ["guarded.py"], "intra_pr_exclusive": True},
            "open": {"paths": ["open.py"]},  # default: intra_pr_exclusive False
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg


# --------------------------------------------------------------------------- #
# (a) mode ON: two different agents, same PR, overlapping symbol -> conflict
# --------------------------------------------------------------------------- #
def test_intra_pr_on_two_agents_same_pr_overlapping_symbol_conflicts(exclusive_domain):
    log, reg = exclusive_domain
    reserve(log, reg, domain="guarded", pr=100, agent="agentA", branch="b",
            symbols=["foo"])
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="guarded", pr=100, agent="agentB", branch="b",
                symbols=["foo", "bar"])
    msg = str(exc.value)
    assert "foo" in msg
    # The blocking holder is the sibling agent on the same PR.
    assert "agentA" in msg


# --------------------------------------------------------------------------- #
# (b) mode ON: two different agents, same PR, DISJOINT symbols -> both succeed
# --------------------------------------------------------------------------- #
def test_intra_pr_on_two_agents_same_pr_disjoint_symbols_coexist(exclusive_domain):
    log, reg = exclusive_domain
    e1 = reserve(log, reg, domain="guarded", pr=100, agent="agentA", branch="b",
                 symbols=["foo"])
    e2 = reserve(log, reg, domain="guarded", pr=100, agent="agentB", branch="b",
                 symbols=["bar"])
    assert e1.symbols == ("foo",)
    assert e2.symbols == ("bar",)
    assert len(log.active_all()) == 2


# --------------------------------------------------------------------------- #
# (c) mode ON: same agent, same PR, re-reserve same/covering symbol -> idempotent
# --------------------------------------------------------------------------- #
def test_intra_pr_on_same_agent_re_reserve_is_idempotent(exclusive_domain):
    log, reg = exclusive_domain
    e1 = reserve(log, reg, domain="guarded", pr=100, agent="agentA", branch="b",
                 symbols=["foo"])
    e2 = reserve(log, reg, domain="guarded", pr=100, agent="agentA", branch="b",
                 symbols=["foo"])
    assert e1 == e2  # idempotent: same entry returned, no duplicate appended
    assert len(log.active_all()) == 1


# --------------------------------------------------------------------------- #
# (d) mode ON: whole-domain request by agent B while agent A (same PR)
#     holds a symbol -> conflict
# --------------------------------------------------------------------------- #
def test_intra_pr_on_whole_domain_over_sibling_symbol_conflicts(exclusive_domain):
    log, reg = exclusive_domain
    reserve(log, reg, domain="guarded", pr=100, agent="agentA", branch="b",
            symbols=["foo"])
    with pytest.raises(DomainHeldError) as exc:
        # whole-domain request (no symbols) by a different agent, same PR
        reserve(log, reg, domain="guarded", pr=100, agent="agentB", branch="b")
    msg = str(exc.value).lower()
    assert "whole-domain" in msg or "refused" in msg


# --------------------------------------------------------------------------- #
# (e) mode OFF (default): two different agents, same PR, same symbol -> both OK
#     (backward-compat — existing PR-ownership model preserved)
# --------------------------------------------------------------------------- #
def test_intra_pr_off_two_agents_same_pr_same_symbol_both_succeed(exclusive_domain):
    log, reg = exclusive_domain
    # 'open' domain has intra_pr_exclusive False (default)
    e1 = reserve(log, reg, domain="open", pr=100, agent="agentA", branch="b",
                 symbols=["foo"])
    # Same PR, different agent, SAME symbol: under default mode this is the
    # PR-ownership idempotency path -> returns the existing entry, no error.
    e2 = reserve(log, reg, domain="open", pr=100, agent="agentB", branch="b",
                 symbols=["foo"])
    assert e1.symbols == ("foo",)
    assert e2.symbols == ("foo",)
    # No conflict raised; no second active entry created (idempotent on PR key).
    assert len(log.active_all()) == 1


def test_intra_pr_off_whole_domain_default_registry_permissive(tmp_path: Path):
    """Sanity: a domain without the flag keeps today's pr-only semantics."""
    reg = _reg({"domains": {"d": {"paths": ["d.py"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="d", pr=5, agent="a1", branch="b")
    # Same PR, different agent, whole-domain re-reserve: idempotent (no raise).
    e = reserve(log, reg, domain="d", pr=5, agent="a2", branch="b")
    assert e.is_whole_domain
    assert len(log.active_all()) == 1


# --------------------------------------------------------------------------- #
# Cross-PR behavior must remain unchanged regardless of the flag.
# --------------------------------------------------------------------------- #
def test_intra_pr_on_cross_pr_overlap_still_conflicts(exclusive_domain):
    log, reg = exclusive_domain
    reserve(log, reg, domain="guarded", pr=100, agent="agentA", branch="b",
            symbols=["foo"])
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="guarded", pr=200, agent="agentB", branch="b2",
                symbols=["foo"])


# --------------------------------------------------------------------------- #
# Registry parsing: intra_pr_exclusive is read off the Domain dataclass.
# --------------------------------------------------------------------------- #
def test_registry_parses_intra_pr_exclusive_flag():
    reg = _reg({
        "domains": {
            "on": {"paths": ["a.py"], "intra_pr_exclusive": True},
            "off": {"paths": ["b.py"]},
        }
    })
    assert reg.domains["on"].intra_pr_exclusive is True
    assert reg.domains["off"].intra_pr_exclusive is False


# --------------------------------------------------------------------------- #
# CLI: --intra-pr-exclusive flag forces the mode ON for a single reserve even
# when the registry domain does not declare it.
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


def test_cli_intra_pr_exclusive_flag_denies_sibling_agent(tmp_path: Path):
    r1 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "agentA", "--branch", "b",
        "--symbols", "foo",
        "--intra-pr-exclusive",
    )
    assert r1.returncode == 0, r1.stderr
    r2 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "agentB", "--branch", "b",
        "--symbols", "foo",
        "--intra-pr-exclusive",
    )
    assert r2.returncode == 1, f"expected DENIED, got stdout={r2.stdout} stderr={r2.stderr}"
    assert "DENIED" in r2.stderr


def test_cli_intra_pr_exclusive_flag_off_sibling_agent_idempotent(tmp_path: Path):
    r1 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "agentA", "--branch", "b",
        "--symbols", "foo",
    )
    assert r1.returncode == 0, r1.stderr
    # Without the flag, same PR different agent same symbol is permissive.
    r2 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "agentB", "--branch", "b",
        "--symbols", "foo",
    )
    assert r2.returncode == 0, f"expected permissive, got stderr={r2.stderr}"
