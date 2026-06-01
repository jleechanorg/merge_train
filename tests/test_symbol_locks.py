"""Symbol-level reservation tests for merge_train.domain_lock."""

from __future__ import annotations

import json
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
    check,
    release,
    reserve,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


@pytest.fixture
def shared_domain(tmp_path: Path) -> tuple[LockLog, Registry]:
    reg = _reg({
        "domains": {
            "shared": {"paths": ["shared.py"]},
            "other": {"paths": ["other.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg


def test_lock_entry_round_trip_with_symbols():
    e = LockEntry(
        domain="d", pr=1, agent="a", branch="b",
        opened_at="t", status="active",
        symbols=("foo", "bar"),
    )
    s = e.to_json()
    assert "symbols" in s
    e2 = LockEntry.from_json(s)
    assert e == e2
    assert not e2.is_whole_domain


def test_lock_entry_round_trip_without_symbols_is_clean():
    e = LockEntry(
        domain="d", pr=1, agent="a", branch="b",
        opened_at="t", status="active",
    )
    s = e.to_json()
    assert "symbols" not in s
    e2 = LockEntry.from_json(s)
    assert e == e2
    assert e2.is_whole_domain


def test_lock_entry_legacy_log_line_without_symbols():
    legacy = '{"domain":"d","pr":1,"agent":"a","branch":"b","opened_at":"t","status":"active","closed_at":null,"note":null}'
    e = LockEntry.from_json(legacy)
    assert e.symbols == ()


def test_lock_entry_log_line_with_null_symbols():
    line = '{"domain":"d","pr":1,"agent":"a","branch":"b","opened_at":"t","status":"active","symbols":null}'
    e = LockEntry.from_json(line)
    assert e.symbols == ()


def test_reserve_two_disjoint_symbol_locks_coexist(shared_domain):
    log, reg = shared_domain
    e1 = reserve(log, reg, domain="shared", pr=1, agent="a", branch="b1",
                 symbols=["foo"])
    e2 = reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2",
                 symbols=["bar"])
    assert e1.symbols == ("foo",)
    assert e2.symbols == ("bar",)
    assert len(log.active_all()) == 2


def test_reserve_overlapping_symbols_rejected(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo", "bar"])
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2",
                symbols=["bar", "baz"])
    assert "bar" in str(exc.value)
    assert "PR #1" in str(exc.value)


def test_reserve_whole_domain_blocks_subsequent_symbol_lock(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b")
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2",
                symbols=["foo"])
    assert "fully held" in str(exc.value)


def test_reserve_symbol_lock_blocks_subsequent_whole_domain(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2")
    msg = str(exc.value).lower()
    assert "whole-domain" in msg or "refused" in msg


def test_reserve_symbols_deduped_and_sorted(shared_domain):
    log, reg = shared_domain
    e = reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
                symbols=["foo", "bar", "foo"])
    assert e.symbols == ("bar", "foo")


def test_release_clears_symbol_lock_only_for_that_pr(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b1",
            symbols=["foo"])
    reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2",
            symbols=["bar"])
    released = release(log, pr=1)
    assert len(released) == 1
    assert released[0].symbols == ("foo",)
    active = log.active_all()
    assert len(active) == 1
    assert active[0].pr == 2


def test_release_then_whole_domain_succeeds(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    release(log, pr=1)
    e = reserve(log, reg, domain="shared", pr=2, agent="a", branch="b")
    assert e.is_whole_domain


def test_check_symbol_level_disjoint_is_free(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    result = check(
        log, reg,
        files=["shared.py"], pr=2,
        touched_symbols_by_path={"shared.py": {"bar"}},
    )
    assert result.ok


def test_check_symbol_level_overlap_is_held(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    result = check(
        log, reg,
        files=["shared.py"], pr=2,
        touched_symbols_by_path={"shared.py": {"foo", "bar"}},
    )
    assert not result.ok
    assert result.held[0][1].pr == 1


def test_check_whole_domain_held_blocks_any_symbol_edit(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b")
    result = check(
        log, reg,
        files=["shared.py"], pr=2,
        touched_symbols_by_path={"shared.py": {"foo"}},
    )
    assert not result.ok
    assert result.held[0][1].is_whole_domain


def test_check_file_level_caller_against_symbol_holder_blocks(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    result = check(log, reg, files=["shared.py"], pr=2)
    assert not result.ok


def test_check_own_pr_carve_out_with_symbols(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=10, agent="a", branch="b",
            symbols=["foo"])
    result = check(
        log, reg,
        files=["shared.py"], pr=10,
        touched_symbols_by_path={"shared.py": {"foo", "bar"}},
    )
    assert result.ok


def test_check_two_symbol_holders_caller_overlaps_one(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    reserve(log, reg, domain="shared", pr=2, agent="a", branch="b",
            symbols=["bar"])
    result = check(
        log, reg,
        files=["shared.py"], pr=3,
        touched_symbols_by_path={"shared.py": {"bar"}},
    )
    assert not result.ok
    assert result.held[0][1].pr == 2


def test_check_two_symbol_holders_caller_disjoint_is_free(shared_domain):
    log, reg = shared_domain
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["foo"])
    reserve(log, reg, domain="shared", pr=2, agent="a", branch="b",
            symbols=["bar"])
    result = check(
        log, reg,
        files=["shared.py"], pr=3,
        touched_symbols_by_path={"shared.py": {"baz"}},
    )
    assert result.ok


def test_check_touched_symbols_aggregated_per_domain(tmp_path: Path):
    reg = _reg({"domains": {"d1": {"paths": ["a.py", "b.py"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b",
            symbols=["foo"])
    result = check(
        log, reg,
        files=["a.py", "b.py"], pr=2,
        touched_symbols_by_path={"a.py": {"bar"}, "b.py": {"foo"}},
    )
    assert not result.ok


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


def test_cli_reserve_with_symbols(tmp_path: Path):
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "a", "--branch", "b",
        "--symbols", "foo,bar",
    )
    assert r.returncode == 0, r.stderr
    assert "symbols=bar,foo" in r.stdout


def test_cli_two_disjoint_reserves(tmp_path: Path):
    r1 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "a", "--branch", "b",
        "--symbols", "foo",
    )
    assert r1.returncode == 0
    r2 = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "2",
        "--agent", "a", "--branch", "b",
        "--symbols", "bar",
    )
    assert r2.returncode == 0, r2.stderr


def test_cli_overlapping_symbols_denied(tmp_path: Path):
    _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "1",
        "--agent", "a", "--branch", "b",
        "--symbols", "foo",
    )
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "shared", "--pr", "2",
        "--agent", "a", "--branch", "b",
        "--symbols", "foo",
    )
    assert r.returncode == 1
    assert "DENIED" in r.stderr


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _make_diff_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    src = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    (repo / "shared.py").write_text(src)
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-q", "-m", "init")
    reg = repo / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {"shared": {"paths": ["shared.py"]}}
    }))
    log = tmp_path / "log.jsonl"
    return repo, reg, log


def test_cli_check_diff_mode_disjoint_is_free(tmp_path: Path):
    repo, reg, log = _make_diff_repo(tmp_path)
    src = (repo / "shared.py").read_text()
    (repo / "shared.py").write_text(
        src.replace("    return 1\n", "    return 11\n")
    )
    _git(repo, "add", "shared.py")
    subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "shared", "--pr", "1",
        "--agent", "a", "--branch", "b",
        "--symbols", "beta",
    ], check=True, capture_output=True)
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "--git-cwd", str(repo),
        "check", "--files", "shared.py", "--pr", "2",
        "--diff-mode",
    ], capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"
    assert "FREE" in r.stdout


def test_cli_check_diff_mode_overlap_is_held(tmp_path: Path):
    repo, reg, log = _make_diff_repo(tmp_path)
    src = (repo / "shared.py").read_text()
    (repo / "shared.py").write_text(
        src.replace("    return 2\n", "    return 22\n")
    )
    _git(repo, "add", "shared.py")
    subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "shared", "--pr", "1",
        "--agent", "a", "--branch", "b",
        "--symbols", "shared.py:beta",
    ], check=True, capture_output=True)
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "--git-cwd", str(repo),
        "check", "--files", "shared.py", "--pr", "2",
        "--diff-mode", "--json",
    ], capture_output=True, text=True)
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert payload["held"][0]["domain"] == "shared"
    assert "shared.py:beta" in payload["touched_symbols"]["shared"]


