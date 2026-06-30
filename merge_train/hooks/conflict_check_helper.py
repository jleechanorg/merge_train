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
    # NOTE: _decision_payload is defined further down, so it is NOT available
    # here. Emit a hand-built allow envelope (same shape) to avoid a NameError
    # masking the real ImportError.
    _reason = "merge_train: package import failed; allowing"
    print(json.dumps({
        "decision": "approve",
        "reason": _reason,
        "systemMessage": _reason,
    }))
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
# Output helpers
# --------------------------------------------------------------------------- #

# Exit codes used for Claude Code TUI visibility:
#   0  → silent approve (tool runs, no notification)
#   1  → non-blocking warn (tool runs, TUI shows first line of stderr)
#   2  → block (tool prevented, stderr shown as reason)
_EXIT_SILENT_APPROVE = 0
_EXIT_WARN_NOTIFY = 1  # triggers "hook error" TUI notice — first stderr line shown

# Map internal decision names to Claude Code's canonical hook output values.
# Claude Code uses "approve" (not "allow") and "block" (not "deny").
# "allow"/"warn" both mean "let the tool proceed". "deny" is an old alias for "block".
# always_approve.sh (the reference implementation) outputs {"decision":"approve"}.
_DECISION_MAP: dict = {
    "allow": "approve",
    "warn": "approve",
    "deny": "block",
    "block": "block",
    "approve": "approve",
}

# Claude Code's hook schema caps ``systemMessage`` and ``stdout`` at
# 10,000 characters. When the reason text exceeds 10K (e.g., a 5+ PR
# conflict breakdown), Claude Code silently replaces it with a preview
# + "see file path" — the chat banner this whole feature exists to
# surface disappears. We pre-truncate at 8,000 chars (safe margin under
# the 10K cap) and append " (truncated)" so consumers can see the cut.
_REASON_HARD_CAP = 8_000


def _truncate_reason(reason: str) -> str:
    """Cap ``reason`` at :data:`_REASON_HARD_CAP` chars; append
    " (truncated)" if we cut anything. Identical truncation is
    applied to both chat-visible fields so they stay in sync."""
    if len(reason) <= _REASON_HARD_CAP:
        return reason
    return reason[:_REASON_HARD_CAP] + " (truncated)"


def _decision_payload(decision: str, reason: str) -> dict:
    """Build a PreToolUse hook output payload with a chat-visible reason.

    Output format uses the canonical Claude Code top-level fields:
      {"decision": "approve"|"block", "reason": "...", "systemMessage": "..."}
    The old ``hookSpecificOutput.permissionDecision`` wrapper is kept as a
    parallel field so codex/cursor/gemini runtimes that may still read it
    continue to work. Internal decision names ("allow", "warn", "deny") are
    mapped via :data:`_DECISION_MAP` to the canonical values before output.
    """
    safe_reason = _truncate_reason(reason)
    cc_decision = _DECISION_MAP.get(decision, "approve")
    return {
        "decision": cc_decision,
        "reason": safe_reason,
        "systemMessage": safe_reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": cc_decision,
            "permissionDecisionReason": safe_reason,
        },
    }


def _emit(decision: str, reason: str) -> None:
    """Print the decision JSON to stdout (Claude Code reads this)."""
    print(json.dumps(_decision_payload(decision, reason)))


def _silent_approve() -> None:
    """Emit a minimal approve with no systemMessage.

    Used for the no-conflict case so routine edits are completely silent.
    Claude Code shows nothing for exit 0 + ``{"decision":"approve"}`` with
    no ``systemMessage`` — the user is not spammed on every file write.
    """
    print(json.dumps({"decision": "approve"}))


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
# Tool-schema normalization (cross-runtime)
# --------------------------------------------------------------------------- #

