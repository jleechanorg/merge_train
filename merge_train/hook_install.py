"""Per-agent hook installer + synthetic-Edit test harness.

This module is the Python entry point for the Phase C roadmap goal:
``install-hooks --agent {claude,opencode,codex}`` and
``test-hooks --agent {claude,opencode,codex,all}``. It replaces the
implicit ``install.sh`` hook wiring with an idempotent, callable API
that:

- Copies hook shell scripts to ``$HOME/.local/bin/`` (the canonical
  install location, matching ``install.sh``'s pattern).
- Patches ``~/.claude/settings.json`` (Claude Code) PreToolUse matchers
  for Edit + Write to invoke ``conflict-warn-pre-tool.sh``.
- Patches ``~/.codex/hooks.json`` (Codex CLI) PreToolUse Edit matcher
  to invoke ``predict-spawn-check.sh``.
- Writes a ``.opencode.json`` instruction block to the target repo
  telling OpenCode agents to run ``predict-conflicts`` before editing.

After PR #18, all hooks are **warn-only** — no hook wired by this
module blocks the underlying tool. Idempotency is enforced by
inspecting existing config and either skipping or in-place updating.

The test harness (``test_hooks_for_agent``) installs a hook, then
synthetically feeds it a PreToolUse Edit payload and asserts that the
process exits 0 with no denial decision. It is **not** a real Claude/
Codex/OpenCode round-trip — it just exercises the hook binaries the
installer wired up.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #

AGENT_CHOICES: tuple[str, ...] = ("claude", "opencode", "codex", "agy", "all")
"""Agent selectors accepted by the install-hooks / test-hooks CLIs.

``agy`` is the user-scope installer for the Antigravity / Gemini CLI
(``~/.local/bin/agy``). It writes the per-edit conflict-warn hook to
``~/.gemini/config/hooks.json`` under the ``BeforeTool`` event.
"""

#: Hook shell scripts shipped in this repo that get installed to ~/.local/bin.
ALL_HOOK_SCRIPTS: tuple[str, ...] = (
    "predict-spawn-check.sh",
    "conflict-warn-pre-tool.sh",
    "gemini-conflict-warn.sh",
    "conflict_check_helper.py",
)

#: Canonical install dir for hook scripts. ``$HOME/.local/bin`` is on
#: PATH for most users and matches the ``install.sh`` pattern. The
#: paths below are resolved lazily via helper functions so tests can
#: ``monkeypatch.setattr(Path, "home", ...)`` before resolution.
HOOKS_INSTALL_DIR_NAME: str = ".local/bin"
CLAUDE_SETTINGS_REL: str = ".claude/settings.json"
CODEX_HOOKS_REL: str = ".codex/hooks.json"
AGY_HOOKS_REL: str = ".gemini/config/hooks.json"


def hooks_install_dir() -> Path:
    """Return ``$HOME/.local/bin`` (lazy, respects Path.home patches)."""
    return Path.home() / HOOKS_INSTALL_DIR_NAME


def claude_settings_path() -> Path:
    """Return ``$HOME/.claude/settings.json`` (lazy)."""
    return Path.home() / CLAUDE_SETTINGS_REL


def codex_hooks_path() -> Path:
    """Return ``$HOME/.codex/hooks.json`` (lazy)."""
    return Path.home() / CODEX_HOOKS_REL


def agy_hooks_path() -> Path:
    """Return ``$HOME/.gemini/config/hooks.json`` (lazy).

    This is the user-scope Gemini/Antigravity hook config. The
    project-scope equivalent lives at ``<repo>/.gemini/settings.json``
    and is intentionally NOT touched by this installer — merge_train
    only manages the user-scope config so the hook fires across every
    repo the user opens in agy.
    """
    return Path.home() / AGY_HOOKS_REL


# Back-compat aliases — older code/tests may import these as Path-like
# names. Each is a function that returns a fresh Path, so test-time
# ``monkeypatch.setattr(Path, "home", ...)`` is honored.
HOOKS_INSTALL_DIR = hooks_install_dir
CLAUDE_SETTINGS_PATH = claude_settings_path
CODEX_HOOKS_PATH = codex_hooks_path
AGY_HOOKS_PATH = agy_hooks_path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _find_hooks_dir(base_dir: Optional[Path] = None) -> Path:
    """Locate the hooks source directory containing the hook scripts.

    Checks:
    1. A ``hooks`` directory inside the ``merge_train`` package (installed layout).
    2. A ``hooks`` directory beside the ``merge_train`` package (editable/source layout).
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent

    # Installed package layout
    pkg_hooks = base_dir / "hooks"
    if pkg_hooks.is_dir():
        return pkg_hooks

    # Source repo layout
    repo_hooks = base_dir.parent / "hooks"
    if repo_hooks.is_dir():
        return repo_hooks

    raise FileNotFoundError("merge_train hooks directory not found")


