"""Tests for merge_train.domain_lock core lib + CLI behavior."""

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
    UnknownPathError,
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


def test_cli_missing_registry_exit2(tmp_path: Path):
    cmd = [
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(tmp_path / "missing.yaml"),
        "--log", str(tmp_path / "log.jsonl"),
        "check", "--files", "a.py",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 2
