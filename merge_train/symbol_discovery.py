"""Automatic symbol discovery from git/GitHub PR diffs.

Extracts which Python symbols (functions, classes, methods) are touched
by a PR or the current staged changes — so callers can populate
``PRSpec.symbols_by_file`` without hand-authoring YAML.

The main entry points:

* ``symbols_from_staged_diff(cwd)`` — reads the current git index.
* ``symbols_from_pr_diff(pr_number, repo)`` — uses ``gh pr diff``.

Both return ``dict[str, set[str]]`` mapping file path -> symbol names.
Non-Python files and files that fail to parse are silently omitted
(callers fall back to whole-file locking for them).
"""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from typing import Optional

from merge_train.symbols import (
    UnsupportedLanguageError,
    is_python_path,
    parse_hunks,
    staged_content_for_file,
    staged_diff_for_file,
    touched_symbols,
)


# --------------------------------------------------------------------------- #
# Unified-diff splitter
# --------------------------------------------------------------------------- #

_FILE_HEADER_RE = re.compile(
    r"^diff --git a/(?P<path>.+?) b/(?P<bpath>.+)$"
)


def _split_diff_by_file(diff_text: str) -> dict[str, str]:
    """Split a unified diff into per-file slices keyed by the 'b/' path."""
    result: dict[str, str] = {}
    current_path: Optional[str] = None
    current_lines: list[str] = []

    for line in diff_text.splitlines(keepends=True):
        m = _FILE_HEADER_RE.match(line)
        if m:
            if current_path and current_lines:
                result[current_path] = "".join(current_lines)
            current_path = m.group("bpath")
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)

    if current_path and current_lines:
        result[current_path] = "".join(current_lines)
    return result


# --------------------------------------------------------------------------- #
# Staged-diff discovery
# --------------------------------------------------------------------------- #


def symbols_from_staged_diff(
    cwd: Optional[Path] = None,
) -> dict[str, set[str]]:
    """Return touched symbols for every staged Python file.

    Runs ``git diff --staged --name-only`` to enumerate changed files,
    then resolves symbols for each ``.py`` file via the index.

    Returns ``{file_path: set_of_symbol_names}``. Files that can't be
    symbol-resolved (non-Python, parse errors) are silently omitted.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--staged", "--name-only", "--diff-filter=ACMRT"],
            capture_output=True, text=True, check=False,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        return {}
    if proc.returncode != 0:
        return {}

    result: dict[str, set[str]] = {}
    for path in proc.stdout.splitlines():
        path = path.strip()
        if not path or not is_python_path(path):
            continue
        try:
            diff = staged_diff_for_file(path, cwd=cwd)
            if not diff.strip():
                continue
            content = staged_content_for_file(path, cwd=cwd)
            syms = touched_symbols(new_source=content, diff_text=diff)
            result[path] = syms
        except (UnsupportedLanguageError, RuntimeError, FileNotFoundError):
            pass
    return result


# --------------------------------------------------------------------------- #
# PR diff discovery (via gh CLI)
# --------------------------------------------------------------------------- #


def _gh_pr_diff(pr_number: int, repo: Optional[str] = None) -> str:
    """Fetch the full unified diff for a GitHub PR via the ``gh`` CLI."""
    cmd = ["gh", "pr", "diff", str(pr_number), "--patch"]
    if repo:
        cmd = ["gh", "pr", "diff", str(pr_number), "--repo", repo, "--patch"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _gh_pr_head_ref(pr_number: int, repo: Optional[str] = None) -> str:
    """Return the head branch name for a PR."""
    cmd = ["gh", "pr", "view", str(pr_number),
           "--json", "headRefName", "--jq", ".headRefName"]
    if repo:
        cmd = ["gh", "pr", "view", str(pr_number), "--repo", repo,
               "--json", "headRefName", "--jq", ".headRefName"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return proc.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def _gh_file_content_at_ref(
    path: str,
    ref: str,
    repo: str,
) -> str:
    """Fetch file content at *ref* from GitHub via ``gh api``."""
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}",
             "--jq", ".content"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            return ""
        raw_b64 = proc.stdout.strip().replace("\n", "")
        return base64.b64decode(raw_b64).decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.SubprocessError, Exception):
        return ""


def symbols_from_pr_diff(
    pr_number: int,
    repo: Optional[str] = None,
) -> dict[str, set[str]]:
    """Return touched symbols for every Python file changed in a GitHub PR.

    Uses ``gh pr diff --patch`` to get the full diff, splits it per file,
    and calls :func:`~merge_train.symbols.touched_symbols` for each ``.py``
    file. Post-edit content is fetched via ``gh api``.

    Files that cannot be fetched or parsed are silently omitted.
    Returns ``{file_path: set_of_symbol_names}``.
    """
    diff_text = _gh_pr_diff(pr_number, repo)
    if not diff_text:
        return {}
    file_diffs = _split_diff_by_file(diff_text)
    if not file_diffs:
        return {}

    head_ref = _gh_pr_head_ref(pr_number, repo) if repo else ""
    result: dict[str, set[str]] = {}

    for path, file_diff in file_diffs.items():
        if not is_python_path(path):
            continue
        hunks = parse_hunks(file_diff)
        if not hunks:
            continue
        content = ""
        if repo and head_ref:
            content = _gh_file_content_at_ref(path, head_ref, repo)
        if not content:
            continue
        try:
            syms = touched_symbols(new_source=content, diff_text=file_diff)
            result[path] = syms
        except Exception:
            pass
    return result


def symbols_from_files_in_pr(
    pr_number: int,
    files: list[str],
    repo: Optional[str] = None,
) -> dict[str, set[str]]:
    """Enrich a file list with their touched symbols from a PR diff.

    Efficient: fetches the full diff once, filters to *files*.
    Returns only files that have non-empty touched symbol sets.
    """
    diff_text = _gh_pr_diff(pr_number, repo)
    if not diff_text:
        return {}
    file_diffs = _split_diff_by_file(diff_text)
    head_ref = _gh_pr_head_ref(pr_number, repo) if repo else ""
    requested = set(files)
    result: dict[str, set[str]] = {}

    for path, file_diff in file_diffs.items():
        if path not in requested or not is_python_path(path):
            continue
        hunks = parse_hunks(file_diff)
        if not hunks:
            continue
        content = ""
        if repo and head_ref:
            content = _gh_file_content_at_ref(path, head_ref, repo)
        if not content:
            continue
        try:
            syms = touched_symbols(new_source=content, diff_text=file_diff)
            if syms:
                result[path] = syms
        except Exception:
            pass
    return result
