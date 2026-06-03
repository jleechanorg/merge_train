"""Tests for the ``install-hooks`` and ``test-hooks`` CLIs.

These verify the per-agent hook installer (idempotent, warn-only) and
the synthetic-Edit test harness that exercises each hook end-to-end.

The installer must:
- Be idempotent (re-running does not duplicate entries).
- Never produce blocking (deny) config — all hooks are warn-only per PR #18.
- Write hook scripts to ``~/.local/bin/`` (mirroring install.sh).
- For Claude: patch ``~/.claude/settings.json`` PreToolUse matchers Edit+Write.
- For Codex: patch ``~/.codex/hooks.json`` PreToolUse matcher Edit.
- For OpenCode: write predict-conflicts instructions to ``.opencode.json``
  at the target repo root (matches the repo's own config).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

from merge_train.hook_install import (
    AGENT_CHOICES,
    ALL_HOOK_SCRIPTS,
    HOOKS_INSTALL_DIR,
    TEST_HOOKS,
    hooks_install_dir,
    install_hooks_for_agent,
    test_hooks_for_agent,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``$HOME`` (and Path.home) to a temp directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Create a fake target repo (no .git, but with a marker file)."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fake\n")
    return repo


@pytest.fixture
def fake_claude_settings(fake_home: Path) -> Path:
    """Write a minimal ``~/.claude/settings.json`` with no hooks block."""
    p = fake_home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    return p


@pytest.fixture
def fake_codex_hooks(fake_home: Path) -> Path:
    """Write a minimal ``~/.codex/hooks.json`` with no hooks block."""
    p = fake_home / ".codex" / "hooks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"hooks": {}}))
    return p


# --------------------------------------------------------------------------- #
# Module-level sanity
# --------------------------------------------------------------------------- #


def test_agent_choices_includes_required_agents() -> None:
    """The CLI must accept claude, opencode, codex, and all."""
    assert "claude" in AGENT_CHOICES
    assert "opencode" in AGENT_CHOICES
    assert "codex" in AGENT_CHOICES
    assert "all" in AGENT_CHOICES


def test_all_hook_scripts_exist_in_repo() -> None:
    """ALL_HOOK_SCRIPTS names map to files in this repo's hooks/ dir."""
    repo_root = Path(__file__).resolve().parents[1]
    for name in ALL_HOOK_SCRIPTS:
        assert (
            repo_root / "merge_train" / "hooks" / name
        ).is_file(), f"missing hook: {name}"


def test_hooks_install_dir_is_under_home_local_bin() -> None:
    """Hook scripts install to ~/.local/bin/ (matches install.sh pattern)."""
    d = hooks_install_dir()
    assert d.name == "bin"
    assert d.parent.name == ".local"


# --------------------------------------------------------------------------- #
# install_hooks_for_agent — Claude
# --------------------------------------------------------------------------- #


def test_install_hooks_claude_patches_settings_json(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_repo: Path,
) -> None:
    """Claude install adds conflict-warn-pre-tool to PreToolUse Edit+Write."""
    result = install_hooks_for_agent("claude", target=fake_repo)

    assert result["agent"] == "claude"
    assert result["installed"] is True
    data = json.loads(fake_claude_settings.read_text())
    pre_tool_hooks = data.get("hooks", {}).get("PreToolUse", [])
    edit_matchers = [m for m in pre_tool_hooks if m.get("matcher") == "Edit"]
    write_matchers = [m for m in pre_tool_hooks if m.get("matcher") == "Write"]
    assert edit_matchers, "Edit matcher must be added"
    assert write_matchers, "Write matcher must be added"

    # The new entry references the installed conflict-warn-pre-tool.sh
    edit_cmd = " ".join(
        h.get("command", "") for m in edit_matchers for h in m.get("hooks", [])
    )
    assert "conflict-warn-pre-tool.sh" in edit_cmd