def _repo_root() -> Path:
    """Locate the merge_train source repo root.

    We walk up from this file's parent (``merge_train/``) to the
    directory that contains ``hooks/`` (or check for pyproject.toml as root marker).
    """
    here = Path(__file__).resolve().parent
    # If in dev/editable source repo layout, pyproject.toml is in the root
    if (here.parent / "pyproject.toml").is_file():
        return here.parent
    # Check old hooks structure beside merge_train folder
    candidate = here.parent / "hooks"
    if candidate.is_dir():
        return candidate.parent
    raise FileNotFoundError("merge_train source repo not found")


def _install_hook_scripts() -> list[Path]:
    """Copy hook shell scripts to ~/.local/bin.

    Idempotent — overwrites if present. Returns the list of installed
    script paths.
    """
    src_dir = _find_hooks_dir()
    HOOKS_INSTALL_DIR().mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []

    # Fail fast if any required script in ALL_HOOK_SCRIPTS is missing
    for name in ALL_HOOK_SCRIPTS:
        src = src_dir / name
        if not src.is_file():
            raise FileNotFoundError(f"hook source file missing: {src}")

    for name in ALL_HOOK_SCRIPTS:
        src = src_dir / name
        dst = HOOKS_INSTALL_DIR() / name
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        installed.append(dst)
    return installed


def _is_stale_source_cmd(cmd: str, src_root: str) -> bool:
    """Return True if *cmd* points into the old source-repo path layout."""
    return bool(src_root) and src_root in cmd and "hooks/" in cmd


def _strip_stale_source_entries(hooks: dict, src_root: str) -> None:
    """Remove any hook entries that still point at the source-repo path.

    Mirrors the cleanup already done in ``install.sh`` step 3d.
    """
    for event_hooks in hooks.values():
        for entry in event_hooks:
            orig = entry.get("hooks", [])
            entry["hooks"] = [
                h
                for h in orig
                if not _is_stale_source_cmd(h.get("command", ""), src_root)
            ]


# --------------------------------------------------------------------------- #
# Per-agent installers
# --------------------------------------------------------------------------- #


