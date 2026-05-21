"""Merge-queue stress tests for merge_train.domain_lock.

Simulates concurrent merge-queue pressure on the lock registry using
subprocess-based multi-process contention.
"""

from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from merge_train.domain_lock import (
    DomainHeldError,
    LockEntry,
    LockLog,
    PlanItem,
    Registry,
    check,
    release,
    reserve,
    reserve_plan,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


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


def _worker_reserve_symbols(args):
    log_path, reg_data, domain, pr, symbols = args
    from merge_train.domain_lock import LockLog, Registry, reserve, DomainHeldError
    log = LockLog(log_path)
    reg = Registry.from_dict(reg_data)
    try:
        entry = reserve(log, reg, domain=domain, pr=pr, agent=f"agent-{pr}",
                        branch=f"branch-{pr}", symbols=symbols,
                        now="2026-05-20T00:00:00Z")
        return ("ok", pr, entry.symbols)
    except DomainHeldError:
        return ("denied", pr, ())


def _worker_reserve_plan(args):
    log_path, reg_data, pr, plan_items = args
    from merge_train.domain_lock import LockLog, Registry, reserve_plan, DomainHeldError
    log = LockLog(log_path)
    reg = Registry.from_dict(reg_data)
    try:
        entries = reserve_plan(
            log, reg, pr=pr, agent=f"agent-{pr}", branch=f"branch-{pr}",
            plan=plan_items, now="2026-05-20T00:00:00Z",
        )
        return ("ok", pr, [(e.domain, e.symbols) for e in entries])
    except DomainHeldError:
        return ("denied", pr, [])


def _worker_release(args):
    log_path, pr = args
    from merge_train.domain_lock import LockLog, release
    log = LockLog(log_path)
    released = release(log, pr=pr, now="2026-05-20T00:00:01Z")
    return (pr, len(released))


def _worker_check(args):
    log_path, reg_data, files, pr = args
    from merge_train.domain_lock import LockLog, Registry, check
    log = LockLog(log_path)
    reg = Registry.from_dict(reg_data)
    result = check(log, reg, files=files, pr=pr)
    return (pr, result.ok, len(result.free), len(result.held))


def test_50_concurrent_prs_overlapping_symbol_contention(tmp_path: Path):
    reg_data = {
        "domains": {
            "shared": {"paths": ["shared.py"]},
        }
    }
    log_path = tmp_path / "log.jsonl"
    symbol_pool = [f"sym_{i}" for i in range(25)]
    args_list = []
    for i in range(50):
        pr = i + 1
        s1 = symbol_pool[i % 25]
        s2 = symbol_pool[(i + 1) % 25]
        args_list.append((log_path, reg_data, "shared", pr, [s1, s2]))
    with multiprocessing.Pool(processes=8) as pool:
        results = pool.map(_worker_reserve_symbols, args_list)
    ok_prs = [r for r in results if r[0] == "ok"]
    denied_prs = [r for r in results if r[0] == "denied"]
    assert len(ok_prs) >= 1
    assert len(ok_prs) + len(denied_prs) == 50
    held_symbols: set[str] = set()
    for _, _, syms in ok_prs:
        new = set(syms)
        assert not held_symbols.intersection(new), (
            f"overlap detected: {held_symbols & new}"
        )
        held_symbols.update(new)
    log = LockLog(log_path)
    active = log.active_all()
    assert len(active) == len(ok_prs)
    for entry in active:
        assert entry.status == "active"


def test_reserve_plan_atomicity_under_contention(tmp_path: Path):
    reg_data = {
        "domains": {
            "alpha": {"paths": ["a.py"]},
            "beta": {"paths": ["b.py"]},
        }
    }
    log_path = tmp_path / "log.jsonl"
    log = LockLog(log_path)
    reserve(log, _reg(reg_data), domain="alpha", pr=999, agent="blocker",
            branch="b", symbols=["x1"], now="2026-05-20T00:00:00Z")
    args_list = []
    for i in range(10):
        pr = i + 1
        plan = [
            {"domain": "alpha", "symbols": ["x1"]},
            {"domain": "beta", "symbols": ["y1"]},
        ]
        args_list.append((log_path, reg_data, pr, plan))
    with multiprocessing.Pool(processes=4) as pool:
        results = pool.map(_worker_reserve_plan, args_list)
    ok_results = [r for r in results if r[0] == "ok"]
    denied_results = [r for r in results if r[0] == "denied"]
    assert len(ok_results) == 0
    assert len(denied_results) == 10
    active = log.active_all()
    beta_active = [e for e in active if e.domain == "beta"]
    assert len(beta_active) == 0
    rollback_entries = [
        e for e in log.entries()
        if e.status == "released" and e.note == "rollback:reserve_plan"
    ]
    assert len(rollback_entries) == 0


def test_flock_prevents_race_condition_on_whole_domain(tmp_path: Path):
    reg_path = tmp_path / "reg.yaml"
    log_path = tmp_path / "log.jsonl"
    reg_data = {"domains": {"shared": {"paths": ["shared.py"]}}}
    reg_path.write_text(yaml.safe_dump(reg_data))
    procs = []
    for i in range(20):
        cmd = [
            sys.executable, "-m", "merge_train.domain_lock",
            "--registry", str(reg_path), "--log", str(log_path),
            "reserve",
            "--domain", "shared", "--pr", str(i + 1),
            "--agent", f"a{i}", "--branch", f"b{i}",
        ]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append(p)
    outcomes = []
    for p in procs:
        p.wait()
        outcomes.append(p.returncode)
    successes = [rc for rc in outcomes if rc == 0]
    denials = [rc for rc in outcomes if rc == 1]
    assert len(successes) == 1
    assert len(denials) == 19
    log = LockLog(log_path)
    assert len(log.active_all()) == 1


def test_concurrent_release_no_log_corruption(tmp_path: Path):
    reg = _reg({"domains": {"shared": {"paths": ["shared.py"]}}})
    log_path = tmp_path / "log.jsonl"
    log = LockLog(log_path)
    num_prs = 25
    for i in range(num_prs):
        reserve(log, reg, domain="shared", pr=i + 1, agent=f"a{i}",
                branch=f"b{i}", symbols=[f"sym_{i}"],
                now="2026-05-20T00:00:00Z")
    assert len(log.active_all()) == num_prs
    args_list = [(log_path, i + 1) for i in range(num_prs)]
    with multiprocessing.Pool(processes=8) as pool:
        results = pool.map(_worker_release, args_list)
    total_released = sum(count for _, count in results)
    assert total_released == num_prs
    raw_lines = log_path.read_text().strip().splitlines()
    for line_no, line in enumerate(raw_lines):
        data = json.loads(line)
        assert "domain" in data
        assert "status" in data
        assert "pr" in data
    assert len(log.active_all()) == 0


def test_20_prs_disjoint_symbols_cotenancy(tmp_path: Path):
    reg = _reg({"domains": {"shared": {"paths": ["shared.py"]}}})
    log_path = tmp_path / "log.jsonl"
    args_list = []
    for i in range(25):
        pr = i + 1
        args_list.append((log_path, {"domains": {"shared": {"paths": ["shared.py"]}}},
                          "shared", pr, [f"symbol_{i}"]))
    with multiprocessing.Pool(processes=8) as pool:
        results = pool.map(_worker_reserve_symbols, args_list)
    ok_results = [r for r in results if r[0] == "ok"]
    assert len(ok_results) == 25
    log = LockLog(log_path)
    active = log.active_all()
    assert len(active) == 25
    all_syms: set[str] = set()
    for entry in active:
        assert len(entry.symbols) == 1
        all_syms.update(entry.symbols)
    assert len(all_syms) == 25
    result = check(log, reg, files=["shared.py"], pr=999,
                   touched_symbols_by_path={"shared.py": {"symbol_0"}})
    assert not result.ok
    result_free = check(log, reg, files=["shared.py"], pr=999,
                        touched_symbols_by_path={"shared.py": {"unclaimed_sym"}})
    assert result_free.ok


def test_whole_domain_exclusion_at_scale(tmp_path: Path):
    reg_data = {"domains": {"shared": {"paths": ["shared.py"]}}}
    log_path = tmp_path / "log.jsonl"
    reg_path = tmp_path / "reg.yaml"
    reg_path.write_text(yaml.safe_dump(reg_data))
    r = _run_cli(tmp_path, "reserve", "--domain", "shared", "--pr", "1",
                 "--agent", "holder", "--branch", "b")
    assert r.returncode == 0
    procs = []
    for i in range(30):
        cmd = [
            sys.executable, "-m", "merge_train.domain_lock",
            "--registry", str(reg_path), "--log", str(log_path),
            "reserve", "--domain", "shared",
            "--pr", str(i + 100), "--agent", f"a{i}", "--branch", f"b{i}",
        ]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append(p)
    codes = []
    for p in procs:
        p.wait()
        codes.append(p.returncode)
    assert all(rc == 1 for rc in codes)
    log = LockLog(log_path)
    active = log.active_all()
    assert len(active) == 1
    assert active[0].pr == 1


def test_check_under_concurrent_reserve_release(tmp_path: Path):
    reg_data = {"domains": {"shared": {"paths": ["shared.py"]}}}
    log_path = tmp_path / "log.jsonl"
    log = LockLog(log_path)
    reg = _reg(reg_data)
    reserve(log, reg, domain="shared", pr=1, agent="a1", branch="b1",
            symbols=["alpha"], now="2026-05-20T00:00:00Z")
    reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2",
            symbols=["beta"], now="2026-05-20T00:00:00Z")
    reserve_args = [
        (log_path, reg_data, "shared", i + 10, [f"sym_{i}"])
        for i in range(10)
    ]
    release_args = [
        (log_path, 1),
        (log_path, 2),
    ]
    check_args = [
        (log_path, reg_data, ["shared.py"], 999),
    ] * 20
    with multiprocessing.Pool(processes=8) as pool:
        reserve_results = pool.map(_worker_reserve_symbols, reserve_args)
        release_results = pool.map(_worker_release, release_args)
        check_results = pool.map(_worker_check, check_args)
    for pr, ok, free_count, held_count in check_results:
        assert isinstance(ok, bool)
        assert isinstance(free_count, int)
        assert isinstance(held_count, int)
    raw_lines = log_path.read_text().strip().splitlines()
    for line in raw_lines:
        data = json.loads(line)
        assert "domain" in data
        assert "status" in data
