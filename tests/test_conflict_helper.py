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

    Both blocking and non-blocking decisions live under ``hookSpecificOutput``
    with ``permissionDecision`` in the strict ``allow|deny|ask|defer`` enum.
    No legacy top-level ``decision`` field is emitted (Codex rejects
    ``decision: "approve"`` and Claude Code rejects ``approve`` as an
    unsupported ``permissionDecision`` enum value with the message
    ``Hook JSON output validation failed — (root): Invalid input``).

    See https://code.claude.com/docs/en/hooks for the current schema.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module._decision_payload("allow", "merge_train: hello — no conflicts.")
    # No legacy top-level fields — current runtimes reject both `decision`
    # and `systemMessage` as legacy / ambiguous.
    assert "decision" not in payload, f"legacy top-level 'decision' is rejected: {payload}"
    assert "systemMessage" not in payload, (
        f"top-level 'systemMessage' is ambiguous when paired with permissionDecision; "
        f"the runtime picks one based on structured-concurrency semantics: {payload}"
    )
    # Canonical hook-specific fields.
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        payload["hookSpecificOutput"]["permissionDecisionReason"]
        == "merge_train: hello — no conflicts."
    )


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
    """``_DECISION_MAP`` must translate every internal name to a value in the
    Claude Code ``permissionDecision`` strict enum (``allow | deny | ask |
    defer``). The legacy ``"approve"`` / ``"block"`` values are NOT in the
    enum and are rejected with ``Hook JSON output validation failed —
    (root): Invalid input`` on every Write/Edit (see issue #42)."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Every internal name must map to either "allow" or "deny" — never the
    # legacy "approve"/"block" values (now strict-enum violations).
    expected = {
        "allow": "allow",
        "warn": "allow",
        "approve": "allow",
        "deny": "deny",
        "block": "deny",
    }
    for internal, canonical in expected.items():
        p = module._decision_payload(internal, "test reason")
        assert p["hookSpecificOutput"]["permissionDecision"] == canonical, (
            f"_DECISION_MAP[{internal!r}] should map to {canonical!r}; "
            f"got {p['hookSpecificOutput']['permissionDecision']!r}"
        )
        # The deprecated top-level 'decision' field is never emitted.
        assert "decision" not in p, (
            f"legacy top-level 'decision' field emitted for {internal!r}: {p}"
        )
        # The chat-visible reason always travels as permissionDecisionReason.
        assert p["hookSpecificOutput"]["permissionDecisionReason"] == "test reason"


def test_emit_empty_payload_includes_system_message() -> None:
    """Empty stdin → allow payload with ``permissionDecision: "allow"`` and
    a chat-visible ``permissionDecisionReason``, not legacy
    ``systemMessage`` (the empty-payload path passes through
    ``_decision_payload``, so we just verify the canonical shape)."""
    helper = _helper_path_for_test()
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=b"",
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "empty payload" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_issue_42_repro_emits_allow_permission_decision(tmp_path: Path) -> None:
    """End-to-end regression for issue #42.

    ``echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/x.txt","content":"hi"}}' |
       bash ~/.local/bin/conflict-warn-pre-tool.sh``

    Before the fix, this emitted ``{"decision":"approve"}`` (legacy top-level
    field) which Claude Code rejected on every Write/Edit with
    ``PreToolUse:Write hook error / Hook JSON output validation failed —
    (root): Invalid input``. After the fix, the output is the current-schema
    ``hookSpecificOutput.permissionDecision="allow"`` — strictly inside the
    Claude Code enum (``allow | deny | ask | defer``).

    We exercise the in-tree wrapper via subprocess.run with the same args as
    the issue repro. Using the in-tree wrapper is equivalent for a Write on
    a non-git path: the bash wrapper delegates to the helper, which falls
    through to ``_emit("allow", "...not inside a git repo; allowing.")``.
    """
    helper = _helper_path_for_test()
    wrapper = helper.parent / "conflict-warn-pre-tool.sh"
    if not wrapper.is_file():
        pytest.skip("conflict-warn-pre-tool.sh wrapper not in tree")

    stdin_payload = (
        '{"tool_name":"Write","tool_input":{"file_path":"/tmp/x.txt","content":"hi"}}'
    )
    result = subprocess.run(
        ["bash", str(wrapper)],
        input=stdin_payload.encode(),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"wrapper should not error on a non-mutation / non-git path; got rc={result.returncode}\n"
        f"stderr: {result.stderr.decode()}"
    )

    stdout_text = result.stdout.decode().strip()
    assert stdout_text, "expected non-empty stdout (allow JSON payload)"
    payload = json.loads(stdout_text.splitlines()[-1])

    # The fix: canonical permissionDecision enum value.
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow", (
        f"issue #42 regression: expected permissionDecision='allow', "
        f"got {payload['hookSpecificOutput'].get('permissionDecision')!r}.\n"
        f"Claude Code rejects any value outside the strict enum "
        f"{{allow, deny, ask, defer}} with "
        f"'Hook JSON output validation failed — (root): Invalid input'."
    )
    # No legacy top-level "decision" field — that was the bug.
    assert "decision" not in payload, (
        f"legacy top-level 'decision' field emitted; this is the bug from issue #42: "
        f"the helper used to emit {{'decision':'approve'}} which Claude Code "
        f"rejected. payload={payload}"
    )
    # The chat-visible reason travels as permissionDecisionReason, not as
    # the legacy "reason" or "systemMessage".
    assert "permissionDecisionReason" in payload["hookSpecificOutput"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_decision_payload_deny_uses_deny_enum_value() -> None:
    """Block path emits ``permissionDecision: "deny"`` (canonical enum).

    Confirms the deny side of the mapping too; covers the full
    ``allow|deny`` split instead of just the allow side of the regression.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for internal in ("deny", "block"):
        p = module._decision_payload(internal, "blocked: conflict with PR #41")
        assert p["hookSpecificOutput"]["permissionDecision"] == "deny", (
            f"_DECISION_MAP[{internal!r}] should map to 'deny'; got "
            f"{p['hookSpecificOutput']['permissionDecision']!r}"
        )
        assert p["hookSpecificOutput"]["permissionDecisionReason"] == "blocked: conflict with PR #41"


