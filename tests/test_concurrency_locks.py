"""Tests for concurrency and race safety in merge_train."""

from __future__ import annotations

import multiprocessing
import tempfile
from pathlib import Path
import pytest

from merge_train.domain_lock import (
    DomainHeldError,
    LockLog,
    Registry,
    reserve,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


def _attempt_reserve(args) -> str:
    """Worker function to attempt reservation of a domain in a separate process.

    Returns:
        "success" if reserved, "held" if DomainHeldError was raised, or the exception string.
    """
    log_path, reg_dict, domain, pr, agent, branch = args
    reg = _reg(reg_dict)
    log = LockLog(log_path)
    try:
        reserve(log, reg, domain=domain, pr=pr, agent=agent, branch=branch)
        return "success"
    except DomainHeldError:
        return "held"
    except Exception as e:
        return str(e)


def test_concurrent_reserve_only_one_wins_multiprocessing():
    """Verify that multiple processes racing to reserve the same domain has exactly one winner."""
    reg_dict = {
        "domains": {
            "exclusive": {"paths": ["a.py"]},
        }
    }
    
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "locks.jsonl"
        
        # We spawn 15 concurrent processes to race on the lock
        num_processes = 15
        pool_args = [
            (
                log_path,
                reg_dict,
                "exclusive",
                i + 1,  # distinct PRs so they collide
                f"agent-{i}",
                f"branch-{i}"
            )
            for i in range(num_processes)
        ]
        
        with multiprocessing.Pool(processes=num_processes) as pool:
            results = pool.map(_attempt_reserve, pool_args)
            
        success_count = results.count("success")
        held_count = results.count("held")
        
        # Verify that exactly one process got the lock, and the rest were blocked
        assert success_count == 1, f"Expected exactly 1 success, got {success_count} (results: {results})"
        assert held_count == num_processes - 1, f"Expected {num_processes - 1} held, got {held_count}"
