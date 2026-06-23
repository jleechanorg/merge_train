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
    """``_decision_payload`` outputs the canonical Claude Code hook format.

    - Top-level ``decision`` uses "approve"/"block" (NOT "allow"/"deny").
    - Top-level ``reason`` and ``systemMessage`` carry the same text.
    - Legacy ``hookSpecificOutput.permissionDecision`` is kept for backward
      compat with codex/cursor/gemini runtimes — it also uses "approve".
    always_approve.sh (the reference implementation) uses {"decision":"approve"}.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module._decision_payload("allow", "merge_train: hello — no conflicts.")
    # Top-level canonical fields (Claude Code PreToolUse hook spec).
    assert payload["decision"] == "approve", (
        f"'allow' must map to 'approve' (not 'allow'); got {payload['decision']!r}. "
        "Claude Code rejects 'allow' as unsupported (see always_approve.sh)."
    )
    assert "systemMessage" in payload, "systemMessage must be at top level"
    assert payload["systemMessage"] == "merge_train: hello — no conflicts."
    assert payload["reason"] == "merge_train: hello — no conflicts."
    # Legacy field preserved for codex/cursor/gemini backward compat.
    assert payload["hookSpecificOutput"]["permissionDecision"] == "approve"
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


def test_non_mutation_tool_emits_no_stdout(tmp_path: Path) -> None:
    """Non-mutation tools (Read, Bash, ...) must produce NO stdout.

    Exit 0 with no stdout is the correct "implicit approve" signal for
    Claude Code (and other runtimes). Previously the helper emitted a
    decision payload with permissionDecision:"allow" which Claude Code
    rejected as "unsupported permissionDecision:allow".
    """
    helper = _helper_path_for_test()
    for tool in ("Read", "Bash", "TodoWrite", "unknown_tool"):
        result = subprocess.run(
            [sys.executable, str(helper)],
            input=json.dumps({"tool_name": tool, "tool_input": {}}).encode(),
            capture_output=True,
            check=True,
        )
        assert result.stdout == b"", (
            f"tool={tool!r}: expected empty stdout (implicit approve); "
            f"got: {result.stdout!r}"
        )
        assert b"not a file mutation" in result.stderr, (
            f"tool={tool!r}: expected skip warning on stderr; got: {result.stderr!r}"
        )


def test_decision_map_canonical_values() -> None:
    """_DECISION_MAP must translate every internal name to Claude Code's values.

    Claude Code only accepts "approve" and "block" as permissionDecision values.
    "allow"/"warn" → "approve", "deny"/"block" → "block".
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    expected = {
        "allow": "approve",
        "warn": "approve",
        "approve": "approve",
        "deny": "block",
        "block": "block",
    }
    for internal, canonical in expected.items():
        p = module._decision_payload(internal, "test reason")
        assert p["decision"] == canonical, (
            f"_DECISION_MAP[{internal!r}] should map to {canonical!r}; "
            f"got {p['decision']!r}"
        )
        assert p["hookSpecificOutput"]["permissionDecision"] == canonical


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


def _make_fake_gh(tmp_path: Path, stdout_json: str) -> Path:
    """Write a fake ``gh`` script that prints ``stdout_json`` and exits 0."""
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(f"#!/bin/sh\necho '{stdout_json}'\n")
    fake_gh.chmod(0o755)
    return fake_gh


def test_no_conflict_silent_approve(tmp_path: Path) -> None:
    """When no open PRs exist, the helper must exit 0 with a minimal
    ``{"decision":"approve"}`` containing NO ``systemMessage`` — the
    user should not be notified on every routine edit.

    Regression for: every Edit was emitting an allow+systemMessage payload
    that the TUI showed as a "no conflicts" banner on every file write.
    """
    import os
    helper = _helper_path_for_test()
    # Use a uniquely-named repo dir so the cache key doesn't collide with
    # other tests or prior runs (cache lives at /tmp/merge_train_cache_{name}.json).
    repo = tmp_path / "norepo"
    # Wipe any stale cache for this repo name.
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)

    # Fake gh returns an empty list (no open PRs).
    fake_gh = _make_fake_gh(tmp_path, "[]")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    # Create a minimal git repo so the helper can find repo root.
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=str(repo), check=True, capture_output=True,
    )

    tool_input = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "foo.py")},
    })
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=tool_input.encode(),
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    assert result.returncode == 0, f"expected exit 0; got {result.returncode}\nstderr: {result.stderr.decode()}"
    stdout_text = result.stdout.decode().strip()
    assert stdout_text, "expected non-empty stdout (approve JSON)"
    payload = json.loads(stdout_text.splitlines()[-1])
    assert payload.get("decision") == "approve", f"expected decision:approve; got {payload}"
    assert "systemMessage" not in payload, (
        f"no-conflict path must NOT emit systemMessage (causes TUI banner on every edit); "
        f"got: {payload.get('systemMessage')!r}"
    )


def test_warn_only_conflict_exits_nonzero(tmp_path: Path) -> None:
    """When a warn-only conflict is found, the helper must exit 1 so
    Claude Code surfaces a TUI notification (non-blocking "hook error"
    banner showing the first stderr line).

    The stdout payload must still be ``decision:approve`` so the tool
    is NOT blocked.

    Regression for: warn-only conflicts silently returned exit 0 + allow,
    which Claude Code showed as nothing — the user never saw the conflict.
    """
    import os
    helper = _helper_path_for_test()

    # Create a minimal git repo with a conflicting PR response.
    # Use a uniquely-named repo dir to avoid stale cache collisions.
    repo = tmp_path / "warnrepo"
    # Wipe any stale cache for this repo name.
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/jleechanorg/worldarchitect.ai.git"],
        cwd=str(repo), check=True, capture_output=True,
    )
    target_file = repo / "foo.py"
    target_file.write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    # Fake gh: returns one open PR that touches foo.py (same file as the edit).
    pr_list_json = json.dumps([{
        "number": 999,
        "headRefName": "other-branch",
        "title": "Other PR touching foo.py",
    }])
    fake_gh = tmp_path / "gh"
    # gh pr diff --name-only returns one filename per line (no diff headers).
    fake_gh.write_text(
        f"#!/bin/sh\n"
        f'if echo "$*" | grep -q "pr list"; then echo \'{pr_list_json}\'; exit 0; fi\n'
        f'if echo "$*" | grep -q "pr diff"; then echo "foo.py"; exit 0; fi\n'
        f"exit 0\n"
    )
    fake_gh.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    tool_input = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "foo.py")},
    })
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=tool_input.encode(),
        capture_output=True,
        cwd=str(repo),
        env=env,
    )

    # Exit 1 = TUI notification (non-blocking).
    assert result.returncode == 1, (
        f"warn-only conflict must exit 1 for TUI visibility; got {result.returncode}\n"
        f"stdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}"
    )
    stdout_text = result.stdout.decode().strip()
    assert stdout_text, "expected non-empty stdout (approve JSON even for warn-only)"
    last_line = stdout_text.splitlines()[-1]
    # May fail JSON parse if debug output is mixed — verify separately.
    try:
        payload = json.loads(last_line)
        assert payload.get("decision") == "approve", (
            f"warn-only must still approve (tool not blocked); got {payload.get('decision')!r}"
        )
    except json.JSONDecodeError:
        pytest.fail(f"stdout last line is not valid JSON: {last_line!r}")