def _install_claude(target: Path) -> dict:
    """Patch ``~/.claude/settings.json`` PreToolUse matchers for Edit+Write.

    Hook: ``bash ~/.local/bin/conflict-warn-pre-tool.sh`` (warn-only).
    Idempotent: re-running does not append a duplicate entry.
    """
    try:
        _install_hook_scripts()
        try:
            src_root = str(_repo_root())
        except FileNotFoundError:
            src_root = ""
        cmd = f"bash {HOOKS_INSTALL_DIR() / 'conflict-warn-pre-tool.sh'}"

        settings_path = CLAUDE_SETTINGS_PATH()
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text())
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

        data.setdefault("hooks", {})
        pre_tool = data["hooks"].setdefault("PreToolUse", [])

        # Remove any stale entries that still reference the old source-repo path.
        _strip_stale_source_entries({"PreToolUse": pre_tool}, src_root)
        pre_tool = data["hooks"]["PreToolUse"]

        for matcher in ("Edit", "Write"):
            match_entry = next(
                (m for m in pre_tool if m.get("matcher") == matcher), None
            )
            if match_entry is None:
                match_entry = {"matcher": matcher, "hooks": []}
                pre_tool.append(match_entry)
            if not any(h.get("command") == cmd for h in match_entry.get("hooks", [])):
                match_entry.setdefault("hooks", []).append(
                    {
                        "type": "command",
                        "command": cmd,
                        "timeout": 15000,
                    }
                )

        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2))
        return {
            "agent": "claude",
            "installed": True,
            "settings_path": str(settings_path),
            "command": cmd,
        }
    except Exception as exc:
        return {
            "agent": "claude",
            "installed": False,
            "error": str(exc),
        }


def _install_codex(target: Path) -> dict:
    """Patch ``~/.codex/hooks.json`` PreToolUse Edit matcher.

    Hook: ``bash ~/.local/bin/conflict-warn-pre-tool.sh`` (warn-only).
    Mirrors the Claude per-edit symbol-level conflict check, so Codex
    users get the same chat-visible ``permissionDecisionReason`` on
    every Edit. The orchestrator-mode ``predict-spawn-check.sh`` is a
    separate, opt-in path for AO spawn-time checks (it needs the
    ``MERGE_TRAIN_FILES`` env var and is not wired here).
    """
    try:
        _install_hook_scripts()
        try:
            src_root = str(_repo_root())
        except FileNotFoundError:
            src_root = ""
        cmd = f"bash {HOOKS_INSTALL_DIR() / 'conflict-warn-pre-tool.sh'}"

        hooks_path = CODEX_HOOKS_PATH()
        if hooks_path.exists():
            try:
                data = json.loads(hooks_path.read_text())
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

        data.setdefault("hooks", {})
        pre_tool = data["hooks"].setdefault("PreToolUse", [])

        # Remove any stale entries that still reference the old source-repo path.
        _strip_stale_source_entries({"PreToolUse": pre_tool}, src_root)
        # And any prior install that wired predict-spawn-check.sh into the
        # Edit matcher — that script needs MERGE_TRAIN_FILES, so it never
        # fires on normal Edits and would just mask the per-edit one.
        for matcher in pre_tool:
            if matcher.get("matcher") == "Edit":
                matcher["hooks"] = [
                    h
                    for h in matcher.get("hooks", [])
                    if "predict-spawn-check" not in h.get("command", "")
                ]
        pre_tool = data["hooks"]["PreToolUse"]

        edit_entry = next((m for m in pre_tool if m.get("matcher") == "Edit"), None)
        if edit_entry is None:
            edit_entry = {"matcher": "Edit", "hooks": []}
            pre_tool.append(edit_entry)
        if not any(h.get("command") == cmd for h in edit_entry.get("hooks", [])):
            edit_entry.setdefault("hooks", []).append(
                {
                    "type": "command",
                    "command": cmd,
                    "timeoutSec": 15,
                    "statusMessage": "merge_train: checking conflicts...",
                }
            )

        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(data, indent=2))
        return {
            "agent": "codex",
            "installed": True,
            "hooks_path": str(hooks_path),
            "command": cmd,
        }
    except Exception as exc:
        return {
            "agent": "codex",
            "installed": False,
            "error": str(exc),
        }