def test_cli_check_diff_mode_json_includes_fallback(tmp_path: Path):
    repo, reg, log = _make_diff_repo(tmp_path)
    (repo / "config.yaml").write_text("key: value\n")
    _git(repo, "add", "config.yaml")
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "--git-cwd", str(repo),
        "check", "--files", "shared.py", "config.yaml",
        "--diff-mode", "--json",
    ], capture_output=True, text=True)
    payload = json.loads(r.stdout)
    assert "fallback_files" in payload
    assert "config.yaml" in payload["fallback_files"]


def test_reserve_many_symbols_refuses_with_summarized_symbols(shared_domain):
    log, reg = shared_domain
    # Reserve a domain with 5 symbols
    reserve(log, reg, domain="shared", pr=1, agent="a", branch="b",
            symbols=["s1", "s2", "s3", "s4", "s5"])
    
    # Try to reserve the whole domain — should fail and raise DomainHeldError
    # showing "5 symbols" instead of listing them all
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2")
    assert "5 symbols" in str(exc.value)
    
    # Try to reserve overlapping symbols (s1, s2, s3, s4, s6) — should fail
    # showing "4 symbols" in the error because 4 of them overlap
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="shared", pr=2, agent="a2", branch="b2",
                symbols=["s1", "s2", "s3", "s4", "s6"])
    assert "4 symbols" in str(exc.value)