# File-mutation tool names across every CLI fanout runtime we wire a hook into.
# Each runtime names its edit tools differently; the conflict check must
# recognize all of them or it silently no-ops ("not a file mutation; skipping").
#   Claude   : Edit, Write, MultiEdit, NotebookEdit  (tool_input.file_path)
#   Cursor   : Edit, Write                            (tool_input.file_path)
#   Gemini   : write_file, replace                    (tool_input.file_path)
#   Codex    : apply_patch                            (path(s) in patch body)
#   OpenCode : edit, write                            (surfaced by the plugin)
#   Windsurf : replace_file_content, multi_replace_file_content (legacy)
_MUTATION_TOOLS = frozenset({
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "write_file", "replace",
    "apply_patch",
    "edit", "write",
    "replace_file_content", "multi_replace_file_content",
})

# Codex's apply_patch embeds its target file(s) in the patch text under its
# `command` field, as `*** Update File: <path>` / `*** Add File: <path>` /
# `*** Delete File: <path>` / `*** Move to: <path>` lines. A single patch can
# touch multiple files, so every match counts.
_APPLY_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:(?:Update|Add|Delete)\s+File|Move\s+to):\s*(.+?)\s*$",
    re.MULTILINE,
)


def _extract_paths(tool_name: str, tool_input: dict, payload: dict) -> list:
    """Return the list of file paths a mutation tool will touch.

    Most runtimes expose a single ``file_path`` (Claude / Cursor / Gemini /
    OpenCode). Codex's ``apply_patch`` embeds one-or-more paths in the patch
    text under its ``command`` field, so it can touch several files at once.
    """
    if tool_name == "apply_patch":
        command = tool_input.get("command") or payload.get("command") or ""
        if isinstance(command, list):
            command = "\n".join(str(c) for c in command)
        seen: list = []
        for p in _APPLY_PATCH_FILE_RE.findall(command or ""):
            p = p.strip()
            if p and p not in seen:
                seen.append(p)
        return seen

    single = (
        tool_input.get("file_path")
        or tool_input.get("TargetFile")
        or tool_input.get("path")
        or payload.get("file_path")
        or payload.get("TargetFile")
        or ""
    )
    return [single] if single else []


def _normalize_rel(file_path: str, repo_path: Path) -> str:
    """Best-effort path relative to the repo root (posix); falls back to raw."""
    try:
        abs_path = Path(file_path).resolve()
        if abs_path.is_relative_to(repo_path):
            return abs_path.relative_to(repo_path).as_posix()
    except Exception:
        pass
    return file_path


