"""Tests for the acquire command and file-level fallback locking."""

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
    check,
    release,
    reserve_plan,
)

def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)

@pytest.fixture
def sample_registry_and_log(tmp_path: Path) -> tuple[LockLog, Registry, Path]:
    reg_yaml = tmp_path / "file_domains.yaml"
    reg_yaml.write_text(yaml.safe_dump({
        "domains": {
            "rewards-engine": {"paths": ["mvp_site/rewards_engine.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
        }
    }))
    reg = load_registry_helper(reg_yaml)
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg, reg_yaml

def load_registry_helper(path: Path) -> Registry:
    from merge_train.domain_lock import load_registry
    return load_registry(path)

# --------------------------------------------------------------------------- #
# Programmatic resolution and reservation tests
# --------------------------------------------------------------------------- #

def test_acquire_resolves_and_locks_mixed(sample_registry_and_log):
    log, reg, _ = sample_registry_and_log
    
    # We will manually resolve paths to domains:
    # mvp_site/rewards_engine.py -> rewards-engine
    # README.md -> file:README.md
    files = ["mvp_site/rewards_engine.py", "README.md"]
    grouped = reg.domains_for_paths(files)
    unmapped = grouped.pop("__unmapped__", [])
    
    plan_items = []
    for d, paths in grouped.items():
        plan_items.append({"domain": d})
    for p in unmapped:
        plan_items.append({"domain": f"file:{p.lstrip('./')}"})
        
    assert len(plan_items) == 2
    assert {item["domain"] for item in plan_items} == {"rewards-engine", "file:README.md"}
    
    # Let's reserve them atomically
    entries = reserve_plan(
        log, reg,
        pr=7000, agent="codex-1", branch="feat/foo", plan=plan_items
    )
    
    assert len(entries) == 2
    active = log.active_all()
    assert len(active) == 2
    assert {e.domain for e in active} == {"rewards-engine", "file:README.md"}

def test_acquire_checks_virtual_domains_successfully(sample_registry_and_log):
    log, reg, _ = sample_registry_and_log
    # Acquire file:README.md
    reserve_plan(
        log, reg, pr=7000, agent="codex-1", branch="feat/foo",
        plan=[{"domain": "file:README.md"}]
    )
    
    # Check README.md
    res = check(log, reg, files=["README.md"])
    assert not res.ok
    assert len(res.held) == 1
    assert res.held[0][0] == "file:README.md"
    assert res.held[0][1].pr == 7000
    
    # Check with same PR - should not conflict
    res_same = check(log, reg, files=["README.md"], pr=7000)
    assert res_same.ok

# --------------------------------------------------------------------------- #
# CLI execution tests
# --------------------------------------------------------------------------- #

def test_cli_acquire_happy_path(sample_registry_and_log):
    log, reg, reg_yaml = sample_registry_and_log
    
    # Execute domain_lock acquire
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg_yaml),
        "--log", str(log.path),
        "acquire",
        "--files", "mvp_site/rewards_engine.py", "README.md",
        "--pr", "7000",
        "--agent", "codex-1",
        "--branch", "feat/foo"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert "RESERVED: " in res.stdout
    
    # Verify they are in the lock log
    active = log.active_all()
    assert len(active) == 2
    assert {e.domain for e in active} == {"rewards-engine", "file:README.md"}

def test_cli_acquire_dry_run(sample_registry_and_log):
    log, reg, reg_yaml = sample_registry_and_log
    
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg_yaml),
        "--log", str(log.path),
        "acquire",
        "--files", "README.md",
        "--pr", "7000",
        "--agent", "codex-1",
        "--branch", "feat/foo",
        "--dry-run"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert "WOULD-RESERVE: file:README.md" in res.stdout
    assert len(log.active_all()) == 0

def test_cli_acquire_denied_due_to_held(sample_registry_and_log):
    log, reg, reg_yaml = sample_registry_and_log
    
    # Lock file:README.md first
    reserve_plan(
        log, reg, pr=7000, agent="codex-1", branch="feat/foo",
        plan=[{"domain": "file:README.md"}]
    )
    
    # Try to acquire it with another PR
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg_yaml),
        "--log", str(log.path),
        "acquire",
        "--files", "README.md",
        "--pr", "7001",
        "--agent", "codex-2",
        "--branch", "feat/bar"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    assert "DENIED: " in res.stderr
    
    # Assert rollback: no additional locks were written for 7001
    active = log.active_all()
    assert len(active) == 1
    assert active[0].pr == 7000


def test_cli_check_virtual_domain_text_format(sample_registry_and_log):
    log, reg, reg_yaml = sample_registry_and_log
    
    # Lock file:README.md first
    reserve_plan(
        log, reg, pr=7000, agent="codex-1", branch="feat/foo",
        plan=[{"domain": "file:README.md"}]
    )
    
    # Check it via CLI in text mode
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg_yaml),
        "--log", str(log.path),
        "check",
        "--files", "README.md"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    assert "HELD (blocking):" in res.stdout
    assert "Domain: file:README.md" in res.stdout
    assert "Held by: PR#7000" in res.stdout