def _install_opencode(target: Path) -> dict:
    """Write predict-conflicts instructions to ``<target>/.opencode.json``.

    OpenCode does not have a per-hook binary in the same way Claude/Codex
    do. Instead, we drop a declarative instruction block into the repo's
    ``.opencode.json`` telling agents to run ``predict-conflicts`` before
    editing. Mirrors what ``install.sh`` step 3c does.
    """
    try:
        _install_hook_scripts()
        cfg_path = target / ".opencode.json"
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text())
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

        data.setdefault("$schema", "https://opencode.ai/config.json")
        existing = data.get("instructions") or ""
        if "predict-conflicts" not in existing:
            block = (
                "IMPORTANT: Before starting any coding task, run: "
                "predict-conflicts --from-prs <PR_NUMBER> to check for "
                "symbol-level conflicts with other open PRs. If conflicts "
                "are detected, pick a different task or coordinate with the "
                "conflicting PR's author."
            )
            if existing and not existing.endswith("\n"):
                existing = existing + "\n\n"
            data["instructions"] = (existing + block) if existing else block

        cfg_path.write_text(json.dumps(data, indent=2))
        return {
            "agent": "opencode",
            "installed": True,
            "config_path": str(cfg_path),
        }
    except Exception as exc:
        return {
            "agent": "opencode",
            "installed": False,
            "error": str(exc),
        }


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def _install_agy(target: Path) -> dict:
    """Patch ``~/.gemini/config/hooks.json`` ``BeforeTool`` event.

    Hook: ``bash ~/.local/bin/conflict-warn-pre-tool.sh`` (warn-only).

    The Gemini / Antigravity hook schema is intentionally simpler than
    Codex's: a single ``BeforeTool`` event with no per-tool matcher (it
    fires on every tool call). Filtering to Edit / Write happens
    inside ``conflict_check_helper.py`` itself — non-file-mutation
    tool calls short-circuit to a no-op allow.

    The per-edit wiring (rather than the project-scope session guard
    ``gemini-conflict-warn.sh``) gives the user real symbol-level
    conflict detection on every Edit/Write, matching Claude Code UX.
    Idempotent: re-running does not append a duplicate entry.
    """
    try:
        _install_hook_scripts()
        try:
            src_root = str(_repo_root())
        except FileNotFoundError:
            src_root = ""
        cmd = f"bash {HOOKS_INSTALL_DIR() / 'conflict-warn-pre-tool.sh'}"

        hooks_path = AGY_HOOKS_PATH()
        if hooks_path.exists():
            try:
                data = json.loads(hooks_path.read_text())
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

        data.setdefault("hooks", {})
        before_tool = data["hooks"].setdefault("BeforeTool", [])

        # Remove any stale entries that still reference the old source-repo path.
        _strip_stale_source_entries({"BeforeTool": before_tool}, src_root)
        before_tool = data["hooks"]["BeforeTool"]

        # Agy / Gemini schema mirrors Codex: ``BeforeTool[]`` is a list of
        # wrapper objects, each carrying a nested ``hooks[]`` list. The
        # project-scope ``.gemini/settings.json`` uses the same shape
        # (no per-tool matcher — the script itself filters Edit/Write).
        # If a wrapper already exists with our command, skip appending.
        def _has_cmd(wrapper: dict) -> bool:
            return any(h.get("command") == cmd for h in wrapper.get("hooks", []))

        if not any(_has_cmd(w) for w in before_tool):
            before_tool.append({"hooks": [{"type": "command", "command": cmd}]})

        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(data, indent=2))
        return {
            "agent": "agy",
            "installed": True,
            "hooks_path": str(hooks_path),
            "command": cmd,
        }
    except Exception as exc:
        return {
            "agent": "agy",
            "installed": False,
            "error": str(exc),
        }


