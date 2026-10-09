"""Tests for the bash hook script's logfile behavior.

The bash script (``conflict-warn-pre-tool.sh``) is what ``install-hooks``
copies to ``~/.local/bin/``. It must:

1. Still emit the JSON envelope on stdout (so the CLI's parser works).
2. Still emit status lines on stderr (so Codex/Agy TUI see them).
3. Write a logfile under the configured log root
   containing timestamp + stdin payload + exit code.

We target the **in-tree** script under ``merge_train/hooks/`` so the test
is immune to a stale ``~/.local/bin/`` install (e.g., if the user hasn't
re-run ``install-hooks`` after a security fix). Falls back to the global
copy if the in-tree file is missing (e.g., wheel install) and skips
with a clear message otherwise — same pattern as
``tests/test_conflict_helper.py::_helper_path_for_test``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "merge_train" / "hooks" / "conflict-warn-pre-tool.sh"
_HOOK_SCRIPT_FALLBACK = Path.home() / ".local" / "bin" / "conflict-warn-pre-tool.sh"


def _hook_script() -> Path:
    if HOOK_SCRIPT.is_file():
        return HOOK_SCRIPT
    if _HOOK_SCRIPT_FALLBACK.is_file():
        return _HOOK_SCRIPT_FALLBACK
    pytest.skip("conflict-warn-pre-tool.sh not found in-tree or at ~/.local/bin/")


@pytest.fixture
def log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give each test a private hook log root."""
    root = tmp_path / "merge_train-logs"
    monkeypatch.setenv("MERGE_TRAIN_LOG_ROOT", str(root))
    return root


def test_hook_script_writes_logfile(log_root: Path) -> None:
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
    log_dir = log_root / repo.name / branch
    assert log_dir.is_dir(), f"log dir not created: {log_dir}"
    logs = sorted(log_dir.glob("hook-*.log"))
    assert logs, f"no log file in {log_dir}"

    content = logs[-1].read_text()
    assert "=== Edit attempt" in content
    # The Edit body (new_string) is REDACTED from the log entry — only the
    # file_path + tool_name summary is written. This prevents leaking the
    # literal content the agent is about to write into a world-readable log.
    assert "new_string" not in content, (
        f"new_string leaked into log; expected body=<redacted>. Content: {content!r}"
    )
    assert "body=<redacted>" in content or '"new_string"' not in content
    assert "hello.py" in content, "file_path should still be present in log"
    assert "exit=0" in content
    assert "stdout:" in content


def test_hook_script_still_emits_valid_json(log_root: Path) -> None:
    """No-conflict stdout must be empty/whitespace for implicit allow.

    Codex rejects legacy ``decision:"approve"``. Conflict warnings still emit
    JSON context and are covered in
    ``test_conflict_helper.py::test_warn_only_conflict_exits_zero_with_context``.
    """
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
    assert result.returncode == 0
    assert result.stdout == b""


def test_hook_script_is_silent_without_conflicts(log_root: Path) -> None:
    """Routine successful checks must not interrupt the coding CLI's TUI."""
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
    assert err == "", f"routine check polluted the CLI TUI: {err!r}"


def test_hook_script_handles_non_git_cwd(log_root: Path, tmp_path: Path) -> None:
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
    # CRITICAL: the non-git path must not pollute stderr with a
    # `tee: No such file or directory` error (C2 in the adversarial review).
    err = result.stderr.decode()
    assert "tee:" not in err, (
        f"hook polluted stderr on non-git cwd; defeats chat-visible UX. Got: {err!r}"
    )
    # And no log dir should have been created for `no-repo` when REPO_ROOT is empty.
    no_repo_dir = log_root / "no-repo"
    assert not no_repo_dir.exists() or not any(no_repo_dir.iterdir()), (
        f"non-git cwd should not create a no-repo log dir; got: {list(no_repo_dir.iterdir())}"
    )


def test_hook_script_redacts_arbitrary_body_keys(log_root: Path) -> None:
    """Regression test for the brittle redaction logic.

    The original code only invoked the Python redaction helper when the
    input JSON contained one of the well-known Edit/Write keys
    (``new_string``, ``new_text``, ``content``). If an agent tool used a
    different key (e.g. ``insert_text``, ``replacement``, or a custom
    field), the redaction was skipped and the full payload — potentially
    containing secrets — was written verbatim to the log.

    This test feeds a synthetic tool invocation with a custom key
    (``insert_text``) carrying a secret-looking value and asserts that
    the literal value does NOT appear in the logfile. The fix made the
    Python redaction unconditional.
    """
    repo = Path(__file__).resolve().parents[1]
    payload = json.dumps(
        {
            "tool_name": "CustomTool",
            "tool_input": {
                "file_path": str(repo / "hello.py"),
                "insert_text": "SUPER_SECRET_TOKEN_42",
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
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr.decode()}"

    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip() or "detached"
    log_dir = log_root / repo.name / branch
    logs = sorted(log_dir.glob("hook-*.log"))
    assert logs, f"no log file in {log_dir}"
    content_text = logs[-1].read_text()
    # The secret literal must NOT appear anywhere in the log, regardless
    # of which JSON key carried it. The Python helper writes only
    # tool_name + basename(file_path) + body=<redacted>.
    assert "SUPER_SECRET_TOKEN_42" not in content_text, (
        f"secret value leaked into log via non-standard key 'insert_text'. "
        f"Log content: {content_text!r}"
    )
    assert "body=<redacted>" in content_text, (
        f"expected body=<redacted> marker; got: {content_text!r}"
    )
    assert "insert_text" not in content_text, (
        f"insert_text key leaked into log; expected body=<redacted> only. "
        f"Log content: {content_text!r}"
    )


def test_hook_script_logfile_is_owner_only(log_root: Path) -> None:
    """The log file must be 0600 (owner-only) and the log dir 0700.
    Otherwise on a shared box, any local user can read every file the
    agent edited plus the literal Edit body (C1 in adversarial review)."""
    import stat
    import datetime

    repo = Path(__file__).resolve().parents[1]
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "hello.py"), "new_string": "SECRET = 42"}}
    )
    subprocess.run(
        ["bash", str(_hook_script())],
        input=payload.encode(),
        capture_output=True,
        cwd=repo,
        timeout=30,
    )
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip() or "detached"
    log_date = datetime.date.today().isoformat()
    log_file = log_root / repo.name / branch / f"hook-{log_date}.log"
    assert log_file.exists(), f"log file not created: {log_file}"
    mode = stat.S_IMODE(log_file.stat().st_mode)
    assert mode & 0o077 == 0, f"log file is group/world readable: mode={oct(mode)}"
    log_dir = log_file.parent
    dmode = stat.S_IMODE(log_dir.stat().st_mode)
    assert dmode & 0o077 == 0, f"log dir is group/world readable: mode={oct(dmode)}"
