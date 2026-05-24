"""Tests for merge_train.domain_lock core lib + CLI behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

from merge_train.domain_lock import (
    DEFAULT_LOG,
    DomainHeldError,
    LockEntry,
    LockLog,
    Registry,
    UnknownPathError,
    _resolve_default_log,
    audit,
    check,
    list_locks,
    load_registry,
    release,
    reserve,
)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


def test_registry_from_dict_loads_domains():
    reg = _reg({
        "domains": {
            "alpha": {"paths": ["a/**", "a.py"], "owners": ["@a"]},
            "beta": {"paths": ["b/*.py"]},
        }
    })
    assert set(reg.domains) == {"alpha", "beta"}
    assert reg.domains["alpha"].owners == ("@a",)
    assert reg.domains["beta"].owners == ()


def test_registry_from_dict_empty():
    reg = _reg({})
    assert reg.domains == {}
    reg2 = _reg({"domains": None})
    assert reg2.domains == {}


def test_domain_for_path_glob_match():
    reg = _reg({
        "domains": {
            "rewards": {"paths": ["mvp_site/rewards_engine.py"]},
            "tests": {"paths": ["mvp_site/tests/test_*.py"]},
        }
    })
    assert reg.domain_for_path("mvp_site/rewards_engine.py") == "rewards"
    assert reg.domain_for_path("mvp_site/tests/test_foo.py") == "tests"
    assert reg.domain_for_path("mvp_site/other.py") is None


def test_domain_for_path_strips_leading_dotslash():
    reg = _reg({"domains": {"x": {"paths": ["./foo.py"]}}})
    assert reg.domain_for_path("foo.py") == "x"
    assert reg.domain_for_path("./foo.py") == "x"


def test_domain_for_path_first_match_wins():
    reg = _reg({
        "domains": {
            "first": {"paths": ["foo.py"]},
            "second": {"paths": ["foo.py"]},
        }
    })
    assert reg.domain_for_path("foo.py") == "first"


def test_domains_for_paths_groups_and_collects_unmapped():
    reg = _reg({
        "domains": {
            "a": {"paths": ["a/*.py"]},
            "b": {"paths": ["b/*.py"]},
        }
    })
    grouped = reg.domains_for_paths(["a/1.py", "a/2.py", "b/3.py", "c/4.py"])
    assert sorted(grouped["a"]) == ["a/1.py", "a/2.py"]
    assert grouped["b"] == ["b/3.py"]
    assert grouped["__unmapped__"] == ["c/4.py"]


# --------------------------------------------------------------------------- #
# load_registry (file IO)
# --------------------------------------------------------------------------- #


def test_load_registry_reads_yaml(tmp_path: Path):
    p = tmp_path / "reg.yaml"
    p.write_text(yaml.safe_dump({"domains": {"x": {"paths": ["x.py"]}}}))
    reg = load_registry(p)
    assert "x" in reg.domains


def test_load_registry_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_registry(tmp_path / "missing.yaml")


# --------------------------------------------------------------------------- #
# LockLog + LockEntry round-trip
# --------------------------------------------------------------------------- #


def test_lock_entry_json_round_trip():
    e = LockEntry(
        domain="d", pr=1, agent="a", branch="b",
        opened_at="2026-05-16T00:00:00Z", status="active",
    )
    s = e.to_json()
    e2 = LockEntry.from_json(s)
    assert e == e2


def test_lock_log_append_and_entries(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    e1 = LockEntry("d1", 1, "a", "b", "2026-05-16T00:00:00Z", "active")
    e2 = LockEntry("d2", 2, "a", "b", "2026-05-16T00:00:01Z", "active")
    log.append(e1)
    log.append(e2)
    entries = log.entries()
    assert entries == [e1, e2]


def test_lock_log_entries_missing_returns_empty(tmp_path: Path):
    log = LockLog(tmp_path / "nope.jsonl")
    assert log.entries() == []
    assert log.active() == {}


def test_lock_log_active_reflects_release(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    log.append(LockEntry("d", 1, "a", "b", "t1", "active"))
    assert "d" in log.active()
    log.append(LockEntry("d", 1, "a", "b", "t1", "released", closed_at="t2"))
    assert log.active() == {}


def test_lock_log_active_latest_per_domain(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    log.append(LockEntry("d", 1, "a", "b", "t1", "active"))
    log.append(LockEntry("d", 1, "a", "b", "t1", "released", closed_at="t2"))
    log.append(LockEntry("d", 2, "a2", "b2", "t3", "active"))
    active = log.active()
    assert set(active) == {"d"}
    assert active["d"].pr == 2


# --------------------------------------------------------------------------- #
# reserve / release
# --------------------------------------------------------------------------- #


def _two_domains(tmp_path: Path) -> tuple[LockLog, Registry]:
    reg = _reg({"domains": {
        "d1": {"paths": ["a.py"]},
        "d2": {"paths": ["b.py"]},
    }})
    log = LockLog(tmp_path / "log.jsonl")
    return log, reg


def test_reserve_writes_active_entry(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    entry = reserve(log, reg, domain="d1", pr=10, agent="alice", branch="feat/x")
    assert entry.status == "active"
    assert entry.pr == 10
    assert log.active()["d1"].pr == 10


def test_reserve_unknown_domain_raises(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    with pytest.raises(UnknownPathError):
        reserve(log, reg, domain="nope", pr=1, agent="a", branch="b")


def test_reserve_double_reserve_raises_domain_held(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    with pytest.raises(DomainHeldError) as exc:
        reserve(log, reg, domain="d1", pr=2, agent="a2", branch="b2")
    assert "PR #1" in str(exc.value)


def test_reserve_after_release_succeeds(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    released = release(log, pr=1)
    assert len(released) == 1
    reserve(log, reg, domain="d1", pr=2, agent="a2", branch="b2")
    assert log.active()["d1"].pr == 2


def test_release_filter_by_domain(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    reserve(log, reg, domain="d2", pr=1, agent="a", branch="b")
    released = release(log, pr=1, domain="d1")
    assert len(released) == 1
    assert released[0].domain == "d1"
    assert "d2" in log.active()
    assert "d1" not in log.active()


def test_release_unknown_pr_returns_empty(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    assert release(log, pr=999) == []


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #


def test_check_all_free(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    result = check(log, reg, files=["a.py", "b.py"])
    assert result.ok
    assert sorted(result.free) == ["d1", "d2"]
    assert result.held == []
    assert result.unmapped == []


def test_check_held_by_other_pr(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b")
    result = check(log, reg, files=["a.py", "b.py"], pr=20)
    assert not result.ok
    assert len(result.held) == 1
    assert result.held[0][0] == "d1"
    assert result.held[0][1].pr == 10
    assert result.free == ["d2"]


def test_check_own_pr_does_not_self_conflict(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b")
    result = check(log, reg, files=["a.py"], pr=10)
    assert result.ok
    assert result.free == ["d1"]


def test_check_unmapped_files_warned_not_held(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    result = check(log, reg, files=["unmapped.py", "a.py"])
    assert result.ok
    assert result.unmapped == ["unmapped.py"]
    assert result.free == ["d1"]


def test_check_symbol_level_no_overlap_is_free(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b",
            symbols={"alpha"})
    result = check(log, reg, files=["a.py"], pr=20,
                   touched_symbols_by_path={"a.py": {"beta"}})
    assert result.ok
    assert "d1" in result.free


def test_check_symbol_level_overlap_is_held(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b",
            symbols={"alpha"})
    result = check(log, reg, files=["a.py"], pr=20,
                   touched_symbols_by_path={"a.py": {"alpha"}})
    assert not result.ok
    assert result.held[0][0] == "d1"


def test_check_missing_symbol_entry_is_whole_domain(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b",
            symbols={"alpha"})
    result = check(log, reg, files=["a.py"], pr=20,
                   touched_symbols_by_path={})
    assert not result.ok
    assert result.held[0][0] == "d1"


def test_check_explicit_empty_set_is_genuinely_free(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b",
            symbols={"alpha"})
    result = check(log, reg, files=["a.py"], pr=20,
                   touched_symbols_by_path={"a.py": set()})
    assert result.ok
    assert "d1" in result.free


def test_check_none_value_is_whole_domain_fallback(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=10, agent="a", branch="b",
            symbols={"alpha"})
    result = check(log, reg, files=["a.py"], pr=20,
                   touched_symbols_by_path={"a.py": None})
    assert not result.ok
    assert result.held[0][0] == "d1"


# --------------------------------------------------------------------------- #
# list_locks / audit
# --------------------------------------------------------------------------- #


def test_list_locks_active_only(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    reserve(log, reg, domain="d2", pr=2, agent="a", branch="b")
    release(log, pr=1)
    active = list_locks(log, status="active")
    assert len(active) == 1
    assert active[0].domain == "d2"


def test_list_locks_all_includes_released(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    release(log, pr=1)
    all_entries = list_locks(log, status="all")
    assert len(all_entries) == 2
    statuses = [e.status for e in all_entries]
    assert statuses == ["active", "released"]


def test_audit_shape(tmp_path: Path):
    log, reg = _two_domains(tmp_path)
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    rep = audit(log, reg)
    assert "registry" in rep and "active_locks" in rep
    assert "d1" in rep["registry"]["domains"]
    assert "d1" in rep["active_locks"]
    assert rep["total_entries"] == 1
    assert "generated_at" in rep


# --------------------------------------------------------------------------- #
# CLI smoke (subprocess)
# --------------------------------------------------------------------------- #


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    reg = tmp_path / "reg.yaml"
    log = tmp_path / "log.jsonl"
    if not reg.exists():
        reg.write_text(yaml.safe_dump({
            "domains": {"d1": {"paths": ["a.py"]}, "d2": {"paths": ["b.py"]}}
        }))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log), *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def test_cli_check_free(tmp_path: Path):
    r = _run_cli(tmp_path, "check", "--files", "a.py")
    assert r.returncode == 0
    assert "FREE" in r.stdout


def test_cli_reserve_then_check_held_exit1(tmp_path: Path):
    r1 = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "alice", "--branch", "feat/x",
    )
    assert r1.returncode == 0
    assert "RESERVED" in r1.stdout
    r2 = _run_cli(tmp_path, "check", "--files", "a.py", "--pr", "2")
    assert r2.returncode == 1
    assert "HELD" in r2.stdout


def test_cli_check_own_pr_free(tmp_path: Path):
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "alice", "--branch", "b",
    )
    r = _run_cli(tmp_path, "check", "--files", "a.py", "--pr", "1")
    assert r.returncode == 0
    assert "FREE" in r.stdout


def test_cli_check_json_output(tmp_path: Path):
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "alice", "--branch", "b",
    )
    r = _run_cli(tmp_path, "check", "--files", "a.py", "--pr", "2", "--json")
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert payload["held"][0]["domain"] == "d1"


def test_cli_check_diff_mode_syntax_error_is_held(tmp_path: Path):
    import tempfile
    with tempfile.TemporaryDirectory() as repo_dir:
        repo = Path(repo_dir)
        _git(repo, "init", "-q", "--initial-branch=main")
        _git(repo, "config", "user.email", "t@t.test")
        _git(repo, "config", "user.name", "t")
        (repo / "a.py").write_text("def alpha():\n    return 1\n")
        _git(repo, "add", "a.py")
        _git(repo, "commit", "-q", "-m", "init")
        (repo / "a.py").write_text("def broken(:\n    pass\n")
        _git(repo, "add", "a.py")

        reg = tmp_path / "reg.yaml"
        log = tmp_path / "log.jsonl"
        reg.write_text(yaml.safe_dump({
            "domains": {"d1": {"paths": ["a.py"]}}
        }))
        _run_cli(tmp_path, "reserve",
                 "--domain", "d1", "--pr", "1",
                 "--agent", "alice", "--branch", "b")
        cmd = [
            sys.executable, "-m", "merge_train.domain_lock",
            "--registry", str(reg), "--log", str(log),
            "--git-cwd", str(repo),
            "check", "--files", "a.py", "--pr", "2",
            "--diff-mode",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        assert r.returncode == 1
        assert "HELD" in r.stdout


def test_cli_git_cwd_affects_reserve_log_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    subprocess.run(["git", "remote", "add", "origin", "https://example.com/myrepo.git"],
                   cwd=repo, check=True, capture_output=True)
    reg = repo / "reg.yaml"
    log = tmp_path / "isolated_log.jsonl"
    reg.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "--git-cwd", str(repo),
        "reserve", "--domain", "d1", "--pr", "1",
        "--agent", "a", "--branch", "b",
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "RESERVED" in r.stdout


def test_cli_double_reserve_exit1(tmp_path: Path):
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    )
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "2", "--agent", "a", "--branch", "b",
    )
    assert r.returncode == 1
    assert "DENIED" in r.stderr


def test_cli_release_then_reserve(tmp_path: Path):
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    )
    r_rel = _run_cli(tmp_path, "release", "--pr", "1")
    assert r_rel.returncode == 0
    r_re = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "2", "--agent", "a", "--branch", "b",
    )
    assert r_re.returncode == 0


def test_cli_audit_json(tmp_path: Path):
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    )
    r = _run_cli(tmp_path, "audit")
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert "registry" in payload
    assert "d1" in payload["active_locks"]


def test_entries_skips_comment_lines(tmp_path: Path):
    log_path = tmp_path / "log.jsonl"
    entry = LockEntry(domain="d1", pr=1, agent="a", branch="b", opened_at="2026-01-01T00:00:00Z", status="reserved")
    log_path.write_text(
        "# this is a comment\n" + entry.to_json() + "\n# another comment\n",
        encoding="utf-8",
    )
    log = LockLog(log_path)
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0].domain == "d1"


def test_cli_missing_registry_exit2(tmp_path: Path):
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(tmp_path / "missing.yaml"),
        "--log", str(tmp_path / "log.jsonl"),
        "check", "--files", "a.py",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 2


# --------------------------------------------------------------------------- #
# External lock path (orch-i7uv)
# --------------------------------------------------------------------------- #


def test_default_log_path_under_merge_train_locks():
    path = Path(_resolve_default_log())
    assert path.parent.parent == Path.home() / ".merge_train" / "locks"
    assert path.name == "pr_domain_locks.jsonl"


def test_resolve_default_log_uses_remote_hash():
    with mock.patch("subprocess.run", return_value=mock.Mock(
        stdout="https://github.com/example/repo.git\n",
    )):
        path = Path(_resolve_default_log())
    assert path.parent.parent == Path.home() / ".merge_train" / "locks"
    import hashlib
    expected = hashlib.sha256(b"https://github.com/example/repo.git").hexdigest()[:12]
    assert path.parent.name == expected


def test_resolve_default_log_fallback_when_no_git():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        path = Path(_resolve_default_log())
    assert path.parent.name == "default"
    assert path.parent.parent == Path.home() / ".merge_train" / "locks"


def test_resolve_default_log_fallback_when_no_origin():
    with mock.patch("subprocess.run", return_value=mock.Mock(stdout="")):
        path = Path(_resolve_default_log())
    assert path.parent.name == "default"


def test_default_log_is_sentinel():
    assert DEFAULT_LOG == "<auto>"


def test_resolve_default_log_uses_cwd(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", "https://example.com/test.git")
    result = _resolve_default_log(cwd=repo)
    assert "test.git" not in result
    assert ".merge_train/locks" in result


def test_resolve_default_log_fallback_no_git(tmp_path: Path):
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        result = _resolve_default_log(cwd=tmp_path)
    assert "default" in result
    assert ".merge_train/locks" in result


def test_env_var_overrides_default(tmp_path: Path, monkeypatch):
    custom_log = tmp_path / "custom.jsonl"
    monkeypatch.setenv("MERGE_TRAIN_LOG", str(custom_log))
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg),
        "reserve", "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0
    assert custom_log.exists()


def test_log_flag_overrides_default(tmp_path: Path):
    custom_log = tmp_path / "flag_override.jsonl"
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg),
        "--log", str(custom_log),
        "reserve", "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0
    assert custom_log.exists()


def test_concurrent_append_same_external_log(tmp_path: Path):
    log_path = tmp_path / "shared.jsonl"
    log = LockLog(log_path)
    e1 = LockEntry("d1", 1, "a", "b", "t1", "active")
    e2 = LockEntry("d2", 2, "a2", "b2", "t2", "active")
    log.append(e1)
    log.append(e2)
    entries = log.entries()
    assert len(entries) == 2
    assert entries[0].domain == "d1"
    assert entries[1].domain == "d2"


def test_backward_compat_env_var_in_repo_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MERGE_TRAIN_LOG", str(tmp_path / "pr_domain_locks.jsonl"))
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg),
        "reserve", "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0
    assert (tmp_path / "pr_domain_locks.jsonl").exists()


# --------------------------------------------------------------------------- #
# Concurrency safety (orch-xxvz)
# --------------------------------------------------------------------------- #


def test_lock_log_lock_context_manager(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    with log.lock() as fh:
        assert fh is not None


def _worker_reserve(args):
    log_path, reg_data, domain, pr, agent, branch = args
    from merge_train.domain_lock import LockLog, Registry, reserve, DomainHeldError
    log = LockLog(log_path)
    reg = Registry.from_dict(reg_data)
    try:
        reserve(log, reg, domain=domain, pr=pr, agent=agent, branch=branch,
                now="2026-05-17T00:00:00Z")
        return ("ok", pr)
    except DomainHeldError:
        return ("denied", pr)


def test_concurrent_reserve_only_one_wins(tmp_path: Path):
    import multiprocessing

    reg_data = {"domains": {"d1": {"paths": ["a.py"]}}}
    log_path = tmp_path / "log.jsonl"
    args_list = [
        (log_path, reg_data, "d1", 1, "a1", "b1"),
        (log_path, reg_data, "d1", 2, "a2", "b2"),
    ]
    with multiprocessing.Pool(2) as pool:
        results = pool.map(_worker_reserve, args_list)
    statuses = [r[0] for r in results]
    assert "ok" in statuses
    assert "denied" in statuses
    log = LockLog(log_path)
    assert len(log.active_all()) == 1


# --------------------------------------------------------------------------- #
# Backward-compat: global flags accepted after the subcommand
# --------------------------------------------------------------------------- #


def _global_opts_fixture(tmp_path: Path) -> tuple[Path, Path]:
    reg = tmp_path / "reg.yaml"
    log = tmp_path / "log.jsonl"
    reg.write_text(yaml.safe_dump({
        "domains": {"d1": {"paths": ["a.py"]}, "d2": {"paths": ["b.py"]}}
    }))
    return reg, log


def test_cli_check_accepts_git_cwd_after_subcommand(tmp_path: Path):
    """Regression: ``check --files X --git-cwd Y`` (sub-level) used to fail with
    ``unrecognized arguments: --git-cwd``. Both positions must parse."""
    reg, log = _global_opts_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "check", "--files", "a.py",
        "--registry", str(reg), "--log", str(log), "--git-cwd", str(repo),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r} stdout={r.stdout!r}"
    assert "FREE" in r.stdout


def test_cli_check_accepts_git_cwd_before_subcommand(tmp_path: Path):
    reg, log = _global_opts_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log), "--git-cwd", str(repo),
        "check", "--files", "a.py",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "FREE" in r.stdout


def test_cli_reserve_accepts_global_opts_after_subcommand(tmp_path: Path):
    reg, log = _global_opts_fixture(tmp_path)
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
        "--registry", str(reg), "--log", str(log),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "RESERVED" in r.stdout
    assert log.exists()


def test_cli_release_accepts_global_opts_after_subcommand(tmp_path: Path):
    reg, log = _global_opts_fixture(tmp_path)
    subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "d1", "--pr", "7",
        "--agent", "a", "--branch", "b",
    ], check=True, capture_output=True)
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "release", "--pr", "7",
        "--registry", str(reg), "--log", str(log),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"


def test_cli_list_accepts_global_opts_after_subcommand(tmp_path: Path):
    reg, log = _global_opts_fixture(tmp_path)
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "list",
        "--registry", str(reg), "--log", str(log),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"


def test_cli_audit_accepts_global_opts_after_subcommand(tmp_path: Path):
    reg, log = _global_opts_fixture(tmp_path)
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "audit",
        "--registry", str(reg), "--log", str(log),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"


def test_cli_subcommand_global_opts_override_top_level(tmp_path: Path):
    """When the user passes a global flag on BOTH sides, the sub-level value
    wins (last-wins) — but more importantly, it must not raise."""
    reg, log_top = _global_opts_fixture(tmp_path)
    log_sub = tmp_path / "log_sub.jsonl"
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log_top),
        "reserve",
        "--domain", "d1", "--pr", "3", "--agent", "a", "--branch", "b",
        "--log", str(log_sub),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert log_sub.exists(), "sub-level --log should override top-level"
    assert not log_top.exists(), "top-level --log should NOT have been used"


def test_cli_subcommand_registry_override_top_level(tmp_path: Path):
    """Sub-level --registry overrides top-level --registry. Prevents regression
    where _add_global_opts_to_subparser silently dropped --registry."""
    reg_top = tmp_path / "reg_top.yaml"
    reg_sub = tmp_path / "reg_sub.yaml"
    log = tmp_path / "log.jsonl"
    # reg_top has ONLY d1; reg_sub has ONLY d2 — issuing a `reserve --domain d2`
    # must succeed via reg_sub and fail via reg_top.
    reg_top.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    reg_sub.write_text(yaml.safe_dump({"domains": {"d2": {"paths": ["b.py"]}}}))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg_top), "--log", str(log),
        "reserve",
        "--domain", "d2", "--pr", "4", "--agent", "a", "--branch", "b",
        "--registry", str(reg_sub),  # override
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "RESERVED" in r.stdout
    assert "d2" in r.stdout


def test_cli_reserve_plan_accepts_global_opts_after_subcommand(tmp_path: Path):
    """`reserve-plan` (hyphenated subcommand) must also accept globals at sub-level."""
    reg, log = _global_opts_fixture(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "d1", "symbols": []},
            {"domain": "d2", "symbols": []},
        ]
    }))
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "reserve-plan",
        "--pr", "11", "--agent", "a", "--branch", "b",
        "--plan", str(plan),
        "--registry", str(reg), "--log", str(log),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert log.exists()


# ── --dry-run tests ──────────────────────────────────────────────────────


def test_cli_reserve_dry_run_free(tmp_path: Path):
    """--dry-run on a free domain prints WOULD-RESERVE and exits 0."""
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
        "--dry-run",
    )
    assert r.returncode == 0
    assert "WOULD-RESERVE" in r.stdout
    assert "d1" in r.stdout
    # No lock entry written
    log = tmp_path / "log.jsonl"
    assert not log.exists() or log.read_text().strip() == ""


def test_cli_reserve_dry_run_held(tmp_path: Path):
    """--dry-run on a held domain prints HELD and exits 1 without writing."""
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    )
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "2", "--agent", "c", "--branch", "d",
        "--dry-run",
    )
    assert r.returncode == 1
    assert "HELD" in r.stderr


def test_cli_reserve_dry_run_symbol_held(tmp_path: Path):
    """--dry-run with overlapping symbols on held domain exits 1."""
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {"d1": {"paths": ["a.py"]}}
    }))
    log = tmp_path / "log.jsonl"
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "d1", "--pr", "1",
        "--agent", "a", "--branch", "b", "--symbols", "foo,bar",
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    cmd2 = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "d1", "--pr", "2",
        "--agent", "c", "--branch", "d", "--symbols", "bar,baz",
        "--dry-run",
    ]
    r2 = subprocess.run(cmd2, capture_output=True, text=True)
    assert r2.returncode == 1
    assert "HELD" in r2.stderr


def test_cli_reserve_dry_run_unknown_domain(tmp_path: Path):
    """--dry-run on unknown domain exits 2."""
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "nope", "--pr", "1", "--agent", "a", "--branch", "b",
        "--dry-run",
    )
    assert r.returncode == 2


def test_cli_reserve_plan_dry_run_all_free(tmp_path: Path):
    """--dry-run on reserve-plan with all free legs exits 0."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "d1", "symbols": []},
            {"domain": "d2", "symbols": []},
        ]
    }))
    r = _run_cli(
        tmp_path, "reserve-plan",
        "--pr", "1", "--agent", "a", "--branch", "b",
        "--plan", str(plan),
        "--dry-run",
    )
    assert r.returncode == 0
    assert r.stdout.count("WOULD-RESERVE") == 2
    log = tmp_path / "log.jsonl"
    assert not log.exists() or log.read_text().strip() == ""