def install_hooks_for_agent(agent: str, target: Optional[Path] = None) -> list | dict:
    """Install hooks for *agent*. Returns a result dict or list of dicts.

    Args:
        agent: One of ``"claude"``, ``"opencode"``, ``"codex"``, ``"agy"``,
            or ``"all"``.
        target: For ``"opencode"``, the repo root whose ``.opencode.json``
            gets written. Defaults to the current working directory.

    Raises:
        ValueError: if *agent* is not a known selector.
    """
    if agent not in AGENT_CHOICES:
        raise ValueError(
            f"unknown agent {agent!r}; expected one of {list(AGENT_CHOICES)}"
        )

    if target is None:
        target = Path.cwd()
    target = Path(target).resolve()

    installers: dict[str, Callable[[Path], dict]] = {
        "claude": _install_claude,
        "opencode": _install_opencode,
        "codex": _install_codex,
        "agy": _install_agy,
    }

    if agent == "all":
        return [installers[a](target) for a in ("claude", "opencode", "codex", "agy")]
    return installers[agent](target)


# --------------------------------------------------------------------------- #
# Test harness — synthetic Edit event
# --------------------------------------------------------------------------- #


def _run_hook_binary(bin_path: Path, payload: Optional[dict] = None) -> dict:
    """Run a hook shell script with a JSON payload on stdin.

    Returns ``{"exit_code", "stdout", "stderr"}``. Caller decides
    whether exit-code-0 + no-permissionDecision-deny = "ok".
    """
    payload_s = json.dumps(payload or {})
    try:
        proc = subprocess.run(
            ["bash", str(bin_path)],
            input=payload_s,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "timeout"}
    except FileNotFoundError as exc:
        return {"exit_code": -1, "stdout": "", "stderr": f"missing: {exc}"}


def _test_claude(target: Path) -> dict:
    """Synthesize a PreToolUse Edit payload; assert hook exits 0 + allows."""
    settings_path = CLAUDE_SETTINGS_PATH()
    if not settings_path.exists():
        return {
            "agent": "claude",
            "ok": False,
            "exit_code": -1,
            "reason": "Claude settings.json not installed",
        }
    try:
        data = json.loads(settings_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "agent": "claude",
            "ok": False,
            "exit_code": -1,
            "reason": f"Claude settings.json malformed: {exc}",
        }
    pre_tool = data.get("hooks", {}).get("PreToolUse", [])
    edit_matchers = [m for m in pre_tool if m.get("matcher") == "Edit"]
    if not edit_matchers or not any(
        "conflict-warn-pre-tool" in h.get("command", "")
        for m in edit_matchers
        for h in m.get("hooks", [])
    ):
        return {
            "agent": "claude",
            "ok": False,
            "exit_code": -1,
            "reason": "conflict-warn-pre-tool hook not wired into Edit matcher",
        }
    bin_path = HOOKS_INSTALL_DIR() / "conflict-warn-pre-tool.sh"
    if not bin_path.is_file():
        return {
            "agent": "claude",
            "ok": False,
            "exit_code": -1,
            "reason": f"hook script missing: {bin_path}",
        }
    payload = {
        "session_id": "synthetic",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/example.py"},
    }
    res = _run_hook_binary(bin_path, payload)
    ok = res["exit_code"] == 0
    # Warn-only: stdout/stderr may contain warnings; that's expected.
    return {
        "agent": "claude",
        "ok": ok,
        "exit_code": res["exit_code"],
        "hook": str(bin_path),
        "stderr": res["stderr"][:500],
    }


