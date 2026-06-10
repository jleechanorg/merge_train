#!/usr/bin/env python3
"""PreToolUse conflict-check helper for Claude Code.

Invoked by ``conflict-warn-pre-tool.sh`` (installed at
``~/.local/bin/``). Reads a JSON tool request from stdin, looks for
open PRs that also touch the target file, and emits a Claude Code
PreToolUse decision on stdout.

Chat visibility
---------------
Every output decision carries a ``permissionDecisionReason`` so the
user sees a one-line summary in the chat (not just on stderr). When
conflicts are found, the reason is the full conflict breakdown.

Enforcement
-----------
Per-repo enforcement is read from ``~/merge_train/config.json`` via
:func:`merge_train.config.load_config`. If the config file is missing
or the package is not importable, falls back to the previous
hardcoded defaults (``merge_train`` repo = block, others = warn) so
existing installs keep working.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Add merge_train source locations to sys.path so we can import the
# package. The helper is normally called from ~/.local/bin/, where
# merge_train itself is NOT on sys.path.
for _p in (Path.home() / "merge_train", Path("/Users/jleechan/projects/merge_train")):
    if _p.exists():
        sys.path.insert(0, str(_p))

# Optional imports — fail-safe if the package isn't installed.
try:
    from merge_train.symbol_discovery import symbols_from_files_in_pr
    from merge_train.symbols import extract_symbols, is_python_path
except ImportError:  # pragma: no cover — fail-safe fallback
    print(json.dumps(_decision_payload("allow", "merge_train: package import failed; allowing")))
    sys.exit(0)

try:
    from merge_train.config import (
        default_config as _default_config,
        get_repo_alias as _get_repo_alias,
        load_config as _load_config,
        lookup_enforcement as _lookup_enforcement,
    )
except ImportError:  # pragma: no cover — fall back to legacy hardcoded enforcement
    _load_config = None
    _lookup_enforcement = None
    _get_repo_alias = None
    _default_config = None


# --------------------------------------------------------------------------- #
# Output helpers — every decision is chat-visible via permissionDecisionReason
# --------------------------------------------------------------------------- #


def _decision_payload(decision: str, reason: str) -> dict:
    """Build a PreToolUse hook output payload with a chat-visible reason.

    ``permissionDecisionReason`` is rendered in the chat UI, so the
    user sees the hook's verdict even on a silent allow.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
    }


def _emit(decision: str, reason: str) -> None:
    """Print the decision JSON to stdout (Claude Code reads this)."""
    print(json.dumps(_decision_payload(decision, reason)))


# --------------------------------------------------------------------------- #
# Enforcement resolution — config file > hardcoded legacy defaults
# --------------------------------------------------------------------------- #


def _legacy_enforcement(repo_name: str) -> str:
    """Pre-config-file fallback: merge_train = block, others = warn.

    Preserved verbatim so existing installs behave identically when
    the config file is absent or the package can't be imported.
    """
    if repo_name == "merge_train":
        return "block"
    return "warn"


