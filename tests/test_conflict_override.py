"""Tests for manual conflict override — 'CONFLICT APPROVED' mode."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from merge_train.domain_lock import (
    DomainHeldError,
    LockLog,
    Registry,
    check,
    release,
    reserve,
)

OVERRIDE = "CONFLICT APPROVED"
WRONG_OVERRIDE = "conflict approved"  # case-sensitive


def _reg(advisory: bool = False) -> Registry:
    return Registry.from_dict({
        "domains": {
            "core": {"paths": ["core.py"], "advisory": advisory},
        }
    })


@pytest.fixture
def log(tmp_path: Path) -> LockLog:
    return LockLog(tmp_path / "log.jsonl")


# ── reserve override ──────────────────────────────────────────────────────────

def test_reserve_override_bypasses_held_domain(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    # PR#2 would normally get DomainHeldError
    entry = reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2",
                    override=OVERRIDE)
    assert entry.pr == 2


def test_reserve_override_wrong_phrase_still_raises(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2",
                override=WRONG_OVERRIDE)


def test_reserve_no_override_still_raises_on_held(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2")


def test_reserve_override_symbol_conflict_bypassed(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b",
            symbols=["fn_x"])
    entry = reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2",
                    symbols=["fn_x"], override=OVERRIDE)
    assert entry.pr == 2


def test_reserve_override_records_in_log(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2",
            override=OVERRIDE)
    entries = log.entries()
    override_entries = [e for e in entries if e.override]
    assert len(override_entries) == 1
    assert override_entries[0].pr == 2


# ── check override ────────────────────────────────────────────────────────────

def test_check_override_treats_held_as_free(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    result = check(log, reg, files=["core.py"], pr=2, override=OVERRIDE)
    assert result.ok
    assert "core" in result.free


def test_check_override_wrong_phrase_still_held(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    result = check(log, reg, files=["core.py"], pr=2, override=WRONG_OVERRIDE)
    assert not result.ok
    assert len(result.held) == 1


def test_check_no_override_still_held(log):
    reg = _reg()
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    result = check(log, reg, files=["core.py"], pr=2)
    assert not result.ok


# ── CLI integration ───────────────────────────────────────────────────────────

def test_cli_reserve_override(tmp_path):
    reg_file = tmp_path / "reg.yaml"
    reg_file.write_text("domains:\n  core:\n    paths:\n      - core.py\n")
    log_file = tmp_path / "log.jsonl"

    base = ["python3", "-m", "merge_train.domain_lock",
            "--registry", str(reg_file), "--log", str(log_file)]

    # PR#1 reserves
    subprocess.run(base + ["reserve", "--domain", "core", "--pr", "1",
                            "--agent", "a", "--branch", "b"], check=True)

    # PR#2 without override — should fail
    r = subprocess.run(base + ["reserve", "--domain", "core", "--pr", "2",
                                "--agent", "a2", "--branch", "b2"],
                       capture_output=True)
    assert r.returncode == 1

    # PR#2 with override — should succeed
    r = subprocess.run(base + ["reserve", "--domain", "core", "--pr", "2",
                                "--agent", "a2", "--branch", "b2",
                                "--override", OVERRIDE],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "OVERRIDE" in r.stdout


def test_cli_check_override(tmp_path):
    reg_file = tmp_path / "reg.yaml"
    reg_file.write_text("domains:\n  core:\n    paths:\n      - core.py\n")
    log_file = tmp_path / "log.jsonl"

    base = ["python3", "-m", "merge_train.domain_lock",
            "--registry", str(reg_file), "--log", str(log_file)]

    subprocess.run(base + ["reserve", "--domain", "core", "--pr", "1",
                            "--agent", "a", "--branch", "b"], check=True)

    # check without override — exit 1
    r = subprocess.run(base + ["check", "--files", "core.py", "--pr", "2"],
                       capture_output=True)
    assert r.returncode == 1

    # check with override — exit 0
    r = subprocess.run(base + ["check", "--files", "core.py", "--pr", "2",
                                "--override", OVERRIDE],
                       capture_output=True, text=True)
    assert r.returncode == 0