def test_cli_reserve_plan_dry_run_one_held(tmp_path: Path):
    """--dry-run on reserve-plan where one leg is held exits 1."""
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "99", "--agent", "x", "--branch", "y",
    )
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "d1", "symbols": []},
            {"domain": "d2", "symbols": []},
        ]
    }))
    r = _run_cli(
        tmp_path, "reserve-plan",
        "--pr", "1", "--agent", "a", "--branch", "b",
        "--plan", str(plan),
        "--dry-run",
    )
    assert r.returncode == 1
    assert "HELD" in r.stderr
    assert "WOULD-RESERVE" in r.stdout


# ── --dry-run tests ──────────────────────────────────────────────────────


def test_cli_reserve_dry_run_free(tmp_path: Path):
    """--dry-run on a free domain prints WOULD-RESERVE and exits 0."""
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
        "--dry-run",
    )
    assert r.returncode == 0
    assert "WOULD-RESERVE" in r.stdout
    assert "d1" in r.stdout
    # No lock entry written
    log = tmp_path / "log.jsonl"
    assert not log.exists() or log.read_text().strip() == ""


def test_cli_reserve_dry_run_held(tmp_path: Path):
    """--dry-run on a held domain prints HELD and exits 1 without writing."""
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "1", "--agent", "a", "--branch", "b",
    )
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "2", "--agent", "c", "--branch", "d",
        "--dry-run",
    )
    assert r.returncode == 1
    assert "HELD" in r.stderr


