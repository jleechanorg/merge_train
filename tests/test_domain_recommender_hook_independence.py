"""test_domain_recommender must not depend on the host's global git hooks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_regression_domain_recommender_ignores_global_git_hooks(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho blocked-by-global-hook >&2\nexit 1\n")
    hook.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[core]\n\thooksPath = {hooks}\n")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(Path(__file__).parent / "test_domain_recommender.py")],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-400:]