def _collect_conflicts_for_path(
    rel_path: str,
    start_line,
    end_line,
    prs_data: dict,
    repo_path: Path,
    repo_remote: str,
    cache_file: Path,
) -> list:
    """Return conflict tuples ``(pr_num, pr_branch, detail)`` for one path.

    Performs symbol-level locking when line ranges are available and the file
    is Python; otherwise falls back to whole-file locking. ``detail`` embeds
    ``rel_path`` so callers can aggregate across multiple paths.
    """
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
                conflicts.append((pr_num, pr_branch, f"symbols in '{rel_path}': {', '.join(overlap)}"))

    return conflicts


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
    if tool_name not in _MUTATION_TOOLS:
        # Not a file-mutation tool — no conflict check needed. Exit 0 with no
        # stdout: Claude Code (and all other runtimes) treat no output as
        # implicit approve. Emitting a decision payload here triggered
        # "unsupported permissionDecision:allow" when tools like Bash fired
        # through a hook with a broad (*) matcher.
        print(
            f"merge_train: tool {tool_name!r} not a file mutation; skipping conflict check.",
            file=sys.stderr,
        )
        return

    # Extract target file path(s). Most runtimes give a single file_path;
    # codex's apply_patch can carry several paths in the patch body.
    tool_input = payload.get("input") or payload.get("tool_input") or {}
    raw_paths = _extract_paths(tool_name, tool_input, payload)
    if not raw_paths:
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

    # Normalize every target path relative to repo root.
    rel_paths = [_normalize_rel(fp, repo_path) for fp in raw_paths]
    paths_label = ", ".join(f"'{p}'" for p in rel_paths)

    # Current branch.
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    # Detect remote OWNER/REPO for `gh --repo` scoping.
    # NOTE: the regex below matches github.com remotes only. For non-
    # GitHub remotes (gitlab.example.com, self-hosted Gitea, etc.) the
    # pattern won't match and `repo_remote` stays empty — downstream
    # `gh pr list` / `gh pr diff` calls then run WITHOUT `--repo`, which
    # works only when the current CWD is inside the right local checkout
    # (so `gh` can infer the repo from the git remote it sees locally).
    # Cross-host self-hosted remotes without a local CWD match will fall
    # through to the error-handling branch below with a `gh CLI error`.
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

    # If the alias is still the raw directory name (fallback / unregistered
    # worktree at /tmp/ or similar), use the GitHub repo name from the remote
    # for a cleaner, recognizable label in conflict messages.
    if repo_remote and repo_alias == repo_name:
        repo_alias = repo_remote.split("/")[-1]

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
                f"merge_train: checked {paths_label} — conflict check skipped due to error: {e}",
                file=sys.stderr,
            )
            _emit(
                "allow",
                f"merge_train: {paths_label} — conflict check skipped (gh CLI error); allowing.",
            )
            return

    if not prs_data:
        print(
            f"merge_train: checked {paths_label} — no conflicts found (no other open PRs).",
            file=sys.stderr,
        )
        _silent_approve()
        return

    # Check every target path against other open PRs. Symbol-level line ranges
    # are only meaningful for a single-file edit (Claude/Cursor StartLine/
    # EndLine); a multi-file apply_patch falls back to whole-file locking.
    single_path = len(rel_paths) == 1
    conflicts: list = []
    for rel_path in rel_paths:
        sl = start_line if single_path else None
        el = end_line if single_path else None
        conflicts.extend(
            _collect_conflicts_for_path(
                rel_path, sl, el, prs_data, repo_path, repo_remote, cache_file
            )
        )

    if conflicts:
        conflict_details = [
            f"PR#{pr_num} (branch '{branch}') is also modifying {detail}"
            for pr_num, branch, detail in conflicts
        ]
        # Build the FIRST-LINE banner message — Claude Code shows the first
        # line of stderr as the "hook error" TUI notification. Make it short
        # and recognizable so the user actually sees "conflict" in the banner.
        first_line = f"merge_train: CONFLICT in {paths_label} ({len(conflicts)} PR{'' if len(conflicts)==1 else 's'}); first: PR#{conflicts[0][0]}/{conflicts[0][2]} — check before merging"
        full_msg = first_line + "\n  " + "\n  ".join(conflict_details)
        reason = f"merge_train: {paths_label} — conflict: " + "; ".join(
            f"PR#{pr_num}/{detail}" for pr_num, _, detail in conflicts
        )

        if enforcement_bool:
            # Block: deny — the tool is prevented. User sees reason in chat.
            # First line of stderr is the short banner; full details follow.
            print(full_msg, file=sys.stderr)
            _emit("deny", reason)
            return
        else:
            # Warn-only: tool runs, but exit 1 makes Claude Code surface the
            # FIRST line of stderr as a TUI notification ("hook error" banner).
            # That first line MUST be the short conflict banner above (not a
            # "checking conflicts..." status message) — otherwise the user sees
            # noise instead of the actual conflict.
            print(full_msg, file=sys.stderr)
            _emit("allow", f"{reason} (warn-only for {repo_alias}; check the other PR before merging).")
            sys.exit(_EXIT_WARN_NOTIFY)

    # No conflicts — silent approve. No systemMessage to avoid noise on every edit.
    print(
        f"merge_train: checked {paths_label} — no conflicts found.",
        file=sys.stderr,
    )
    _silent_approve()


if __name__ == "__main__":
    main()
