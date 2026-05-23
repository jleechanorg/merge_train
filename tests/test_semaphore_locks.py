"""Tests for semaphore (concurrency_limit > 1) locks in merge_train."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from merge_train.domain_lock import (
    DomainHeldError,
    LockEntry,
    LockLog,
    Registry,
    check,
    reserve,
    release,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


def test_registry_parses_concurrency_limit():
    reg = _reg({
        "domains": {
            "mutex": {"paths": ["a.py"]},
            "semaphore": {"paths": ["b.py"], "concurrency_limit": 3},
        }
    })
    assert reg.domains["mutex"].concurrency_limit == 1
    assert reg.domains["semaphore"].concurrency_limit == 3


def test_semaphore_allows_concurrent_reservations_up_to_limit():
    reg = _reg({
        "domains": {
            "shared": {"paths": ["shared.py"], "concurrency_limit": 2},
        }
    })

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "locks.jsonl"
        log = LockLog(log_path)

        # PR 1 reserves: allowed
        reserve(log, reg, domain="shared", pr=1, agent="agent1", branch="b1")
        
        # Checking for PR 2: should be FREE (current distinct PRs = 1, limit = 2)
        res = check(log, reg, files=["shared.py"], pr=2)
        assert res.ok
        assert "shared" in res.free

        # PR 2 reserves: allowed
        reserve(log, reg, domain="shared", pr=2, agent="agent2", branch="b2")

        # Now distinct active PRs = 2 (limit reached). Checking for PR 3 should fail!
        res3 = check(log, reg, files=["shared.py"], pr=3)
        assert not res3.ok
        assert len(res3.held) == 1
        assert res3.held[0][0] == "shared"

        # PR 3 attempting to reserve should raise DomainHeldError
        with pytest.raises(DomainHeldError):
            reserve(log, reg, domain="shared", pr=3, agent="agent3", branch="b3")

        # PR 1 checks again: should be free (own-PR carve-out applies, so distinct other is only PR 2)
        res1 = check(log, reg, files=["shared.py"], pr=1)
        assert res1.ok

        # PR 1 releases: distinct active PR count becomes 1
        release(log, pr=1)

        # Checking for PR 3 should now be FREE!
        res3_retry = check(log, reg, files=["shared.py"], pr=3)
        assert res3_retry.ok
        
        # PR 3 reserves: allowed
        reserve(log, reg, domain="shared", pr=3, agent="agent3", branch="b3")


def test_semaphore_symbol_level_collisions():
    reg = _reg({
        "domains": {
            "shared": {"paths": ["shared.py"], "concurrency_limit": 2},
        }
    })

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "locks.jsonl"
        log = LockLog(log_path)

        # PR 1 reserves symbol "foo"
        reserve(log, reg, domain="shared", pr=1, agent="agent1", branch="b1", symbols=("foo",))

        # PR 2 checks symbol "bar" (different symbol, and under limit): should be free
        res = check(log, reg, files=["shared.py"], pr=2, touched_symbols_by_path={"shared.py": {"bar"}})
        assert res.ok

        # PR 2 checks symbol "foo" (overlapping symbol): should fail even though under PR count limit!
        res_fail = check(log, reg, files=["shared.py"], pr=2, touched_symbols_by_path={"shared.py": {"foo"}})
        assert not res_fail.ok
        assert res_fail.held[0][0] == "shared"