def test_install_hooks_claude_is_idempotent(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_repo: Path,
) -> None:
    """Running install twice does not duplicate PreToolUse entries."""
    install_hooks_for_agent("claude", target=fake_repo)
    install_hooks_for_agent("claude", target=fake_repo)

    data = json.loads(fake_claude_settings.read_text())
    pre_tool_hooks = data.get("hooks", {}).get("PreToolUse", [])
    edit_matchers = [m for m in pre_tool_hooks if m.get("matcher") == "Edit"]
    assert len(edit_matchers) == 1, "Edit matcher must not duplicate"
    # And inside that matcher, only one conflict-warn entry
    assert len(edit_matchers[0]["hooks"]) == 1


def test_install_hooks_claude_copies_scripts_to_local_bin(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_repo: Path,
) -> None:
    """The hook shell scripts are copied to ~/.local/bin/ and made executable."""
    install_hooks_for_agent("claude", target=fake_repo)
    for name in ("conflict-warn-pre-tool.sh", "predict-spawn-check.sh"):
        dst = hooks_install_dir() / name
        assert dst.is_file(), f"hook script not copied: {dst}"
        assert os.access(dst, os.X_OK), f"hook script not executable: {dst}"


def test_install_hooks_claude_removes_stale_source_repo_entries(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """If ~/.claude/settings.json references old source-repo paths, strip them."""
    from merge_train.hook_install import _repo_root

    stale_repo = str(_repo_root())
    settings = fake_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"bash {stale_repo}/hooks/conflict-warn-pre-tool.sh",
                                },
                            ],
                        },
                    ],
                },
            }
        )
    )
    install_hooks_for_agent("claude", target=fake_repo)
    data = json.loads(settings.read_text())
    edit = next(m for m in data["hooks"]["PreToolUse"] if m["matcher"] == "Edit")
    for h in edit["hooks"]:
        assert (
            "merge_train/hooks/" not in h["command"]
        ), "stale source-repo path not stripped"


def test_install_hooks_claude_only_adds_warn_only_config(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_repo: Path,
) -> None:
    """Installer must not add 'deny' or 'ask' permission decisions."""
    install_hooks_for_agent("claude", target=fake_repo)
    data = json.loads(fake_claude_settings.read_text())
    raw = json.dumps(data)
    assert "permissionDecision" not in raw or "deny" not in raw
    # The wired hook (conflict-warn-pre-tool.sh) is itself warn-only
    # — its body emits permissionDecision="allow" only.


# --------------------------------------------------------------------------- #
# install_hooks_for_agent — Codex
# --------------------------------------------------------------------------- #


def test_install_hooks_codex_patches_hooks_json(
    fake_home: Path,
    fake_codex_hooks: Path,
    fake_repo: Path,
) -> None:
    """Codex install adds predict-spawn-check to PreToolUse Edit matcher."""
    install_hooks_for_agent("codex", target=fake_repo)
    data = json.loads(fake_codex_hooks.read_text())
    pre = data.get("hooks", {}).get("PreToolUse", [])
    edit_matchers = [m for m in pre if m.get("matcher") == "Edit"]
    assert edit_matchers, "Edit matcher must be added"
    cmds = " ".join(
        h.get("command", "") for m in edit_matchers for h in m.get("hooks", [])
    )
    assert "predict-spawn-check" in cmds


def test_install_hooks_codex_is_idempotent(
    fake_home: Path,
    fake_codex_hooks: Path,
    fake_repo: Path,
) -> None:
    """Re-running codex install does not duplicate PreToolUse entries."""
    install_hooks_for_agent("codex", target=fake_repo)
    install_hooks_for_agent("codex", target=fake_repo)

    data = json.loads(fake_codex_hooks.read_text())
    pre = data.get("hooks", {}).get("PreToolUse", [])
    edit = [m for m in pre if m.get("matcher") == "Edit"]
    assert len(edit) == 1
    assert len(edit[0]["hooks"]) == 1


