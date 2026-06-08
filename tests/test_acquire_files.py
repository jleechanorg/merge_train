"""Tests for the ``acquire --files`` command (predict-conflicts era).

``acquire --files`` is a declarative collision check: given a list of
files (and the in-flight PR set), decide whether the request can be
allowed or must be denied. The command is atomic — a single conflict
on any file denies the whole transaction.

Tests cover: mapped files, unmapped files (fallback), mixed
mapped/unmapped, atomicity, collision rollback, CLI exit codes, and
flock concurrency.

Reframed from the original ``acquire --files`` spec that targeted the
now-deleted ``domain_lock`` API (PR #19, 2026-06-02). See
``docs/acquire_files_spec.md`` for the new design.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest
import yaml

# --------------------------------------------------------------------------- #
# CLI path
# --------------------------------------------------------------------------- #

CLI_MODULE = "merge_train.acquire"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def reg() -> dict:
    return {
        "domains": {
            "world": {"paths": ["mvp_site/world_logic.py"]},
            "rewards": {"paths": ["mvp_site/rewards_engine.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
        }
    }


@pytest.fixture
def reg_file(tmp_path: Path, reg: dict) -> Path:
    p = tmp_path / "reg.yaml"
    p.write_text(yaml.safe_dump(reg))
    return p


@pytest.fixture
def empty_reg_file(tmp_path: Path) -> Path:
    """An empty registry — every file becomes unmapped (file-level fallback)."""
    p = tmp_path / "reg.yaml"
    p.write_text(yaml.safe_dump({"domains": {}}))
    return p


def _write_plan(tmp_path: Path, prs: list[dict]) -> Path:
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({"prs": prs}))
    return p


# --------------------------------------------------------------------------- #
# Resolver unit tests (no CLI, no git)
# --------------------------------------------------------------------------- #


def test_resolve_file_mapped_uses_touched_symbols(monkeypatch):
    """When a file is mapped, the resolver must call
    ``touched_symbols_for_staged_file`` and return its symbol set."""
    from merge_train import acquire as acquire_mod

    calls: list[tuple[str, Optional[Path]]] = []

    def fake_resolve(path, *, cwd=None):
        calls.append((path, cwd))
        return {"func_a", "Class_b"}

    monkeypatch.setattr(
        "merge_train.acquire.touched_symbols_for_staged_file", fake_resolve
    )

    resolved, fallback = acquire_mod.resolve_files_to_symbols(
        ["mvp_site/world_logic.py"]
    )
    assert "mvp_site/world_logic.py" in resolved
    assert resolved["mvp_site/world_logic.py"] == {"func_a", "Class_b"}
    assert fallback == []
    assert calls and calls[0][0] == "mvp_site/world_logic.py"


def test_resolve_file_unmapped_uses_file_level_fallback(monkeypatch):
    """Unmapped / unsupported files fall back to a file-level lock sentinel."""
    from merge_train import acquire as acquire_mod

    def fake_resolve(path, *, cwd=None):
        raise acquire_mod.UnsupportedLanguageError(f"no support for {path}")

    monkeypatch.setattr(
        "merge_train.acquire.touched_symbols_for_staged_file", fake_resolve
    )

    resolved, fallback = acquire_mod.resolve_files_to_symbols(["some/random.bin"])
    assert fallback == ["some/random.bin"]
    assert resolved["some/random.bin"] == {"file:some/random.bin"}


def test_resolve_file_mixed_mapped_and_unmapped(monkeypatch):
    from merge_train import acquire as acquire_mod

    def fake_resolve(path, *, cwd=None):
        if path.endswith(".py"):
            return {"func_a"}
        raise acquire_mod.UnsupportedLanguageError(f"no support for {path}")

    monkeypatch.setattr(
        "merge_train.acquire.touched_symbols_for_staged_file", fake_resolve
    )

    resolved, fallback = acquire_mod.resolve_files_to_symbols(
        ["src/foo.py", "data/blob.bin"]
    )
    assert resolved["src/foo.py"] == {"func_a"}
    assert resolved["data/blob.bin"] == {"file:data/blob.bin"}
    assert fallback == ["data/blob.bin"]


def test_resolve_file_empty_source_returns_empty(monkeypatch):
    """An empty/missing diff (no changes) resolves to an empty symbol set,
    not a fallback — we treat it as 'no lock unit needed'."""
    from merge_train import acquire as acquire_mod

    def fake_resolve(path, *, cwd=None):
        return set()

    monkeypatch.setattr(
        "merge_train.acquire.touched_symbols_for_staged_file", fake_resolve
    )

    resolved, fallback = acquire_mod.resolve_files_to_symbols(["src/empty.py"])
    assert resolved["src/empty.py"] == set()
    assert fallback == []


# --------------------------------------------------------------------------- #
# Decision unit tests (no CLI, no git, no flock)
# --------------------------------------------------------------------------- #


def test_decide_no_inflight_allow(monkeypatch):
    """An empty in-flight set always results in ALLOW."""
    from merge_train import acquire as acquire_mod
    from merge_train.predict import PRSpec, Plan

    fake_plan = Plan(
        input_prs=[],
        pairwise_conflicts=[],
        parallel_batches=[],
        recommended_order=[],
    )
    monkeypatch.setattr(acquire_mod, "predict_conflicts", lambda *a, **kw: fake_plan)
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({"a.py": {"func"}}, []),
    )

    result = acquire_mod.decide(
        files=["a.py"],
        in_flight=[],
        registry=acquire_mod.Registry.empty(),
        branch="feat/x",
        agent="claude",
    )
    assert result.decision == "allow"
    assert list(result.conflicts) == []


def test_decide_symbol_conflict_deny(monkeypatch):
    """A symbol-level overlap with an in-flight PR denies the request."""
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        DomainConflict,
        PairConflict,
        Plan,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=1,
        domain_conflicts=(
            DomainConflict(domain="world", symbols=("func",), advisory=False),
        ),
        textual_conflicts=(),
    )
    fake_plan = Plan(
        input_prs=[1],
        pairwise_conflicts=[pair],
        parallel_batches=[],
        recommended_order=[],
    )
    monkeypatch.setattr(acquire_mod, "predict_conflicts", lambda *a, **kw: fake_plan)
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({"a.py": {"func"}}, []),
    )

    result = acquire_mod.decide(
        files=["a.py"],
        in_flight=[],
        registry=acquire_mod.Registry.empty(),
        branch="feat/x",
        agent="claude",
    )
    assert result.decision == "deny"
    assert list(result.conflicts) == [
        {"domain": "world", "symbols": ["func"], "conflicting_pr": 1}
    ]


def test_decide_textual_conflict_deny(monkeypatch):
    """A textual conflict (no domain overlap) also denies."""
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        PairConflict,
        Plan,
        TextualConflict,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=2,
        domain_conflicts=(),
        textual_conflicts=(TextualConflict(file="pyproject.toml"),),
    )
    fake_plan = Plan(
        input_prs=[2],
        pairwise_conflicts=[pair],
        parallel_batches=[],
        recommended_order=[],
    )
    monkeypatch.setattr(acquire_mod, "predict_conflicts", lambda *a, **kw: fake_plan)
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({"pyproject.toml": set()}, []),
    )

    result = acquire_mod.decide(
        files=["pyproject.toml"],
        in_flight=[],
        registry=acquire_mod.Registry.empty(),
        branch="feat/x",
        agent="claude",
    )
    assert result.decision == "deny"
    assert result.conflicts[0]["domain"] is None
    assert result.conflicts[0]["conflicting_pr"] == 2


def test_decide_advisory_only_allow(monkeypatch):
    """Advisory-only domain conflicts do NOT block the request."""
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        DomainConflict,
        PairConflict,
        Plan,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=3,
        domain_conflicts=(
            DomainConflict(domain="world", symbols=("func",), advisory=True),
        ),
        textual_conflicts=(),
    )
    fake_plan = Plan(
        input_prs=[3],
        pairwise_conflicts=[pair],
        parallel_batches=[],
        recommended_order=[],
    )
    monkeypatch.setattr(acquire_mod, "predict_conflicts", lambda *a, **kw: fake_plan)
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({"a.py": {"func"}}, []),
    )

    result = acquire_mod.decide(
        files=["a.py"],
        in_flight=[],
        registry=acquire_mod.Registry.empty(),
        branch="feat/x",
        agent="claude",
    )
    assert result.decision == "allow"


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #


def test_atomic_partial_conflict_denies_all(monkeypatch):
    """5 files requested, 1 conflict on file 3 → whole transaction denied."""
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        DomainConflict,
        PairConflict,
        Plan,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=7,
        domain_conflicts=(
            DomainConflict(domain="d", symbols=("sym",), advisory=False),
        ),
        textual_conflicts=(),
    )
    fake_plan = Plan(
        input_prs=[7],
        pairwise_conflicts=[pair],
        parallel_batches=[],
        recommended_order=[],
    )

    call_count = {"n": 0}

    def fake_resolve(files, **kw):
        call_count["n"] += 1
        return ({f: {"sym"} for f in files}, [])

    monkeypatch.setattr(acquire_mod, "predict_conflicts", lambda *a, **kw: fake_plan)
    monkeypatch.setattr("merge_train.acquire.resolve_files_to_symbols", fake_resolve)

    result = acquire_mod.decide(
        files=[f"f{i}.py" for i in range(5)],
        in_flight=[],
        registry=acquire_mod.Registry.empty(),
        branch="feat/x",
        agent="claude",
    )
    # The resolver is called exactly once with all 5 files (atomic batch)
    assert call_count["n"] == 1
    assert result.decision == "deny"
    # All 5 files appear in the output (not just the conflicting one)
    assert len(result.files) == 5
    assert len(result.resolved) == 5


# --------------------------------------------------------------------------- #
# CLI integration tests
# --------------------------------------------------------------------------- #


def _build_cli_env(
    plan: Path, reg: Path, lock_path: Path, files: list[str], **extra
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        CLI_MODULE,
        "--plan",
        str(plan),
        "--registry",
        str(reg),
        "--lock-path",
        str(lock_path),
        "--no-flock",  # tests must not take a real flock by default
    ]
    # Map the test-friendly kwarg names to the actual argparse flag names.
    # ``json=True`` -> --json (store_true, no value)
    # ``json_output=True`` -> --json (also store_true)
    for k, v in extra.items():
        if k == "json" and v is True:
            cmd.append("--json")
        elif k == "json_output" and v is True:
            cmd.append("--json")
        else:
            cmd.extend([f"--{k.replace('_', '-')}", str(v)])
    cmd.extend(files)
    return cmd


def _run_cli(
    plan: Path,
    reg: Path,
    lock_path: Path,
    files: list[str],
    **extra,
) -> tuple[int, str, str]:
    """Run the ``acquire`` CLI in-process and capture exit code + streams.

    This is the in-process equivalent of spawning ``python -m
    merge_train.acquire`` via :func:`subprocess.run`. We use it instead
    of subprocess because the tests rely on ``monkeypatch.setattr`` to
    override ``predict_conflicts`` and ``resolve_files_to_symbols``,
    and those patches do **not** propagate to a child Python process.
    """
    import contextlib
    import io

    from merge_train import acquire as acquire_mod

    argv = _build_cli_env(plan, reg, lock_path, files, **extra)
    stdout, stderr = io.StringIO(), io.StringIO()
    rc = 2
    # Strip ``[python, -m, merge_train.acquire]`` from the front of the
    # command list so we hand main() a clean argv.
    cli_argv = argv[3:]
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            rc = acquire_mod.main(cli_argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, stdout.getvalue(), stderr.getvalue()


def test_cli_mapped_no_conflict_exit0(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod

    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 1, "branch": "b1", "files": ["other.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["a.py"])
    assert rc == 0, stderr
    assert "Decision: allow" in stdout


def test_cli_mapped_with_conflict_exit1(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        DomainConflict,
        PairConflict,
        Plan,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=1,
        domain_conflicts=(
            DomainConflict(domain="d", symbols=("sym",), advisory=False),
        ),
        textual_conflicts=(),
    )
    monkeypatch.setattr(
        acquire_mod,
        "predict_conflicts",
        lambda *a, **kw: Plan(
            input_prs=[1],
            pairwise_conflicts=[pair],
            parallel_batches=[],
            recommended_order=[],
        ),
    )
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 1, "branch": "b1", "files": ["a.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["a.py"])
    assert rc == 1, stderr
    assert "Decision: deny" in stdout
    assert "PR#1" in stdout


def test_cli_unmapped_uses_fallback(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod

    def fake_resolve(files, **kw):
        resolved = {f: ({"sym"} if f.endswith(".py") else {"file:" + f}) for f in files}
        fallback = [f for f in files if not f.endswith(".py")]
        return resolved, fallback

    monkeypatch.setattr("merge_train.acquire.resolve_files_to_symbols", fake_resolve)

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 1, "branch": "b1", "files": ["other.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["data/blob.bin"])
    assert rc == 0, stderr
    assert "fallback" in stdout


def test_cli_mixed_mapped_and_unmapped(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod

    def fake_resolve(files, **kw):
        resolved = {f: ({"sym"} if f.endswith(".py") else {"file:" + f}) for f in files}
        fallback = [f for f in files if not f.endswith(".py")]
        return resolved, fallback

    monkeypatch.setattr("merge_train.acquire.resolve_files_to_symbols", fake_resolve)

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 1, "branch": "b1", "files": ["other.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["src/foo.py", "data/blob.bin"])
    assert rc == 0, stderr
    assert "src/foo.py" in stdout
    assert "data/blob.bin" in stdout


def test_cli_atomic_partial_conflict_denies_all(
    tmp_path: Path, reg_file: Path, monkeypatch
):
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        DomainConflict,
        PairConflict,
        Plan,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=1,
        domain_conflicts=(
            DomainConflict(domain="d", symbols=("sym",), advisory=False),
        ),
        textual_conflicts=(),
    )
    monkeypatch.setattr(
        acquire_mod,
        "predict_conflicts",
        lambda *a, **kw: Plan(
            input_prs=[1],
            pairwise_conflicts=[pair],
            parallel_batches=[],
            recommended_order=[],
        ),
    )
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 1, "branch": "b1", "files": ["f2.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, [f"f{i}.py" for i in range(5)])
    assert rc == 1, stderr
    # All 5 files must appear in the human output (no partial accept)
    for i in range(5):
        assert f"f{i}.py" in stdout


def test_cli_json_output_shape(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod

    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 1, "branch": "b1", "files": ["other.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["a.py"], json=True)
    assert rc == 0, stderr
    payload = json.loads(stdout)
    assert payload["decision"] == "allow"
    assert payload["files"] == ["a.py"]
    assert payload["resolved"] == {"a.py": ["sym"]}
    assert payload["fallback_files"] == []
    assert payload["conflicts"] == []
    assert payload["in_flight_prs"] == [1]
    assert payload["candidate"]["branch"] == "acquire"
    assert "flock_path" in payload


def test_cli_json_deny_includes_conflicts(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod
    from merge_train.predict import (
        DomainConflict,
        PairConflict,
        Plan,
    )

    pair = PairConflict(
        pr_a=acquire_mod._SYNTHETIC_CANDIDATE_PR,
        pr_b=42,
        domain_conflicts=(
            DomainConflict(domain="d", symbols=("sym",), advisory=False),
        ),
        textual_conflicts=(),
    )
    monkeypatch.setattr(
        acquire_mod,
        "predict_conflicts",
        lambda *a, **kw: Plan(
            input_prs=[42],
            pairwise_conflicts=[pair],
            parallel_batches=[],
            recommended_order=[],
        ),
    )
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(
        tmp_path,
        [
            {"pr": 42, "branch": "b42", "files": ["a.py"]},
        ],
    )
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["a.py"], json=True)
    assert rc == 1, stderr
    payload = json.loads(stdout)
    assert payload["decision"] == "deny"
    assert payload["conflicts"] == [
        {"domain": "d", "symbols": ["sym"], "conflicting_pr": 42}
    ]


def test_cli_missing_plan_exit2(tmp_path: Path, reg_file: Path):
    plan = tmp_path / "missing.yaml"
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["a.py"])
    assert rc == 2


def test_cli_malformed_plan_exit2(tmp_path: Path, reg_file: Path):
    plan = tmp_path / "bad.yaml"
    plan.write_text("this is: : not valid: yaml: [")
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, ["a.py"])
    assert rc == 2


def test_cli_no_files_exit2(tmp_path: Path, reg_file: Path):
    plan = _write_plan(tmp_path, [])
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(plan, reg_file, lock, [])
    assert rc == 2


def test_cli_branch_and_agent_propagate(tmp_path: Path, reg_file: Path, monkeypatch):
    from merge_train import acquire as acquire_mod

    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(tmp_path, [{"pr": 1, "branch": "b1", "files": ["other.py"]}])
    lock = tmp_path / "acquire.lock"
    rc, stdout, stderr = _run_cli(
        plan,
        reg_file,
        lock,
        ["a.py"],
        branch="feat/x",
        agent="claude-code",
    )
    assert rc == 0, stderr
    assert "branch=feat/x" in stdout
    assert "agent=claude-code" in stdout


# --------------------------------------------------------------------------- #
# Concurrency (flock)
# --------------------------------------------------------------------------- #


def test_flock_serializes_concurrent_invocations(tmp_path: Path):
    """Two concurrent ``acquire`` invocations on the same lock path must
    serialize: the second waits for the first to release."""
    from merge_train import acquire as acquire_mod

    lock_path = tmp_path / "acquire.lock"

    # Acquire the lock manually to simulate a holder
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Spawn a CLI subprocess that must wait for the lock
        plan = _write_plan(tmp_path, [])
        reg = tmp_path / "reg.yaml"
        reg.write_text(yaml.safe_dump({"domains": {}}))
        cmd = [
            sys.executable,
            "-m",
            CLI_MODULE,
            "--plan",
            str(plan),
            "--registry",
            str(reg),
            "--lock-path",
            str(lock_path),
            "--lock-timeout",
            "2",  # 2 second timeout for the test
            "a.py",
        ]
        start = time.monotonic()
        r = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.monotonic() - start
        # The subprocess must have either timed out (>1.5s) or failed
        # (couldn't acquire lock). Either is acceptable; the point is
        # it must NOT have proceeded immediately.
        if r.returncode == 0:
            # It proceeded — but only if the parent released first.
            # Force a fail by tightening the assertion: elapsed must be
            # at least 1s (timeout boundary).
            assert elapsed >= 1.0, f"acquire proceeded without waiting: {elapsed}s"
        else:
            # Lock acquisition failed — this is the expected outcome
            # since the parent holds the lock.
            assert r.returncode in (1, 2), f"unexpected rc={r.returncode}: {r.stderr}"
    finally:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_no_flock_skips_lock_file(tmp_path: Path, monkeypatch):
    """``--no-flock`` must not create or use the lock file."""
    from merge_train import acquire as acquire_mod

    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({f: {"sym"} for f in files}, []),
    )

    plan = _write_plan(tmp_path, [{"pr": 1, "branch": "b1", "files": ["other.py"]}])
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({"domains": {}}))
    nonexistent_lock = tmp_path / "should_not_appear.lock"

    rc, stdout, stderr = _run_cli(plan, reg, nonexistent_lock, ["a.py"])
    assert rc == 0, stderr
    assert (
        not nonexistent_lock.exists()
    ), "lock file should not be created with --no-flock"


# --------------------------------------------------------------------------- #
# Edge: candidate has no symbols and no files
# --------------------------------------------------------------------------- #


def test_decide_empty_files_is_allow(monkeypatch):
    """An empty file list (after CLI-level guard) should not be exercised
    in ``decide`` — but defensively, an empty in-flight check is allow."""
    from merge_train import acquire as acquire_mod
    from merge_train.predict import Plan

    monkeypatch.setattr(
        acquire_mod,
        "predict_conflicts",
        lambda *a, **kw: Plan(
            input_prs=[],
            pairwise_conflicts=[],
            parallel_batches=[],
            recommended_order=[],
        ),
    )
    monkeypatch.setattr(
        "merge_train.acquire.resolve_files_to_symbols",
        lambda files, **kw: ({}, []),
    )

    result = acquire_mod.decide(
        files=[],
        in_flight=[],
        registry=acquire_mod.Registry.empty(),
        branch="feat/x",
        agent="claude",
    )
    assert result.decision == "allow"
    assert list(result.conflicts) == []


def test_from_prs_exception_chaining():
    from merge_train import acquire as acquire_mod
    import pytest

    with pytest.raises(ValueError) as excinfo:
        acquire_mod._load_from_prs("invalid_pr", None)
    assert excinfo.value.__cause__ is not None


def test_lock_acquire_exception_chaining(tmp_path):
    from merge_train import acquire as acquire_mod
    import pytest
    import os
    import fcntl

    lock_path = tmp_path / "test.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(acquire_mod.LockAcquireError) as excinfo:
            with acquire_mod.acquire_flock(
                lock_path, timeout_seconds=0.01, enabled=True
            ):
                pass
        assert excinfo.value.__cause__ is not None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
