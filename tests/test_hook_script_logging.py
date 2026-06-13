"""Tests for the bash hook script's logfile behavior.

The bash script (``conflict-warn-pre-tool.sh``) is what ``install-hooks``
copies to ``~/.local/bin/``. It must:

1. Still emit the JSON envelope on stdout (so the CLI's parser works).
2. Still emit status lines on stderr (so Codex/Agy TUI see them).
3. Write a logfile to ``/tmp/merge_train/{repo}/{branch}/hook-<date>.log``
   containing timestamp + stdin payload + exit code.

We run the installed script from ``~/.local/bin/`` to test the artifact
that production actually invokes. If not installed, we skip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path.home() / ".local" / "bin" / "conflict-warn-pre-tool.sh"


def _hook_script() -> Path:
    if not HOOK_SCRIPT.is_file():
        pytest.skip("conflict-warn-pre-tool.sh not installed at ~/.local/bin/")
    return HOOK_SCRIPT


@pytest.fixture
def clean_log_dir() -> None:
    """Best-effort cleanup of /tmp/merge_train/merge_train/<current_branch>
    before each test. The merge_train tests run in this repo, so the
    dir would accumulate from prior runs."""
    # Find the branch from the merge_train checkout (the most likely
    # test repo). Don't fail if we can't determine it.
    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():
        return
    branch_proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if branch_proc.returncode != 0:
        return
    branch = branch_proc.stdout.strip()
    target = Path("/tmp/merge_train") / repo.name / branch
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def test_hook_script_writes_logfile(clean_log_dir: None) -> None:
    """Running the bash script with a synthetic Edit produces a logfile."""
    repo = Path(__file__).resolve().parents[1]
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(repo / "hello.py"),
                "new_string": "x = 1",
            },
        }
    )
    result = subprocess.run(
        ["bash", str(_hook_script())],
        input=payload.encode(),
        capture_output=True,
        cwd=repo,
        timeout=30,
    )
    # The script exits 0 for a clean allow.
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr.decode()}"

    branch_proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    branch = branch_proc.stdout.strip() or "detached"
    log_dir = Path("/tmp/merge_train") / repo.name / branch
    assert log_dir.is_dir(), f"log dir not created: {log_dir}"
    logs = sorted(log_dir.glob("hook-*.log"))
    assert logs, f"no log file in {log_dir}"

    content = logs[-1].read_text()
    assert "=== Edit attempt" in content
    assert payload in content
    assert "exit=0" in content
    assert "stdout:" in content


def test_hook_script_still_emits_valid_json(clean_log_dir: None) -> None:
    """Stdout of the script must be a parseable JSON envelope (so the
    CLI's hook protocol parser still works)."""
    repo = Path(__file__).resolve().parents[1]
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(repo / "hello.py"),
                "new_string": "y = 1",
            },
        }
    )
    result = subprocess.run(
        ["bash", str(_hook_script())],
        input=payload.encode(),
        capture_output=True,
        cwd=repo,
        timeout=30,
    )
    # The script's stdout is the JSON envelope (last non-empty line).
    last = [ln for ln in result.stdout.decode().splitlines() if ln.strip()][-1]
    parsed = json.loads(last)
    assert "systemMessage" in parsed
    assert "hookSpecificOutput" in parsed
    assert parsed["hookSpecificOutput"]["permissionDecision"] in {"allow", "deny", "ask"}


def test_hook_script_mirrors_stderr(clean_log_dir: None) -> None:
    """Stderr is preserved (not swallowed) so Codex/Agy TUI can show it."""
    repo = Path(__file__).resolve().parents[1]
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(repo / "hello.py"),
                "new_string": "z = 1",
            },
        }
    )
    result = subprocess.run(
        ["bash", str(_hook_script())],
        input=payload.encode(),
        capture_output=True,
        cwd=repo,
        timeout=30,
    )
    err = result.stderr.decode()
    assert "merge_train: checking conflicts" in err, (
        f"stderr lost; the CLI TUI would see no status line. Got: {err!r}"
    )
    assert "merge_train: checked" in err


def test_hook_script_handles_non_git_cwd(clean_log_dir: None, tmp_path: Path) -> None:
    """If the cwd is not a git repo, the hook must not crash and must
    still emit a valid JSON envelope. Logging is best-effort and skipped."""
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x.py"}})
    result = subprocess.run(
        ["bash", str(_hook_script())],
        input=payload.encode(),
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0
    last = [ln for ln in result.stdout.decode().splitlines() if ln.strip()][-1]
    parsed = json.loads(last)
    assert "systemMessage" in parsed
    # And the reason should reflect that we're not in a git repo.
    assert "not inside a git repo" in parsed["systemMessage"]