def test_cli_reserve_dry_run_symbol_held(tmp_path: Path):
    """--dry-run with overlapping symbols on held domain exits 1."""
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {"d1": {"paths": ["a.py"]}}
    }))
    log = tmp_path / "log.jsonl"
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "d1", "--pr", "1",
        "--agent", "a", "--branch", "b", "--symbols", "foo,bar",
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    cmd2 = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "reserve", "--domain", "d1", "--pr", "2",
        "--agent", "c", "--branch", "d", "--symbols", "bar,baz",
        "--dry-run",
    ]
    r2 = subprocess.run(cmd2, capture_output=True, text=True)
    assert r2.returncode == 1
    assert "HELD" in r2.stderr


def test_cli_reserve_dry_run_unknown_domain(tmp_path: Path):
    """--dry-run on unknown domain exits 2."""
    r = _run_cli(
        tmp_path, "reserve",
        "--domain", "nope", "--pr", "1", "--agent", "a", "--branch", "b",
        "--dry-run",
    )
    assert r.returncode == 2


def test_cli_reserve_plan_dry_run_all_free(tmp_path: Path):
    """--dry-run on reserve-plan with all free legs exits 0."""
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "d1", "symbols": []},
            {"domain": "d2", "symbols": []},
        ]
    }))
    r = _run_cli(
        tmp_path, "reserve-plan",
        "--pr", "1", "--agent", "a", "--branch", "b",
        "--plan", str(plan),
        "--dry-run",
    )
    assert r.returncode == 0
    assert r.stdout.count("WOULD-RESERVE") == 2
    log = tmp_path / "log.jsonl"
    assert not log.exists() or log.read_text().strip() == ""