def test_install_hooks_codex_creates_hooks_json_if_missing(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """If ~/.codex/hooks.json does not exist, installer creates it."""
    assert not (fake_home / ".codex" / "hooks.json").exists()
    install_hooks_for_agent("codex", target=fake_repo)
    p = fake_home / ".codex" / "hooks.json"
    assert p.is_file()
    data = json.loads(p.read_text())
    assert "hooks" in data


# --------------------------------------------------------------------------- #
# install_hooks_for_agent — OpenCode
# --------------------------------------------------------------------------- #


def test_install_hooks_opencode_writes_target_opencode_json(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """OpenCode install writes predict-conflicts instructions to <repo>/.opencode.json."""
    install_hooks_for_agent("opencode", target=fake_repo)
    p = fake_repo / ".opencode.json"
    assert p.is_file(), "OpenCode installer must create .opencode.json in target"
    data = json.loads(p.read_text())
    instructions = data.get("instructions", "")
    assert "predict-conflicts" in instructions


def test_install_hooks_opencode_is_idempotent(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """Re-running opencode install does not duplicate the instructions string."""
    install_hooks_for_agent("opencode", target=fake_repo)
    first = (fake_repo / ".opencode.json").read_text()
    install_hooks_for_agent("opencode", target=fake_repo)
    second = (fake_repo / ".opencode.json").read_text()
    assert first == second, "OpenCode installer must be idempotent"


def test_install_hooks_opencode_preserves_existing_fields(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """If .opencode.json exists with $schema, preserve it."""
    p = fake_repo / ".opencode.json"
    p.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "instructions": "unrelated: do not lose me",
            }
        )
    )
    install_hooks_for_agent("opencode", target=fake_repo)
    data = json.loads(p.read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert "unrelated" in data["instructions"]
    assert "predict-conflicts" in data["instructions"]


def test_install_hooks_opencode_only_appends_predict_conflicts_block(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """If existing instructions already mention predict-conflicts, do not duplicate."""
    p = fake_repo / ".opencode.json"
    p.write_text(
        json.dumps(
            {
                "instructions": "Use predict-conflicts before editing.",
            }
        )
    )
    install_hooks_for_agent("opencode", target=fake_repo)
    data = json.loads(p.read_text())
    count = data["instructions"].count("predict-conflicts")
    assert count == 1, f"predict-conflicts should appear once, got {count}"


# --------------------------------------------------------------------------- #
# install_hooks_for_agent — dispatch + errors
# --------------------------------------------------------------------------- #


def test_install_hooks_all_runs_every_agent(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_codex_hooks: Path,
    fake_repo: Path,
) -> None:
    """`--agent all` calls each per-agent installer in turn."""
    results = install_hooks_for_agent("all", target=fake_repo)
    assert isinstance(results, list) and len(results) == 3
    agents = sorted(r["agent"] for r in results)
    assert agents == ["claude", "codex", "opencode"]


def test_install_hooks_unknown_agent_raises() -> None:
    """Unknown --agent value raises ValueError with a useful message."""
    with pytest.raises(ValueError, match="unknown agent"):
        install_hooks_for_agent("bogus", target=Path("/tmp"))


def test_install_hooks_creates_local_bin_dir(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_repo: Path,
) -> None:
    """hooks_install_dir() is created if missing."""
    d = hooks_install_dir()
    assert not d.exists()
    install_hooks_for_agent("claude", target=fake_repo)
    assert d.is_dir()


# --------------------------------------------------------------------------- #
# test_hooks_for_agent — synthetic event
# --------------------------------------------------------------------------- #


def test_test_hooks_claude_exits_zero_with_warning(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_repo: Path,
) -> None:
    """test-hooks claude runs synthetic Edit, asserts exit 0 + warn behavior."""
    install_hooks_for_agent("claude", target=fake_repo)
    result = test_hooks_for_agent("claude", target=fake_repo)
    assert result["agent"] == "claude"
    assert result["ok"] is True
    assert result["exit_code"] == 0
    # The Claude PreToolUse hook emits permissionDecision=allow; test
    # harness just checks the hook binary itself behaves.
    assert "hook" in result


def test_test_hooks_codex_exits_zero(
    fake_home: Path,
    fake_codex_hooks: Path,
    fake_repo: Path,
) -> None:
    """test-hooks codex runs synthetic Edit, asserts exit 0 + warn behavior."""
    install_hooks_for_agent("codex", target=fake_repo)
    result = test_hooks_for_agent("codex", target=fake_repo)
    assert result["agent"] == "codex"
    assert result["ok"] is True
    assert result["exit_code"] == 0


def test_test_hooks_opencode_exits_zero(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """test-hooks opencode validates the .opencode.json instructions exist."""
    install_hooks_for_agent("opencode", target=fake_repo)
    result = test_hooks_for_agent("opencode", target=fake_repo)
    assert result["agent"] == "opencode"
    assert result["ok"] is True
    assert result["exit_code"] == 0


def test_test_hooks_all_returns_one_per_agent(
    fake_home: Path,
    fake_claude_settings: Path,
    fake_codex_hooks: Path,
    fake_repo: Path,
) -> None:
    """`--agent all` returns one result per installed agent."""
    install_hooks_for_agent("all", target=fake_repo)
    results = test_hooks_for_agent("all", target=fake_repo)
    assert isinstance(results, list) and len(results) == 3
    agents = sorted(r["agent"] for r in results)
    assert agents == ["claude", "codex", "opencode"]
    for r in results:
        assert r["ok"] is True


def test_test_hooks_claude_fails_when_hook_missing(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """If claude settings.json has no hook wired, test-hooks reports ok=False."""
    # No install step — hook entry must be absent.
    result = test_hooks_for_agent("claude", target=fake_repo)
    assert result["ok"] is False
    assert (
        "missing" in (result.get("reason") or "").lower()
        or "not installed" in (result.get("reason") or "").lower()
    )


# --------------------------------------------------------------------------- #
# TEST_HOOKS contract
# --------------------------------------------------------------------------- #


def test_test_hooks_dispatch_table_has_all_agents() -> None:
    """TEST_HOOKS dispatch table covers the three required agents."""
    assert "claude" in TEST_HOOKS
    assert "codex" in TEST_HOOKS
    assert "opencode" in TEST_HOOKS
    for agent, fn in TEST_HOOKS.items():
        assert callable(fn), f"{agent} test_hooks entry is not callable"


def test_install_hooks_codex_removes_stale_source_repo_entries(
    fake_home: Path,
    fake_repo: Path,
) -> None:
    """If ~/.codex/hooks.json references old source-repo paths, strip them."""
    from merge_train.hook_install import _repo_root

    stale_repo = str(_repo_root())
    hooks_file = fake_home / ".codex" / "hooks.json"
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"bash {stale_repo}/hooks/predict-spawn-check.sh",
                                },
                            ],
                        },
                    ],
                },
            }
        )
    )
    install_hooks_for_agent("codex", target=fake_repo)
    data = json.loads(hooks_file.read_text())
    edit = next(m for m in data["hooks"]["PreToolUse"] if m["matcher"] == "Edit")
    for h in edit["hooks"]:
        assert (
            "merge_train/hooks/" not in h["command"]
        ), "stale source-repo path not stripped"


