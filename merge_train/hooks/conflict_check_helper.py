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

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# Open-PR metadata cache TTL. Freshness for already-cached PRs is enforced by
# head SHA, not by this value, so it can be generous.
CACHE_TTL_SECONDS = 300
# Concurrent `gh pr diff` fetches. Serial fetches over ~100 open PRs took ~15s,
# which exceeded the Codex hook time budget and disabled the check entirely.
MAX_FETCH_WORKERS = 24
from pathlib import Path
from ast import literal_eval

# Add merge_train source locations to sys.path so we can import the
# package. The helper is normally called from ~/.local/bin/, where
# merge_train itself is NOT on sys.path.
for _p in (
    Path.home() / "merge_train",
    Path.home() / "projects_other" / "merge_train",
    Path.home() / "projects" / "merge_train",
    Path("/Users/jleechan/projects/merge_train"),
    # uv tool install location (Linux/macOS) — used when merge_train
    # was installed via `uv tool install` and the source-repo path
    # above is absent (e.g. on a fresh Linux box where the source
    # lives in ~/projects_other/merge_train, not the macOS path).
    Path.home() / ".local/share/uv/tools/merge-train/lib/python3.13/site-packages",
    Path.home() / ".local/share/uv/tools/merge-train/lib/python3.12/site-packages",
    Path.home() / ".local/share/uv/tools/merge-train/lib/python3.11/site-packages",
    Path.home() / ".local/share/uv/tools/merge-train/lib/python3.10/site-packages",
):
    if _p.exists():
        sys.path.insert(0, str(_p))

# Optional imports — fail-safe if the package isn't installed. main() reports
# the failure in the active runtime's output shape.
_IMPORT_FAILED = False
try:
    from merge_train.symbol_discovery import symbols_from_files_in_pr
    from merge_train.symbols import extract_symbols, is_python_path
except ImportError:  # pragma: no cover — fail-safe fallback
    _IMPORT_FAILED = True

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

# Exit codes used for hook behavior:
#   0  → success; tool continues unless the JSON explicitly denies
#   2  → block (tool prevented, stderr shown as reason)
_EXIT_SILENT_APPROVE = 0