def test_cli_reserve_plan_dry_run_one_held(tmp_path: Path):
    """--dry-run on reserve-plan where one leg is held exits 1."""
    _run_cli(
        tmp_path, "reserve",
        "--domain", "d1", "--pr", "99", "--agent", "x", "--branch", "y",
    )
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "plan": [
            {"domain": "d1", "symbols": []},
            {"domain": "d2", "symbols": []},
        ]
    }))
    r = _run_cli(
        tmp_path, "reserve-plan",
        "--pr", "1", "--agent", "a", "--branch", "b",
        "--plan", str(plan),
        "--dry-run",
    )
    assert r.returncode == 1
    assert "HELD" in r.stderr
    assert "WOULD-RESERVE" in r.stdout


# ── Advisory domain tests ─────────────────────────────────────────────────────

def test_advisory_domain_does_not_block(tmp_path: Path):
    """Advisory domains: check() returns ok=True with advisory_held populated."""
    reg = _reg({
        "domains": {
            "enforced": {"paths": ["a.py"]},
            "advisory_dom": {"paths": ["b.py"], "advisory": True},
        }
    })
    log = LockLog(tmp_path / "locks.jsonl")
    reserve(log, reg, domain="advisory_dom", pr=1, agent="a1", branch="b1")

    res = check(log, reg, files=["b.py"], pr=2)
    assert res.ok, "advisory conflict must not block (ok must be True)"
    assert res.held == []
    assert len(res.advisory_held) == 1
    assert res.advisory_held[0][0] == "advisory_dom"