def _test_codex(target: Path) -> dict:
    """Synthesize a Codex PreToolUse Edit payload; assert hook exits 0.

    Mirrors ``_test_claude`` / ``_test_agy``: the per-edit
    ``conflict-warn-pre-tool.sh`` script fires on every Edit and emits
    a chat-visible ``permissionDecisionReason``.
    """
    hooks_path = CODEX_HOOKS_PATH()
    if not hooks_path.exists():
        return {
            "agent": "codex",
            "ok": False,
            "exit_code": -1,
            "reason": "Codex hooks.json not installed",
        }
    try:
        data = json.loads(hooks_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "agent": "codex",
            "ok": False,
            "exit_code": -1,
            "reason": f"Codex hooks.json malformed: {exc}",
        }
    pre_tool = data.get("hooks", {}).get("PreToolUse", [])
    edit_matchers = [m for m in pre_tool if m.get("matcher") == "Edit"]
    if not edit_matchers or not any(
        "conflict-warn-pre-tool" in h.get("command", "")
        for m in edit_matchers
        for h in m.get("hooks", [])
    ):
        return {
            "agent": "codex",
            "ok": False,
            "exit_code": -1,
            "reason": "conflict-warn-pre-tool hook not wired into Edit matcher",
        }
    bin_path = HOOKS_INSTALL_DIR() / "conflict-warn-pre-tool.sh"
    if not bin_path.is_file():
        return {
            "agent": "codex",
            "ok": False,
            "exit_code": -1,
            "reason": f"hook script missing: {bin_path}",
        }
    # The script's own tool-name filter handles the dispatch; we send
    # the Claude-style payload (tool_name + tool_input) and it ignores
    # non-Edit/Write tool names internally.
    res = _run_hook_binary(
        bin_path,
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/example.py", "new_string": "x = 1"},
        },
    )
    ok = res["exit_code"] == 0
    return {
        "agent": "codex",
        "ok": ok,
        "exit_code": res["exit_code"],
        "hook": str(bin_path),
        "stderr": res["stderr"][:500],
    }


def _test_opencode(target: Path) -> dict:
    """OpenCode uses declarative instructions; verify .opencode.json is present."""
    cfg = target / ".opencode.json"
    if not cfg.is_file():
        return {
            "agent": "opencode",
            "ok": False,
            "exit_code": -1,
            "reason": f".opencode.json missing at {cfg}",
        }
    try:
        data = json.loads(cfg.read_text())
    except json.JSONDecodeError as exc:
        return {
            "agent": "opencode",
            "ok": False,
            "exit_code": -1,
            "reason": f".opencode.json malformed: {exc}",
        }
    if "predict-conflicts" not in (data.get("instructions") or ""):
        return {
            "agent": "opencode",
            "ok": False,
            "exit_code": -1,
            "reason": ".opencode.json instructions missing predict-conflicts",
        }
    return {
        "agent": "opencode",
        "ok": True,
        "exit_code": 0,
        "config_path": str(cfg),
    }


def _test_agy(target: Path) -> dict:
    """Synthesize a BeforeTool Edit payload; assert hook exits 0 + allows.

    Mirrors ``_test_claude`` but for the agy / Gemini schema: a single
    ``BeforeTool`` event (no matcher) wired to ``conflict-warn-pre-tool.sh``.
    """
    hooks_path = AGY_HOOKS_PATH()
    if not hooks_path.exists():
        return {
            "agent": "agy",
            "ok": False,
            "exit_code": -1,
            "reason": "agy hooks.json not installed",
        }
    try:
        data = json.loads(hooks_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "agent": "agy",
            "ok": False,
            "exit_code": -1,
            "reason": f"agy hooks.json malformed: {exc}",
        }
    before_tool = data.get("hooks", {}).get("BeforeTool", [])
    if not any(
        "conflict-warn-pre-tool" in h.get("command", "")
        for wrapper in before_tool
        for h in wrapper.get("hooks", [])
    ):
        return {
            "agent": "agy",
            "ok": False,
            "exit_code": -1,
            "reason": "conflict-warn-pre-tool hook not wired into BeforeTool event",
        }
    bin_path = HOOKS_INSTALL_DIR() / "conflict-warn-pre-tool.sh"
    if not bin_path.is_file():
        return {
            "agent": "agy",
            "ok": False,
            "exit_code": -1,
            "reason": f"hook script missing: {bin_path}",
        }
    # The agy BeforeTool payload uses the Claude-style nested shape
    # (the script's own tool-name filter handles the dispatch).
    payload = {
        "session_id": "synthetic",
        "hook_event_name": "BeforeTool",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/example.py"},
    }
    res = _run_hook_binary(bin_path, payload)
    ok = res["exit_code"] == 0
    return {
        "agent": "agy",
        "ok": ok,
        "exit_code": res["exit_code"],
        "hook": str(bin_path),
        "stderr": res["stderr"][:500],
    }