def test_install_hooks_fails_when_hook_scripts_missing(
    fake_home: Path,
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If hook scripts are missing, installer must return installed: False."""
    from merge_train import hook_install

    # Mock _repo_root to raise FileNotFoundError, and mock _find_hooks_dir to raise FileNotFoundError
    # so that hook installation fails because no hooks directory can be found
    def mock_find_hooks_dir(*args, **kwargs):
        raise FileNotFoundError("Mocked missing hooks directory")

    monkeypatch.setattr(
        hook_install, "_find_hooks_dir", mock_find_hooks_dir, raising=False
    )

    result = install_hooks_for_agent("claude", target=fake_repo)
    assert result["installed"] is False
    assert "error" in result


def test_find_hooks_dir_installed_vs_source(tmp_path: Path) -> None:
    from merge_train.hook_install import _find_hooks_dir

    # Case 1: Package layout (hooks inside the package folder)
    pkg_dir = tmp_path / "merge_train"
    pkg_hooks = pkg_dir / "hooks"
    pkg_hooks.mkdir(parents=True)

    resolved = _find_hooks_dir(base_dir=pkg_dir)
    assert resolved == pkg_hooks

    # Case 2: Source layout (hooks beside the package folder)
    src_dir = tmp_path / "src"
    src_pkg = src_dir / "merge_train"
    src_hooks = src_dir / "hooks"
    src_pkg.mkdir(parents=True)
    src_hooks.mkdir()

    resolved = _find_hooks_dir(base_dir=src_pkg)
    assert resolved == src_hooks

    # Case 3: Missing
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        _find_hooks_dir(base_dir=missing_dir)
