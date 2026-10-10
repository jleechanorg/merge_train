"""The pre-commit hook must invoke predict-conflicts with only real CLI args."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "merge_train" / "hooks" / "pre-commit.sh"


def test_regression_precommit_passes_no_stray_subcommand(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.txt"
    shim = bindir / "predict-conflicts"
    shim.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{argv_log}"\necho "{{}}"\n')
    shim.chmod(0o755)

    subprocess.run(
        ["bash", str(HOOK)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "MERGE_TRAIN_PR": "5"},
    )

    argv = argv_log.read_text().split()
    assert "predict-conflicts" not in argv, argv
    assert argv[argv.index("--from-prs") + 1] == "5"
