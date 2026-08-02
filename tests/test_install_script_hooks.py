"""Contracts for the canonical shell installer wiring."""

from pathlib import Path


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
