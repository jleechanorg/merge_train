"""Tests for ``conflict_check_helper.py`` chat-visible output.

The helper is a standalone script run by the conflict-warn bash hook.
The ``systemMessage`` field is the only visibility surface that works
identically across Claude Code / Codex / Agy, so we guard it with
direct unit tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HELPER_PATH = Path(__file__).resolve().parents[1] / "merge_train" / "hooks" / "conflict_check_helper.py"


def _helper_path_for_test() -> Path:
    """Resolve the helper path. Falls back to ~/.local/bin/ if the in-tree
    copy is not present (e.g. when running from an installed wheel)."""
    if HELPER_PATH.is_file():
        return HELPER_PATH
    installed = Path.home() / ".local" / "bin" / "conflict_check_helper.py"
    if installed.is_file():
        return installed
    pytest.skip("conflict_check_helper.py not found")


def test_decision_payload_includes_system_message() -> None:
    """``_decision_payload`` puts a top-level ``systemMessage`` alongside
    the ``permissionDecisionReason``. Without this, the chat banner
    doesn't appear in any CLI."""
    # Import the helper module directly to test the helper function.
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module._decision_payload("allow", "merge_train: hello — no conflicts.")
    assert "systemMessage" in payload, "systemMessage must be at top level"
    assert payload["systemMessage"] == "merge_train: hello — no conflicts."
    # The legacy field is preserved for back-compat.
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        payload["hookSpecificOutput"]["permissionDecisionReason"]
        == "merge_train: hello — no conflicts."
    )


def test_system_message_matches_permission_decision_reason() -> None:
    """Both chat-visible fields carry the same text. A future parser can
    rely on either."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for decision, reason in [
        ("allow", "ok"),
        ("deny", "blocked by config"),
        ("ask", "needs human review"),
    ]:
        p = module._decision_payload(decision, reason)
        assert p["systemMessage"] == p["hookSpecificOutput"]["permissionDecisionReason"]


def test_emit_allow_path_includes_system_message(tmp_path: Path) -> None:
    """End-to-end: helper called as a subprocess emits systemMessage
    on the allow path."""
    helper = _helper_path_for_test()
    # A non-Edit tool short-circuits to allow with systemMessage.
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=json.dumps({"tool_name": "Read", "tool_input": {}}).encode(),
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert "systemMessage" in payload
    assert "Read" in payload["systemMessage"]  # reason mentions the skipped tool
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_emit_empty_payload_includes_system_message() -> None:
    """Empty stdin → allow with a systemMessage, not silent."""
    helper = _helper_path_for_test()
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=b"",
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert "systemMessage" in payload
    assert "empty payload" in payload["systemMessage"]
