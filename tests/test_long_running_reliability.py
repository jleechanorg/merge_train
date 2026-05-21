"""Long-running reliability tests for merge_train.domain_lock."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

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


def _setup(tmp_path: Path) -> tuple[LockLog, Registry]:
    reg = _reg({
        "domains": {
            "core": {"paths": ["core.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg


def test_sequential_batches_per_batch_no_active_locks(tmp_path: Path):
    log, reg = _setup(tmp_path)
    num_batches = 10
    slots = 20
    for batch in range(num_batches):
        base_pr = batch * slots + 1
        for slot in range(slots):
            pr = base_pr + slot
            reserve(log, reg, domain="core", pr=pr, agent="a", branch="b",
                    symbols=[f"sym_b{batch}_s{slot}"])
        for slot in range(slots):
            release(log, pr=base_pr + slot, domain="core")
        batch_active = [e for e in log.active_all()
                        if base_pr <= e.pr < base_pr + slots]
        assert batch_active == [], f"batch {batch} leaked locks"


def test_total_entry_count_after_all_batches(tmp_path: Path):
    log, reg = _setup(tmp_path)
    num_batches = 10
    slots = 20
    for batch in range(num_batches):
        base_pr = batch * slots + 1
        for slot in range(slots):
            pr = base_pr + slot
            reserve(log, reg, domain="core", pr=pr, agent="a", branch="b",
                    symbols=[f"sym_b{batch}_s{slot}"])
        for slot in range(slots):
            release(log, pr=base_pr + slot, domain="core")
    assert len(log.entries()) == num_batches * slots * 2


def test_no_lock_leaks_active_all_empty(tmp_path: Path):
    log, reg = _setup(tmp_path)
    num_batches = 10
    slots = 20
    for batch in range(num_batches):
        base_pr = batch * slots + 1
        for slot in range(slots):
            pr = base_pr + slot
            reserve(log, reg, domain="core", pr=pr, agent="a", branch="b",
                    symbols=[f"sym_b{batch}_s{slot}"])
        for slot in range(slots):
            release(log, pr=base_pr + slot, domain="core")
    assert log.active_all() == []


def test_interleaved_batches_random_release_clean(tmp_path: Path):
    log, reg = _setup(tmp_path)
    batch1_prs = list(range(1, 11))
    batch2_prs = list(range(11, 21))
    for pr in batch1_prs:
        reserve(log, reg, domain="core", pr=pr, agent="a1", branch="b1",
                symbols=[f"sym_{pr}"])
    for pr in batch2_prs:
        reserve(log, reg, domain="core", pr=pr, agent="a2", branch="b2",
                symbols=[f"sym_{pr}"])
    all_prs = batch1_prs + batch2_prs
    random.shuffle(all_prs)
    for pr in all_prs:
        release(log, pr=pr, domain="core")
    assert log.active_all() == []


def test_crashed_process_leaves_stale_locks_next_batch_sees_held(tmp_path: Path):
    log, reg = _setup(tmp_path)
    crashed_pr = 100
    reserve(log, reg, domain="core", pr=crashed_pr, agent="crashed",
            branch="dead", symbols=["stale_a", "stale_b"])
    assert len(log.active_all()) == 1
    assert log.active_all()[0].pr == crashed_pr
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="core", pr=200, agent="next", branch="b",
                symbols=["stale_a"])
    assert "stale_a" in str(exc.value)
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="core", pr=200, agent="next", branch="b")
    release(log, pr=crashed_pr, domain="core")
    assert log.active_all() == []
    reserve(log, reg, domain="core", pr=200, agent="next", branch="b",
            symbols=["stale_a"])
    assert len(log.active_all()) == 1


def test_log_integrity_all_lines_parseable(tmp_path: Path):
    log, reg = _setup(tmp_path)
    num_batches = 10
    slots = 20
    for batch in range(num_batches):
        base_pr = batch * slots + 1
        for slot in range(slots):
            pr = base_pr + slot
            reserve(log, reg, domain="core", pr=pr, agent="a", branch="b",
                    symbols=[f"sym_b{batch}_s{slot}"])
        for slot in range(slots):
            release(log, pr=base_pr + slot, domain="core")
    log_path = tmp_path / "log.jsonl"
    assert log_path.exists()
    raw_lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(raw_lines) == num_batches * slots * 2
    for i, line in enumerate(raw_lines):
        parsed = json.loads(line)
        entry = LockEntry.from_json(line)
        assert isinstance(entry, LockEntry)
        assert entry.domain == "core"
        assert entry.status in ("active", "released")
    first_line = raw_lines[0]
    first_entry = LockEntry.from_json(first_line)
    assert first_entry.status == "active"
    assert first_entry.pr == 1
    assert first_entry.symbols == ("sym_b0_s0",)
    entries_via_api = log.entries()
    assert len(entries_via_api) == len(raw_lines)


def test_log_grows_but_active_all_stays_bounded(tmp_path: Path):
    log, reg = _setup(tmp_path)
    fixed_prs = [1, 2, 3]
    num_cycles = 5
    for cycle in range(num_cycles):
        for pr in fixed_prs:
            reserve(log, reg, domain="core", pr=pr, agent="a", branch="b",
                    symbols=[f"sym_pr{pr}_c{cycle}"])
        for pr in fixed_prs:
            release(log, pr=pr, domain="core")
        assert log.active_all() == []
    total_entries = len(log.entries())
    assert total_entries == len(fixed_prs) * 2 * num_cycles
    assert log.active_all() == []
    mid_pr = 99
    reserve(log, reg, domain="core", pr=mid_pr, agent="a", branch="b",
            symbols=["mid_sym"])
    assert len(log.active_all()) == 1
    release(log, pr=mid_pr, domain="core")
    assert log.active_all() == []