TEST_HOOKS: dict[str, Callable[[Path], dict]] = {
    "claude": _test_claude,
    "codex": _test_codex,
    "opencode": _test_opencode,
    "agy": _test_agy,
}
"""Per-agent test hook runners. Each takes the install target and returns a result dict."""


def test_hooks_for_agent(agent: str, target: Optional[Path] = None) -> list | dict:
    """Run the synthetic-Edit test for *agent*.

    Args:
        agent: One of ``"claude"``, ``"opencode"``, ``"codex"``, ``"agy"``,
            or ``"all"``.
        target: Repo root used by OpenCode's test (looks for ``.opencode.json``).
    """
    if agent not in AGENT_CHOICES:
        raise ValueError(
            f"unknown agent {agent!r}; expected one of {list(AGENT_CHOICES)}"
        )
    if target is None:
        target = Path.cwd()
    target = Path(target).resolve()

    if agent == "all":
        return [TEST_HOOKS[a](target) for a in ("claude", "opencode", "codex", "agy")]
    return TEST_HOOKS[agent](target)


# Tell pytest not to auto-collect this as a test (it's an importable
# function exported from this module). Module-level attribute is the
# only way pytest honors it.
test_hooks_for_agent.__test__ = False  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# CLI entry points (console_scripts)
# --------------------------------------------------------------------------- #


def _build_argparser() -> "argparse.ArgumentParser":
    parser = argparse.ArgumentParser(
        prog="merge_train",
        description=(
            "merge_train: install/test per-agent conflict-warn hooks. "
            "After PR #18, all hooks are warn-only."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser(
        "install-hooks",
        help="Install conflict-warn hooks for one or more agents.",
    )
    p_install.add_argument(
        "--agent",
        required=True,
        choices=list(AGENT_CHOICES),
        help="Which agent to install hooks for.",
    )
    p_install.add_argument(
        "--target",
        default=None,
        help="Target repo root (default: cwd). Used by opencode for .opencode.json.",
    )

    p_test = sub.add_parser(
        "test-hooks",
        help="Run synthetic-Edit test against installed hooks.",
    )
    p_test.add_argument(
        "--agent",
        required=True,
        choices=list(AGENT_CHOICES),
        help="Which agent to test hooks for.",
    )
    p_test.add_argument(
        "--target",
        default=None,
        help="Target repo root (default: cwd). Used by opencode.",
    )

    return parser


def main_install_hooks(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for ``merge_train install-hooks``."""
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.cmd != "install-hooks":
        parser.error("this entry point only handles install-hooks")
    target = Path(args.target) if args.target else None
    result = install_hooks_for_agent(args.agent, target=target)
    print(json.dumps(result, indent=2))
    # exit 0 on success, 1 if any sub-install reported installed:False
    if isinstance(result, list):
        return 0 if all(r.get("installed") for r in result) else 1
    return 0 if result.get("installed") else 1


def main_test_hooks(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for ``merge_train test-hooks``."""
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.cmd != "test-hooks":
        parser.error("this entry point only handles test-hooks")
    target = Path(args.target) if args.target else None
    result = test_hooks_for_agent(args.agent, target=target)
    print(json.dumps(result, indent=2))
    if isinstance(result, list):
        return 0 if all(r.get("ok") for r in result) else 1
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    # Allow `python -m merge_train.hook_install install-hooks --agent claude`
    import sys as _sys

    cmd = _sys.argv[1] if len(_sys.argv) > 1 else ""
    if cmd == "install-hooks":
        _sys.exit(main_install_hooks(_sys.argv[1:]))
    if cmd == "test-hooks":
        _sys.exit(main_test_hooks(_sys.argv[1:]))
    print(__doc__, file=_sys.stderr)
    _sys.exit(2)
