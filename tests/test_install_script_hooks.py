"""Contracts for the canonical shell installer wiring."""

from pathlib import Path
import json
import os
import subprocess
import sys


INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"


def _wire_commands(body: str) -> list[str]:
    """Extract complete wire-helper shell invocations from install.sh."""
    lines = body.splitlines()
    commands: list[str] = []
    for index, line in enumerate(lines):
        if line != '"$PYTHON_BIN" "$WIRE_HELPER" \\':
            continue
        command = [line]
        for continuation in lines[index + 1 :]:
            command.append(continuation)
            if not continuation.rstrip().endswith("\\"):
                break
        commands.append("\n".join(command))
    return commands


def _command_for(commands: list[str], config: str, event: str) -> str:
    matches = [
        command
        for command in commands
        if f'--config "${config}" --event {event}' in command
    ]
    assert len(matches) == 1, f"expected one {config}/{event} command, got {matches}"
    return matches[0]


def test_install_script_updates_existing_uv_tool() -> None:
    body = INSTALL_SH.read_text()
    assert 'uv tool install "$MERGE_TRAIN_ROOT" --reinstall --quiet' in body
    assert "skip: already installed via uv" not in body


def test_install_script_uses_one_scope_and_specific_matchers() -> None:
    body = INSTALL_SH.read_text()
    commands = _wire_commands(body)

    codex_project = _command_for(commands, "CODEX_HOOKS", "PreToolUse")
    assert "--remove-only" in codex_project
    assert "--matcher" not in codex_project
    assert (
        '"$PYTHON_BIN" -m merge_train.hook_install install-hooks \\\n'
        '    --agent codex --target "$TARGET"'
    ) in body

    gemini_project = _command_for(commands, "GEMINI_SETTINGS", "BeforeTool")
    assert "--remove-only" in gemini_project
    assert "--matcher" not in gemini_project
    gemini_global = _command_for(commands, "GEMINI_GLOBAL_SETTINGS", "BeforeTool")
    assert '--matcher "write_file|replace"' in gemini_global
    assert "--remove-only" not in gemini_global

    cursor_project = _command_for(commands, "CURSOR_HOOKS", "preToolUse")
    assert '--matcher "Edit|Write|StrReplace|Delete|EditNotebook"' in cursor_project
    assert "--remove-only" not in cursor_project
    cursor_global = _command_for(commands, "CURSOR_GLOBAL_HOOKS", "preToolUse")
    assert "--remove-only" in cursor_global
    assert "--matcher" not in cursor_global


def test_install_script_creates_opencode_plugin_directory() -> None:
    body = INSTALL_SH.read_text()
    assert (
        'if mkdir -p "$OPENCODE_PLUGIN_DIR" '
        '&& cp "$OPENCODE_PLUGIN_SRC" "$OPENCODE_PLUGIN_DST"; then'
    ) in body
    assert 'echo "  ok: installed $OPENCODE_PLUGIN_DST"' in body
    assert 'echo "  WARN: failed to install $OPENCODE_PLUGIN_DST"' in body


def test_install_script_registers_agy_user_scope_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    target = tmp_path / "target"
    home.mkdir()
    bin_dir.mkdir()
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)

    # Keep the full installer isolated from user config and package installs.
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    predict_conflicts = bin_dir / "predict-conflicts"
    predict_conflicts.write_text("#!/bin/sh\nexit 0\n")
    predict_conflicts.chmod(0o755)

    agy_config = home / ".gemini" / "config" / "hooks.json"
    agy_config.parent.mkdir(parents=True)
    agy_config.write_text(
        json.dumps(
            {
                "cmux": {"enabled": True},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [{"type": "command", "command": "cmux hook"}],
                        }
                    ]
                },
            }
        )
    )
    gemini_settings = home / ".gemini" / "settings.json"
    gemini_settings.write_text(json.dumps({"cmux": {"enabled": True}}))

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    for _ in range(2):
        subprocess.run(
            ["bash", str(INSTALL_SH), "--python", sys.executable, str(target)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    agy_hooks = json.loads(agy_config.read_text())
    assert agy_hooks["cmux"] == {"enabled": True}
    assert any(
        hook["command"] == "cmux hook"
        for wrapper in agy_hooks["hooks"]["PreToolUse"]
        for hook in wrapper["hooks"]
    )
    registrations = [
        hook
        for wrapper in agy_hooks["hooks"]["PreToolUse"]
        for hook in wrapper["hooks"]
        if "conflict-warn-pre-tool" in hook["command"]
    ]
    assert len(registrations) == 1
    assert not agy_hooks["hooks"].get("BeforeTool")

    gemini_config = json.loads(gemini_settings.read_text())
    assert gemini_config["cmux"] == {"enabled": True}
    gemini_hooks = gemini_config["hooks"]
    assert gemini_hooks["BeforeTool"]
    assert all(
        "--runtime gemini" in hook["command"]
        for wrapper in gemini_hooks["BeforeTool"]
        for hook in wrapper["hooks"]
    )


def test_install_script_wires_portable_home_path() -> None:
    """Per-repo configs are committed and shared across hosts; never bake an
    expanded home directory into the wired command."""
    import subprocess

    body = INSTALL_SH.read_text()
    case_block = body[body.index('case "$CLAUDE_PRE_TOOL" in') :]
    case_block = case_block[: case_block.index("esac") + len("esac")]
    for home in ("/Users/alice", "/home/alice"):
        script = (
            f'HOME={home}; CLAUDE_PRE_TOOL="$HOME/.local/bin/conflict-warn-pre-tool.sh"\n'
            f'{case_block}\nprintf %s "$WIRE_CMD"'
        )
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
        assert out == "bash $HOME/.local/bin/conflict-warn-pre-tool.sh"