def test_decision_payload_never_emits_legacy_approve_value() -> None:
    """Regression guard: assert ``"approve"`` is never present anywhere in the
    payload output. The legacy value used to appear at top-level ``decision``
    AND at ``hookSpecificOutput.permissionDecision`` (the bug). Both surfaces
    are now strictly inside the ``allow|deny|ask|defer`` enum.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    serialized_all = []
    for internal in ("allow", "warn", "approve", "deny", "block"):
        serialized_all.append(module._decision_payload(internal, f"reason for {internal}"))
    # Any one of these carries "approve" anywhere → the bug returns.
    for payload in serialized_all:
        assert "approve" not in json.dumps(payload), (
            f"legacy 'approve' value present in payload — this is the bug from issue #42: "
            f"{payload}"
        )
        # Also block side: 'block' was the legacy top-level decision value.
        assert "block" not in json.dumps(payload).replace("PermissionRequest", "").replace(
            "permissionDecisionReason", ""
        ), (
            f"legacy 'block' value present in payload — Claude Code's "
            f"permissionDecision enum is allow|deny|ask|defer, NOT 'block': "
            f"{payload}"
        )


def test_decision_payload_truncates_long_reason() -> None:
    """A 12K-char reason must be truncated so ``permissionDecisionReason``
    stays under Claude Code's 10K-char cap. The chat banner is the whole
    point of this feature — if it gets silently replaced with a "see file
    path" stub, the user loses visibility. See M2 in the adversarial
    review of PR #29."""
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

    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    # PermissionDecisionReason must stay under the 10K cap.
    assert len(reason) < 10_000, (
        f"permissionDecisionReason over the 10K cap: len={len(reason)}"
    )
    assert "truncated" in reason, (
        f"expected ' (truncated)' suffix after cut; got tail: {reason[-80:]!r}"
    )
    # And the original 12K payload must NOT have been passed through
    # verbatim (the cut was applied before fanning out).
    assert len(reason) < len(long_reason)


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
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == short
    assert "truncated" not in reason


def _make_fake_gh(tmp_path: Path, stdout_json: str) -> Path:
    """Write a fake ``gh`` script that prints ``stdout_json`` and exits 0."""
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(f"#!/bin/sh\necho '{stdout_json}'\n")
    fake_gh.chmod(0o755)
    return fake_gh


def test_no_conflict_silent_approve(tmp_path: Path) -> None:
    """When no open PRs exist, the helper must exit 0 with NO stdout — the
    user should not be notified on every routine edit.

    Regression for: every Edit emitted legacy ``decision:"approve"``, which
    Codex reports as ``unsupported decision:approve``.
    """
    import os
    helper = _helper_path_for_test()
    # Use a uniquely-named repo dir so the cache key doesn't collide with
    # other tests or prior runs (cache lives at /tmp/merge_train_cache_{name}.json).
    repo = tmp_path / "norepo"
    # Wipe any stale cache for this repo name.
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)

    # Fake gh returns an empty list (no open PRs).
    _make_fake_gh(tmp_path, "[]")
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
    assert stdout_text == "", f"expected empty stdout for implicit allow; got {stdout_text!r}"


def test_warn_only_conflict_emits_allow_payload(tmp_path: Path) -> None:
    """Warn-only conflicts must not make the hook itself look failed.

    Regression for: warn-only conflicts used to emit legacy
    ``decision:"approve"`` (and before this fix used
    ``hookSpecificOutput.additionalContext`` for non-blocking) and either
    triggered the Claude Code schema-validation error or wasted the chance
    to surface the warning. Current runtimes expect a strict-enum
    ``permissionDecision: "allow"`` with a chat-visible
    ``permissionDecisionReason``. The tool still runs — the
    ``permissionDecision: "allow"`` skips the conflict-related prompt but
    lets the warning reason reach the user.
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

    assert result.returncode == 0, (
        f"warn-only conflict must exit 0; got {result.returncode}\n"
        f"stdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}"
    )
    stdout_text = result.stdout.decode().strip()
    assert stdout_text, "expected non-empty stdout warning context"
    payload = json.loads(stdout_text.splitlines()[-1])
    # Canonical schema: permissionDecision in the strict enum, no legacy
    # top-level fields.
    assert "decision" not in payload
    assert "systemMessage" not in payload
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "warn-only" in payload["hookSpecificOutput"]["permissionDecisionReason"], (
        f"warning message must travel as permissionDecisionReason; got "
        f"{payload['hookSpecificOutput']['permissionDecisionReason']!r}"
    )
