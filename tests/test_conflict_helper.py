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


def test_decision_payload_truncates_long_reason() -> None:
    """A 12K-char reason must be truncated so ``systemMessage`` stays
    under Claude Code's 10K-char cap. The chat banner is the whole
    point of this feature — if it gets silently replaced with a
    "see file path" stub, the user loses visibility. See M2 in the
    adversarial review of PR #29."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Synthesize a reason that would otherwise blow past 10K chars —
    # e.g., the conflict reason for 50 PRs all touching the same file.
    long_reason = "PR#1/foo.py — conflict: " + ("x" * 12_000)
    payload = module._decision_payload("deny", long_reason)

    # The two chat-visible fields must stay under the 10K cap and
    # carry the same (truncated) text.
    assert len(payload["systemMessage"]) < 10_000, (
        f"systemMessage over the 10K cap: len={len(payload['systemMessage'])}"
    )
    assert "truncated" in payload["systemMessage"], (
        f"expected ' (truncated)' suffix after cut; got tail: {payload['systemMessage'][-80:]!r}"
    )
    assert payload["systemMessage"] == payload["hookSpecificOutput"]["permissionDecisionReason"]
    # And the original 12K payload must NOT have been passed through
    # verbatim (the cut was applied before fanning out to both fields).
    assert len(payload["systemMessage"]) < len(long_reason)


def test_decision_payload_short_reason_unchanged() -> None:
    """Truncation is a no-op for short reasons. We must not gratuitously
    mutate well-formed output."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    short = "merge_train: hello — no conflicts."
    payload = module._decision_payload("allow", short)
    assert payload["systemMessage"] == short
    assert "truncated" not in payload["systemMessage"]
