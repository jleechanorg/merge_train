"""Contracts for the canonical shell installer wiring."""

from pathlib import Path


INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"


def test_install_script_updates_existing_uv_tool() -> None:
    body = INSTALL_SH.read_text()
    assert 'uv tool install "$MERGE_TRAIN_ROOT" --reinstall --quiet' in body
    assert "skip: already installed via uv" not in body


def test_install_script_uses_one_scope_and_specific_matchers() -> None:
    body = INSTALL_SH.read_text()

    assert 'CODEX_HOOKS" --event PreToolUse' in body
    assert '--config "$CODEX_HOOKS" --event PreToolUse' in body
    assert '--config "$CODEX_GLOBAL_HOOKS" --event PreToolUse' in body
    assert '--matcher "^apply_patch$"' in body

    assert '--config "$GEMINI_SETTINGS" --event BeforeTool' in body
    assert '--config "$GEMINI_GLOBAL_SETTINGS" --event BeforeTool' in body
    assert '--matcher "write_file|replace"' in body

    assert '--config "$CURSOR_HOOKS" --event preToolUse' in body
    assert '--matcher "Edit|Write|StrReplace|Delete|EditNotebook"' in body
    assert '--config "$CURSOR_GLOBAL_HOOKS" --event preToolUse' in body

    # Codex/Gemini own the global scope; Cursor owns the project scope. The
    # opposite layer is migration cleanup only, preventing duplicate runs.
    assert body.count("--remove-only") >= 4


def test_install_script_creates_opencode_plugin_directory() -> None:
    body = INSTALL_SH.read_text()
    assert 'mkdir -p "$OPENCODE_PLUGIN_DIR"' in body
    assert 'cp "$OPENCODE_PLUGIN_SRC" "$OPENCODE_PLUGIN_DST"' in body