# Map internal decision names to current PreToolUse hook output values.
# Codex rejects legacy top-level ``decision: "approve"``. For non-blocking
# cases, emit no permission decision at all; for blocking cases, use the
# hook-specific ``permissionDecision: "deny"`` shape accepted by both Codex and
# current Claude Code.
_DECISION_MAP: dict = {
    "allow": None,
    "warn": None,
    "approve": None,
    "deny": "deny",
    "block": "deny",
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


def _decision_payload(decision: str, reason: str, runtime: str = "claude") -> dict:
    """Build a PreToolUse hook output payload with a chat-visible reason.

    Non-blocking notices intentionally do not include a permission decision:
    current Codex rejects legacy ``decision: "approve"`` and treats it as a
    failed hook. Blocking notices use the hook-specific ``permissionDecision``
    shape accepted by Codex and Claude Code.
    """
    safe_reason = _truncate_reason(reason)
    permission_decision = _DECISION_MAP.get(decision)

    if runtime == "gemini":
        if permission_decision == "deny":
            return {"decision": "deny", "reason": safe_reason}
        return {"systemMessage": safe_reason}

    if runtime == "agy":
        # agy denies the tool unless stdout carries a decision; "ask" defers
        # to agy's normal permission flow.
        return {"decision": permission_decision or "ask", "reason": safe_reason}

    if runtime == "cursor":
        payload = {"permission": permission_decision or "allow"}
        if reason:
            payload["user_message"] = safe_reason
            payload["agent_message"] = safe_reason
        return payload

    payload = {"systemMessage": safe_reason}
    if permission_decision == "deny":
        payload["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": safe_reason,
        }
    else:
        payload["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "additionalContext": safe_reason,
        }
    return payload


def _emit(decision: str, reason: str, runtime: str = "claude") -> None:
    """Print the decision JSON to stdout (Claude Code reads this)."""
    print(json.dumps(_decision_payload(decision, reason, runtime)))


def _silent_approve(runtime: str = "claude") -> None:
    """Emit the runtime's quiet implicit-allow response.

    Claude and Codex use empty stdout. Gemini requires valid JSON and Cursor
    requires its permission envelope; neither response adds a user-visible
    status message.
    """
    if runtime == "gemini":
        print("{}")
    elif runtime == "agy":
        print(json.dumps({"decision": "ask"}))
    elif runtime == "cursor":
        print(json.dumps({"permission": "allow"}))


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
#   Claude   : Edit, Write, MultiEdit (tool_input.file_path),
#              NotebookEdit (tool_input.notebook_path)
#   Cursor   : Edit, Write                            (tool_input.file_path)
#   Gemini   : write_file, replace                    (tool_input.file_path)
#   Codex    : apply_patch                            (path(s) in patch body)
#   OpenCode : edit, write                            (surfaced by the plugin)
#   Agy      : write_to_file, replace_file_content,
#              multi_replace_file_content            (toolCall.args.TargetFile)
#   Windsurf : replace_file_content, multi_replace_file_content (legacy)
_MUTATION_TOOLS = frozenset({
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "StrReplace", "Delete", "EditNotebook",
    "write_file", "replace",
    "apply_patch",
    "edit", "write", "multiedit", "patch",
    "replace_file_content", "multi_replace_file_content", "write_to_file",
})

# Codex's apply_patch embeds its target file(s) in the patch text under its
# `command` field, as `*** Update File: <path>` / `*** Add File: <path>` /
# `*** Delete File: <path>` / `*** Move to: <path>` lines. A single patch can
# touch multiple files, so every match counts.
_APPLY_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:(?:Update|Add|Delete)\s+File|Move\s+to):\s*(.+?)\s*$",
    re.MULTILINE,
)


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the 1-based inclusive line span of the character range [start, end)."""
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, max(start, end - 1)) + 1
    return first, last


def _locate_anchor(text: str, anchor: str) -> tuple[int, int] | None:
    """Map an edit anchor to a line span, or ``None`` when it is not unique.

    A repeated or absent anchor cannot identify a symbol; returning ``None``
    keeps the caller on the whole-file fallback instead of guessing.
    """
    if not anchor:
        return None
    first = text.find(anchor)
    if first < 0 or text.find(anchor, first + 1) >= 0:
        return None
    return _line_span(text, first, first + len(anchor))


def _apply_patch_anchor(patch: str) -> str | None:
    """Recover the old-side text of a single-file patch as a searchable anchor.

    Codex writes hunks as a bare ``@@`` followed by context and ``-`` lines,
    with no line numbers, so the hunk body is the only way to locate the edit
    in the working tree.

    A patch with more than one hunk returns ``None``: a single contiguous range
    cannot describe disjoint hunks, and using only the first one would let a
    conflict in a later hunk resolve to an empty symbol overlap and suppress
    the warning entirely. Multi-hunk patches therefore keep the whole-file
    fallback.
    """
    hunks = [ln for ln in patch.splitlines() if ln.startswith("@@")]
    if len(hunks) > 1:
        return None

    body: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            continue
        if line.startswith("***"):
            continue
        if line.startswith((" ", "-")):
            body.append(line[1:])
        elif line.startswith("+"):
            continue
        elif body:
            body.append(line)
    return "\n".join(body) if body else None


def _patch_text(tool_name: str, tool_input: dict, payload: dict) -> str:
    """Return the patch body for a patch-style tool, or an empty string.

    Mirrors :func:`_extract_paths`: the text may arrive on the tool input or on
    the top-level payload, and may be a list of chunks.
    """
    if tool_name != "apply_patch":
        return ""
    raw = (
        tool_input.get("patch")
        or tool_input.get("command")
        or payload.get("patch")
        or payload.get("command")
        or ""
    )
    if isinstance(raw, list):
        raw = "\n".join(str(chunk) for chunk in raw)
    return str(raw)


def _derive_line_range(
    tool_name: str,
    tool_input: dict,
    payload: dict,
    repo_path: Path,
    rel_path: str,
) -> tuple[int | None, int | None]:
    """Derive the edited line range when the runtime does not send line numbers.

    Claude Code's ``Edit`` sends ``old_string`` and Codex's ``apply_patch``
    sends a patch body; neither carries ``StartLine``/``EndLine``. Without a
    derived range the symbol-level branch is unreachable and every warning
    degrades to whole-file.
    """
    # Symbol mode only applies to Python paths (see _collect_conflicts_for_path);
    # for anything else the range is unused, so skip the file read entirely.
    if not is_python_path(rel_path):
        return None, None

    target = repo_path / rel_path
    try:
        if not target.is_file():
            return None, None
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    anchor = tool_input.get("old_string") or tool_input.get("old_str")
    if not anchor:
        anchor = _apply_patch_anchor(_patch_text(tool_name, tool_input, payload))

    span = _locate_anchor(source, str(anchor)) if anchor else None
    return span if span else (None, None)


def _extract_paths(tool_name: str, tool_input: dict, payload: dict) -> list:
    """Return the list of file paths a mutation tool will touch.

    Most runtimes expose a single ``file_path`` (Claude / Cursor / Gemini /
    OpenCode). Codex's ``apply_patch`` embeds one-or-more paths in the patch
    text under ``patch`` (Codex) or ``command`` (OpenCode), so it can touch
    several files at once.
    """
    if tool_name == "apply_patch":
        command = (
            tool_input.get("patch")
            or tool_input.get("command")
            or payload.get("patch")
            or payload.get("command")
            or ""
        )
        if isinstance(command, list):
            command = "\n".join(str(c) for c in command)
        seen: list = []
        for p in _APPLY_PATCH_FILE_RE.findall(command or ""):
            p = p.strip()
            if p and p not in seen:
                seen.append(p)
        if seen:
            return seen

    single = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("TargetFile")
        or tool_input.get("path")
        or payload.get("file_path")
        or payload.get("TargetFile")
        or ""
    )
    return [single] if single else []


def _scan_literal_exec_command_workdirs(source: str) -> tuple[bool, bool, list[str]]:
    """Read static ``workdir`` literals from nested ``tools.exec_command`` calls.

    Some orchestration tools wrap a command call in source text and run their
    hooks from the outer session directory. Only explicit string literals on
    a recognized ``tools.exec_command({...})`` call are accepted; expressions
    and arbitrary command text are never evaluated.
    """
    def skip_space_and_comments(index: int) -> int:
        while index < len(source):
            if source[index].isspace():
                index += 1
            elif source.startswith("//", index):
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline + 1
            elif source.startswith("/*", index):
                close = source.find("*/", index + 2)
                index = len(source) if close < 0 else close + 2
            else:
                break
        return index

    def skip_quoted(index: int) -> int:
        quote = source[index]
        index += 1
        escaped = False
        while index < len(source):
            char = source[index]
            index += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                break
        return index

    def property_value_end(index: int) -> int:
        curly = bracket = paren = 0
        while index < len(source):
            char = source[index]
            if char in ("'", '"', "`"):
                index = skip_quoted(index)
                continue
            if source.startswith("//", index):
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline + 1
                continue
            if source.startswith("/*", index):
                close = source.find("*/", index + 2)
                index = len(source) if close < 0 else close + 2
                continue
            if char == "{":
                curly += 1
            elif char == "}":
                if curly == bracket == paren == 0:
                    break
                curly -= 1
            elif char == "[":
                bracket += 1
            elif char == "]":
                bracket -= 1
            elif char == "(":
                paren += 1
            elif char == ")":
                paren -= 1
            elif char == "," and curly == bracket == paren == 0:
                break
            index += 1
        return index

    def workdir_property(object_start: int) -> tuple[bool, bool, str | None]:
        index = object_start + 1
        found = False
        value: str | None = None
        while index < len(source):
            index = skip_space_and_comments(index)
            if index >= len(source) or source[index] == "}":
                return found, False, value
            key_start = index
            if source[index] in ("'", '"'):
                key_end = skip_quoted(index)
                try:
                    key = literal_eval(source[index:key_end])
                except (SyntaxError, ValueError):
                    key = None
                index = key_end
            else:
                while index < len(source) and (source[index].isalnum() or source[index] in "_$"):
                    index += 1
                key = source[key_start:index]
            index = skip_space_and_comments(index)
            if key in ("get", "set") and (
                source.startswith("workdir", index)
                and (index + len("workdir") == len(source)
                     or not (source[index + len("workdir")].isalnum()
                             or source[index + len("workdir")] in "_$"))
                or index < len(source) and source[index] == "["
            ):
                # Accessor syntax can compute the workdir dynamically.
                return True, True, None
            if index >= len(source) or source[index] != ":":
                # A bare workdir is JS shorthand, so its value is dynamic.
                # Computed keys and spreads can also supply/override workdir;
                # keep the whole nested context unresolved rather than falling
                # back to the outer hook cwd.
                if key == "workdir" or source.startswith("...", key_start) or source[key_start] == "[":
                    return True, True, None
                # Methods and malformed properties are not candidates.
                index = property_value_end(index)
                if index < len(source) and source[index] == ",":
                    index += 1
                    continue
                return found, False, value
            value_start = skip_space_and_comments(index + 1)
            value_end = property_value_end(value_start)
            if key == "workdir":
                if found:
                    return True, True, None
                found = True
                parsed_value = None
                if value_start < value_end and source[value_start] in ("'", '"'):
                    token_end = skip_quoted(value_start)
                    if skip_space_and_comments(token_end) == value_end:
                        try:
                            parsed_value = literal_eval(source[value_start:token_end])
                        except (SyntaxError, ValueError):
                            pass
                if isinstance(parsed_value, str) and parsed_value:
                    value = parsed_value
                else:
                    return True, True, None
            index = value_end
            if index < len(source) and source[index] == ",":
                index += 1
            elif index < len(source) and source[index] == "}":
                return found, False, value
        return found, False, value

    results: list[str | None] = []
    recognized_workdir = False
    unresolved_workdir = False
    index = 0
    while index < len(source):
        if source[index] in ("'", '"', "`"):
            if source[index] == "`":
                end = skip_quoted(index)
                template = source[index:end]
                if "${" in template and "tools.exec_command" in template:
                    # Nested calls inside template interpolations are source
                    # expressions, but parsing them safely requires a full JS
                    # parser. Preserve ambiguity instead of using outer cwd.
                    recognized_workdir = True
                    unresolved_workdir = True
                index = end
            else:
                index = skip_quoted(index)
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = len(source) if close < 0 else close + 2
            continue
        if source.startswith("tools.exec_command", index):
            before_ok = index == 0 or not (source[index - 1].isalnum() or source[index - 1] in "_$.")
            after = index + len("tools.exec_command")
            after_ok = after == len(source) or not (source[after].isalnum() or source[after] in "_$.")
            if before_ok and after_ok:
                call_paren = skip_space_and_comments(after)
                if call_paren < len(source) and source[call_paren] == "(":
                    object_start = skip_space_and_comments(call_paren + 1)
                    if object_start < len(source) and source[object_start] == "{":
                        found, ambiguous, value = workdir_property(object_start)
                        # A call with no workdir uses the invoking process's
                        # cwd. Keep it in the set of candidate bases so a
                        # sibling call with a different explicit workdir is
                        # recognized as ambiguous.
                        results.append(value if found else None)
                        recognized_workdir = True
                        if found:
                            unresolved_workdir = unresolved_workdir or ambiguous or value is None
                    else:
                        # The call may receive a named options object (or
                        # another expression) whose workdir cannot be
                        # inspected without evaluating source. Preserve that
                        # unresolved nested context instead of using the
                        # outer hook cwd.
                        recognized_workdir = True
                        unresolved_workdir = True
        index += 1
    return recognized_workdir, unresolved_workdir, results


def _nested_exec_sources(payload: dict, tool_input: dict) -> list[str]:
    """Return source fields that can represent executable orchestration.

    OpenCode's ``apply_patch`` puts the patch body in ``tool_input.command``.
    Its contents are mutation data, even when the added text contains a
    literal ``tools.exec_command`` call, so it must never choose the checkout
    whose conflicts are inspected.
    """
    tool_call = payload.get("toolCall") or {}
    tool_name = (
        tool_call.get("name") or payload.get("name") or payload.get("tool_name")
        or payload.get("tool") or ""
    ).lower()
    if tool_name == "apply_patch":
        return []
    return [
        source for source in (
            payload.get("code"), payload.get("command"),
            tool_input.get("code"), tool_input.get("command"),
        ) if isinstance(source, str)
    ]


def _repo_root_for_directory(directory: str) -> str | None:
    """Return a Git root for an explicit directory without changing cwd."""
    try:
        resolved = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return resolved or None


def _repo_root_for_mutation_path(path: str) -> str | None:
    """Resolve a Git root through the nearest existing target ancestor."""
    directory = Path(path).parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    return _repo_root_for_directory(str(directory))


def _resolve_repo_root(
    payload: dict,
    tool_input: dict,
    raw_paths: list[str],
    context_dirs: list[str] | None = None,
) -> str | None:
    """Resolve context only from explicit hook cwd/workdir or mutation paths.

    Every target must independently identify the same checkout. Nested exec
    workdirs identify the base for relative paths. Multiple explicit roots,
    unresolved paths, and targets outside that checkout are ambiguous; the hook
    never searches unrelated repositories.
    """
    if context_dirs is None:
        context_dirs = _mutation_context_directories(payload, tool_input)
    if not raw_paths:
        return _repo_root_for_directory(context_dirs[0]) if len(context_dirs) == 1 else None
    roots: set[str] = set()
    for path in raw_paths:
        if not isinstance(path, str):
            return None
        if Path(path).is_absolute():
            candidates = [Path(path)]
        else:
            if not context_dirs:
                return None
            candidates = [Path(directory) / path for directory in context_dirs]
        path_roots: set[str] = set()
        for candidate in candidates:
            try:
                resolved_path = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            root = _repo_root_for_mutation_path(str(resolved_path))
            if root:
                path_roots.add(root)
        # Every target must identify exactly one root. A path outside Git, an
        # unresolved path, or multiple candidate bases is ambiguous; do not
        # retain a different target's root as an accidental fallback.
        if len(path_roots) != 1:
            return None
        roots.update(path_roots)
    return roots.pop() if len(roots) == 1 else None


def _mutation_context_directories(payload: dict, tool_input: dict) -> list[str]:
    """Return the sole trustworthy base for resolving relative targets.

    Distinct bases are ambiguous even when they share a repository root.
    Return no context so callers fail open instead of checking an arbitrary
    same-named file selected by iteration order.
    """
    nested: list[str | None] = []
    nested_seen = nested_unresolved = False
    for source in _nested_exec_sources(payload, tool_input):
        found, unresolved, workdirs = _scan_literal_exec_command_workdirs(source)
        nested_seen = nested_seen or found
        nested_unresolved = nested_unresolved or unresolved
        nested.extend(workdirs)
    outer = [
        directory for directory in (tool_input.get("workdir"), tool_input.get("cwd"),
                                     payload.get("workdir"), payload.get("cwd"))
        if isinstance(directory, str) and directory
    ]
    if nested_seen and nested_unresolved:
        return []
    if nested_seen:
        candidates = []
        for directory in nested:
            if directory is not None:
                candidates.append(directory)
            else:
                # Nested calls without workdir inherit the payload/tool cwd.
                # If that context is absent, use the process cwd already
                # captured by this hook invocation.
                candidates.extend(outer or [str(Path.cwd())])
    else:
        candidates = outer
    if not candidates:
        candidates = [str(Path.cwd())]
    try:
        absolute_outer = [str(Path(directory).resolve()) for directory in outer
                          if Path(directory).is_absolute()]
        if any(not Path(directory).is_absolute() for directory in candidates):
            # Relative workdirs are meaningful only against one known outer
            # invocation directory. Never reinterpret them against hook cwd.
            if len(set(absolute_outer)) != 1:
                return []
            base = Path(absolute_outer[0])
            candidates = [str(Path(directory) if Path(directory).is_absolute()
                              else base / directory) for directory in candidates]
        unique = list(dict.fromkeys(str(Path(directory).resolve()) for directory in candidates))
    except (OSError, RuntimeError):
        return []
    return unique if len(unique) == 1 else []


def _trunk_conflict_files(repo_path: Path) -> list:
    """Files where HEAD cannot merge cleanly into the local ``origin/main`` ref.

    Uses the ``git merge-tree --write-tree`` exit code (1 == conflicts); output
    is never grepped for markers. Returns ``[]`` when clean or when the check
    cannot run (no ref, git < 2.38), so it can only add a warning.
    """
    try:
        proc = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only", "-z",
             "HEAD", "origin/main"],
            capture_output=True, text=True, check=False, cwd=repo_path, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 1:
        return []
    parts = proc.stdout.split("\0")
    first_block = parts[: parts.index("")] if "" in parts else parts
    return [p for p in first_block[1:] if p]


def _trunk_conflict_note(repo_path: Path, current_branch: str) -> str:
    """One-line warning when this branch already conflicts with origin/main."""
    if not current_branch or current_branch in ("main", "master"):
        return ""
    files = _trunk_conflict_files(repo_path)
    if not files:
        return ""
    shown = ", ".join(files[:10]) + (", ..." if len(files) > 10 else "")
    return (
        f"branch '{current_branch}' conflicts with origin/main in "
        f"{len(files)} file{'' if len(files) == 1 else 's'} ({shown}); "
        "merge or rebase main and re-plan before adding more."
    )


def _normalize_rel(
    file_path: str, repo_path: Path, context_dirs: list[str] | None = None
) -> str | None:
    """Resolve a target relative to the repo root; reject out-of-root paths."""
    try:
        candidates = [Path(file_path)]
        if not Path(file_path).is_absolute():
            candidates = [Path(directory) / file_path for directory in (context_dirs or [])]
        for candidate in candidates:
            abs_path = candidate.resolve()
            if abs_path.is_relative_to(repo_path):
                return abs_path.relative_to(repo_path).as_posix()
    except Exception:
        pass
    return None


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


def _main(runtime: str = "claude") -> None:
    if _IMPORT_FAILED:
        _emit("allow", "merge_train: package import failed; allowing", runtime)
        return
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            _emit("allow", "merge_train: empty payload; allowing.", runtime)
            return

        payload = json.loads(raw_input)
    except Exception:
        _emit("allow", "merge_train: payload parse failed; allowing.", runtime)
        return

    # Check tool name — only file-mutation tools get the conflict check.
    tool_call = payload.get("toolCall") or {}
    tool_name = (
        tool_call.get("name")
        or payload.get("name")
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
        if runtime == "agy":
            _silent_approve(runtime)
        return

    # Extract target file path(s). Most runtimes give a single file_path;
    # codex's apply_patch can carry several paths in the patch body.
    tool_input = (
        tool_call.get("args") or payload.get("input") or payload.get("tool_input") or {}
    )
    raw_paths = _extract_paths(tool_name, tool_input, payload)
    if not raw_paths:
        _emit("allow", "merge_train: no file_path in tool input; allowing.", runtime)
        return

    # Extract start and end lines (for symbol-level locking).
    start_line = tool_input.get("StartLine") or tool_input.get("startLine") or tool_input.get("StartLineNumber")
    end_line = tool_input.get("EndLine") or tool_input.get("endLine") or tool_input.get("EndLineNumber")

    # Resolve Git from the hook's explicit context. Orchestrators may run the
    # hook from their own non-repo root while the nested command names its
    # actual workdir in the payload.
    context_dirs = _mutation_context_directories(payload, tool_input)
    repo_root = _resolve_repo_root(payload, tool_input, raw_paths, context_dirs)
    if not repo_root:
        _emit("allow", "merge_train: not inside a git repo; allowing.", runtime)
        return

    repo_path = Path(repo_root)
    repo_name = repo_path.name

    # Per-repo enforcement from config (or legacy fallback).
    enforcement, repo_alias = _resolve_enforcement(repo_root)
    enforcement_bool = enforcement == "block"

    # Normalize every target path relative to repo root.
    rel_paths = [_normalize_rel(fp, repo_path, context_dirs) for fp in raw_paths]
    if any(path is None for path in rel_paths):
        _emit("allow", "merge_train: target path is ambiguous or outside the selected git repo; allowing.", runtime)
        return
    rel_paths = [path for path in rel_paths if path is not None]
    paths_label = ", ".join(f"'{p}'" for p in rel_paths)

    # Current branch.
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, check=False, cwd=repo_path,
    ).stdout.strip()

    trunk_note = _trunk_conflict_note(repo_path, current_branch)

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
            capture_output=True, text=True, check=False, cwd=repo_path,
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

    # Read from cache. The TTL only has to be short enough that a PR opened
    # seconds ago cannot cause a silent miss; the expensive part (one
    # `gh pr diff` per open PR, up to ~100 of them) is refreshed per PR by
    # head SHA below, so a longer TTL costs no freshness.
    cache_file = Path(f"/tmp/merge_train_cache_{repo_name}.json")
    cached_prs: dict = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            if time.time() - cache.get("timestamp", 0) < CACHE_TTL_SECONDS:
                cached_prs = cache.get("prs", {})
        except Exception:
            pass
    prs_data: dict = dict(cached_prs)
    # PR numbers whose `gh pr diff` failed. Those PRs cannot be checked, so a
    # clean result must not be reported as if the check were complete.
    unchecked: list = []

    # Always re-list open PRs (one fast call) so newly opened PRs are seen;
    # reuse the cached file list for any PR whose head SHA is unchanged.
    try:
        cmd = [
            "gh", "pr", "list", "--state", "open",
            "--json", "number,headRefName,headRefOid", "--limit", "500",
        ]
        if repo_remote:
            cmd += ["--repo", repo_remote]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=repo_path)
        if proc.returncode != 0:
            _emit(
                "allow",
                f"merge_train: {paths_label} — conflict check skipped (gh PR list failed); allowing.",
                runtime,
            )
            return
        prs = json.loads(proc.stdout)
        if proc.returncode == 0:
            reuse: dict = {}
            pending: list = []
            for pr in prs:
                pr_num = pr.get("number")
                pr_branch = pr.get("headRefName")
                pr_oid = pr.get("headRefOid") or ""
                if pr_branch == current_branch:
                    prs_data.pop(str(pr_num), None)
                    continue
                known = cached_prs.get(str(pr_num))
                if known and known.get("oid") == pr_oid and pr_oid:
                    reuse[str(pr_num)] = {
                        "branch": pr_branch,
                        "oid": pr_oid,
                        "files": known.get("files", []),
                        "symbols": known.get("symbols", {}),
                    }
                else:
                    pending.append((str(pr_num), pr_branch, pr_oid))

            # Fetch missing diffs concurrently: with ~100 open PRs the serial
            # loop alone cost ~15s and pushed every Codex apply_patch past its
            # hook time budget, so the check never actually ran.
            def _fetch(entry):
                num, branch, oid = entry
                diff_cmd = ["gh", "pr", "diff", num, "--name-only"]
                if repo_remote:
                    diff_cmd += ["--repo", repo_remote]
                diff_proc = subprocess.run(
                    diff_cmd, capture_output=True, text=True, check=False, cwd=repo_path
                )
                if diff_proc.returncode != 0 and repo_remote:
                    # `gh pr diff` refuses very large PRs; the paginated files
                    # API has no such cap (up to 3000 files).
                    diff_proc = subprocess.run(
                        ["gh", "api", f"repos/{repo_remote}/pulls/{num}/files",
                         "--paginate", "--jq", ".[].filename"],
                        capture_output=True, text=True, check=False, cwd=repo_path,
                    )
                if diff_proc.returncode != 0:
                    return (num, None)
                files = [f.strip() for f in diff_proc.stdout.splitlines() if f.strip()]
                return (num, {"branch": branch, "oid": oid, "files": files, "symbols": {}})

            fetched: dict = {}
            if pending:
                with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as pool:
                    for num, pr_record in pool.map(_fetch, pending):
                        if pr_record:
                            fetched[num] = pr_record
                        else:
                            unchecked.append(num)
            prs_data = dict(fetched)
            prs_data.update(reuse)
            try:
                cache_file.write_text(json.dumps({
                    "timestamp": time.time(),
                    "prs": prs_data,
                }))
            except Exception:
                pass
    except Exception as e:
        print(
            f"merge_train: checked {paths_label} — conflict check skipped due to error: {e}",
            file=sys.stderr,
        )
        _emit(
            "allow",
            f"merge_train: {paths_label} — conflict check skipped (gh CLI error); allowing.",
            runtime,
        )
        return

    if not prs_data and not unchecked:
        if trunk_note:
            _emit("allow", f"merge_train: {trunk_note}", runtime)
        else:
            _silent_approve(runtime)
        return

    # Check every target path against other open PRs. Symbol-level line ranges
    # are only meaningful for a single-file edit: a multi-file apply_patch has
    # no single span, so it falls back to whole-file locking. Runtimes that send
    # StartLine/EndLine are used directly; for Edit (old_string) and Codex
    # apply_patch (hunk body) the range is derived from the payload anchor.
    single_path = len(rel_paths) == 1
    conflicts: list = []
    for rel_path in rel_paths:
        sl = start_line if single_path else None
        el = end_line if single_path else None
        if single_path and (sl is None or el is None):
            sl, el = _derive_line_range(tool_name, tool_input, payload, repo_path, rel_path)
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
        if trunk_note:
            full_msg += "\n  " + trunk_note
            reason += "; " + trunk_note

        if enforcement_bool:
            # Block: deny — the tool is prevented. User sees reason in chat.
            # First line of stderr is the short banner; full details follow.
            print(full_msg, file=sys.stderr)
            _emit("deny", reason, runtime)
            return
        else:
            # Warn-only: tool runs. ``systemMessage`` surfaces the warning
            # without making Codex treat the hook itself as failed.
            # That first line of stderr MUST be the short conflict banner
            # (not a generic status message) so that Claude Code surfaces it
            # cleanly in the TUI when exit status / events are logged.
            print(full_msg, file=sys.stderr)
            _emit(
                "allow",
                f"{reason} (warn-only for {repo_alias}; check the other PR before merging).",
                runtime,
            )
            return

    # No conflicts, but a failed `gh pr diff` means some open PR could not be
    # inspected. Report the check as incomplete instead of emitting the same
    # silence a fully clean check produces. Still fail-open: the edit runs.
    if unchecked:
        unread = ", ".join(f"#{num}" for num in unchecked)
        _emit(
            "allow",
            f"merge_train: {paths_label} — conflict check incomplete; "
            f"{len(unchecked)} open PR{'' if len(unchecked) == 1 else 's'} could not be "
            f"diffed ({unread}); allowing. Re-run after GitHub recovers."
            + (f" {trunk_note}" if trunk_note else ""),
            runtime,
        )
        return

    if trunk_note:
        _emit("allow", f"merge_train: {trunk_note}", runtime)
        return

    # No conflicts — silent approve. No systemMessage to avoid noise on every edit.
    _silent_approve(runtime)


def main(runtime: str = "claude") -> None:
    """Run the hook fail-open if an unexpected payload shape breaks parsing."""
    try:
        _main(runtime)
    except Exception as exc:
        print(f"merge_train: conflict helper failed; allowing: {exc}", file=sys.stderr)
        _emit("allow", "merge_train: conflict helper failed; allowing.", runtime)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--runtime",
        choices=("claude", "codex", "gemini", "cursor", "agy", "opencode"),
        default="claude",
    )
    args, _ = parser.parse_known_args()
    main(args.runtime)