def _resolve_enforcement(repo_root: str) -> tuple[str, str]:
    """Return ``(enforcement, alias)`` for the given repo.

    Resolution order:
        1. ``~/merge_train/config.json`` (if importable + present)
        2. Legacy hardcoded defaults (merge_train = block, others = warn)
    """
    alias = Path(repo_root).name
    if _load_config is None or _lookup_enforcement is None or _get_repo_alias is None:
        return _legacy_enforcement(alias), alias
    try:
        cfg = _load_config()
        mode = _lookup_enforcement(cfg, repo_root)
        alias = _get_repo_alias(cfg, repo_root)
        return mode, alias
    except Exception:
        return _legacy_enforcement(alias), alias


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            _emit("allow", "merge_train: empty payload; allowing.")
            return

        payload = json.loads(raw_input)
    except Exception:
        _emit("allow", "merge_train: payload parse failed; allowing.")
        return

    # Check tool name — only file-mutation tools get the conflict check.
    tool_name = (
        payload.get("name")
        or payload.get("tool_name")
        or payload.get("tool")
        or ""
    )
    if tool_name not in ("Edit", "Write", "replace_file_content", "multi_replace_file_content"):
        _emit("allow", f"merge_train: tool {tool_name!r} not a file mutation; skipping conflict check.")
        return

    # Extract target file path.
    tool_input = payload.get("input") or payload.get("tool_input") or {}
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("TargetFile")
        or payload.get("file_path")
        or payload.get("TargetFile")
        or ""
    )
    if not file_path:
        _emit("allow", "merge_train: no file_path in tool input; allowing.")
        return

    # Extract start and end lines (for symbol-level locking).
    start_line = tool_input.get("StartLine") or tool_input.get("startLine") or tool_input.get("StartLineNumber")
    end_line = tool_input.get("EndLine") or tool_input.get("endLine") or tool_input.get("EndLineNumber")

    # Resolve current git repo.
    try:
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        _emit("allow", "merge_train: not inside a git repo; allowing.")
        return

    repo_path = Path(repo_root)
    repo_name = repo_path.name

    # Per-repo enforcement from config (or legacy fallback).
    enforcement, repo_alias = _resolve_enforcement(repo_root)
    enforcement_bool = enforcement == "block"

    # Normalize file path relative to repo root.
    try:
        abs_path = Path(file_path).resolve()
        if abs_path.is_relative_to(repo_path):
            rel_path = abs_path.relative_to(repo_path).as_posix()
        else:
            rel_path = file_path
    except Exception:
        rel_path = file_path

    # Current branch.
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    print(
        f"merge_train: checking conflicts for '{rel_path}' (branch '{current_branch}') in '{repo_alias}'...",
        file=sys.stderr,
    )

    # Detect remote OWNER/REPO for `gh --repo` scoping.
    repo_remote = ""
    try:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if remote_url:
            if remote_url.endswith(".git"):
                remote_url = remote_url[:-4]
            m = re.search(r"github\.com[:/]([^/]+/[^/]+)$", remote_url)
            if m:
                repo_remote = m.group(1)
    except Exception:
        pass

    # Read from cache (45s TTL).
    cache_file = Path(f"/tmp/merge_train_cache_{repo_name}.json")
    prs_data: dict = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            if time.time() - cache.get("timestamp", 0) < 45:
                prs_data = cache.get("prs", {})
        except Exception:
            pass

    if not prs_data:
        # Query open PRs via GitHub CLI.
        try:
            cmd = ["gh", "pr", "list", "--state", "open", "--json", "number,headRefName"]
            if repo_remote:
                cmd += ["--repo", repo_remote]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                prs = json.loads(proc.stdout)
                for pr in prs:
                    pr_num = pr.get("number")
                    pr_branch = pr.get("headRefName")
                    if pr_branch == current_branch:
                        continue
                    diff_cmd = ["gh", "pr", "diff", str(pr_num), "--name-only"]
                    if repo_remote:
                        diff_cmd += ["--repo", repo_remote]
                    diff_proc = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)
                    if diff_proc.returncode == 0:
                        files = [f.strip() for f in diff_proc.stdout.splitlines() if f.strip()]
                        prs_data[str(pr_num)] = {
                            "branch": pr_branch,
                            "files": files,
                            "symbols": {},
                        }
                cache_file.write_text(json.dumps({
                    "timestamp": time.time(),
                    "prs": prs_data,
                }))
        except Exception as e:
            print(
                f"merge_train: checked '{rel_path}' — conflict check skipped due to error: {e}",
                file=sys.stderr,
            )
            _emit(
                "allow",
                f"merge_train: {rel_path} — conflict check skipped (gh CLI error); allowing.",
            )
            return

    if not prs_data:
        print(
            f"merge_train: checked '{rel_path}' — no conflicts found (no other open PRs).",
            file=sys.stderr,
        )
        _emit(
            "allow",
            f"merge_train: {rel_path} — no conflicts found (no other open PRs in {repo_alias}).",
        )
        return

    # Identify symbols we are editing (for symbol-level locking).
    our_symbols: set = set()
    is_python = is_python_path(rel_path)
    whole_file_lock = True

    if is_python and start_line is not None and end_line is not None:
        try:
            start_l = int(start_line)
            end_l = int(end_line)
            file_on_disk = repo_path / rel_path
            if file_on_disk.exists():
                source = file_on_disk.read_text()
                symbols = extract_symbols(source)
                for sym in symbols:
                    if sym.overlaps(start_l, end_l):
                        our_symbols.add(sym.name)
                if our_symbols:
                    whole_file_lock = False
        except Exception:
            pass

    # Check conflicts against other open PRs.
    conflicts: list = []
    for pr_num, pr_info in prs_data.items():
        pr_files = pr_info.get("files", [])
        if rel_path not in pr_files:
            continue

        pr_branch = pr_info.get("branch", f"pr-{pr_num}")

        if whole_file_lock:
            conflicts.append((pr_num, pr_branch, f"whole-file '{rel_path}'"))
            continue

        pr_symbols = pr_info.get("symbols", {}).get(rel_path)
        if pr_symbols is None:
            try:
                sym_map = symbols_from_files_in_pr(int(pr_num), [rel_path], repo_remote or None)
                pr_symbols = list(sym_map.get(rel_path, []))
                pr_info.setdefault("symbols", {})[rel_path] = pr_symbols
                cache_file.write_text(json.dumps({
                    "timestamp": time.time(),
                    "prs": prs_data,
                }))
            except Exception:
                pr_symbols = []

        if not pr_symbols:
            conflicts.append((pr_num, pr_branch, f"whole-file '{rel_path}'"))
        else:
            overlap = our_symbols.intersection(pr_symbols)
            if overlap:
                conflicts.append((pr_num, pr_branch, f"symbols: {', '.join(overlap)}"))

    if conflicts:
        conflict_details = [
            f"PR#{pr_num} (branch '{branch}') is also modifying {detail}"
            for pr_num, branch, detail in conflicts
        ]
        msg = f"merge_train: Conflict detected in '{rel_path}'!\n  " + "\n  ".join(conflict_details)
        reason = f"merge_train: {rel_path} — conflict: " + "; ".join(
            f"PR#{pr_num}/{detail}" for pr_num, _, detail in conflicts
        )

        if enforcement_bool:
            # Block: return deny with the same reason the user sees in chat.
            print(msg, file=sys.stderr)
            _emit("deny", reason)
            return
        else:
            # Warn-only: still allow, but the user sees the conflict reason in chat.
            print(msg, file=sys.stderr)
            _emit("allow", f"{reason} (warn-only for {repo_alias}; check the other PR before merging).")
            return

    # No conflicts.
    print(
        f"merge_train: checked '{rel_path}' — no conflicts found.",
        file=sys.stderr,
    )
    _emit(
        "allow",
        f"merge_train: {rel_path} — no conflicts found in {repo_alias}.",
    )


if __name__ == "__main__":
    main()