def test_enforced_domain_still_blocks_when_advisory_also_held(tmp_path: Path):
    """Mix: advisory allows, but enforced still blocks."""
    reg = _reg({
        "domains": {
            "enforced": {"paths": ["a.py"]},
            "advisory_dom": {"paths": ["b.py"], "advisory": True},
        }
    })
    log = LockLog(tmp_path / "locks.jsonl")
    reserve(log, reg, domain="enforced", pr=1, agent="a1", branch="b1")
    reserve(log, reg, domain="advisory_dom", pr=1, agent="a1", branch="b1")

    # Only enforced file: blocked
    res = check(log, reg, files=["a.py"], pr=2)
    assert not res.ok
    assert len(res.held) == 1
    assert res.advisory_held == []

    # Only advisory file: allowed with warning
    res2 = check(log, reg, files=["b.py"], pr=2)
    assert res2.ok
    assert res2.advisory_held[0][0] == "advisory_dom"

    # Both files: blocked (enforced wins)
    res3 = check(log, reg, files=["a.py", "b.py"], pr=2)
    assert not res3.ok
    assert res3.held[0][0] == "enforced"
    assert res3.advisory_held[0][0] == "advisory_dom"


def test_advisory_not_in_free_domains_so_hook_skips_reserve(tmp_path: Path):
    """Advisory held domains must not appear in free_domains (hook skips reserve for them)."""
    reg = _reg({
        "domains": {
            "advisory_dom": {"paths": ["b.py"], "advisory": True},
        }
    })
    log = LockLog(tmp_path / "locks.jsonl")
    reserve(log, reg, domain="advisory_dom", pr=1, agent="a1", branch="b1")

    # check() for PR 2: domain is NOT in free (can't be reserved), but ok=True
    res = check(log, reg, files=["b.py"], pr=2)
    assert res.ok
    assert "advisory_dom" not in res.free, "advisory held domain must not be in free_domains"
    assert res.advisory_held[0][0] == "advisory_dom"
