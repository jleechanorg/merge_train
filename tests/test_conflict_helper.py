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
    """``_decision_payload`` outputs a current-runtime hook format.

    Non-blocking payloads must not emit legacy ``decision:"approve"`` because
    Codex rejects that field value. They carry a chat-visible ``systemMessage``
    and model-visible additional context instead.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module._decision_payload("allow", "merge_train: hello — no conflicts.")
    assert "decision" not in payload, f"legacy top-level decision is unsupported: {payload}"
    assert "systemMessage" in payload, "systemMessage must be at top level"
    assert payload["systemMessage"] == "merge_train: hello — no conflicts."
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert payload["hookSpecificOutput"]["additionalContext"] == "merge_train: hello — no conflicts."
    assert "permissionDecision" not in payload["hookSpecificOutput"]


def test_system_message_matches_permission_decision_reason() -> None:
    """Both chat-visible fields carry the same text. A future parser can
    rely on either."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for decision, reason in [("allow", "ok"), ("deny", "blocked by config")]:
        p = module._decision_payload(decision, reason)
        hook_output = p["hookSpecificOutput"]
        visible_reason = (
            hook_output.get("permissionDecisionReason")
            or hook_output.get("additionalContext")
        )
        assert p["systemMessage"] == visible_reason


def test_non_mutation_tool_is_completely_silent(tmp_path: Path) -> None:
    """Non-mutation tools (Read, Bash, ...) must produce no output.

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
        assert result.stderr == b"", (
            f"tool={tool!r}: expected empty stderr; got: {result.stderr!r}"
        )


def test_decision_map_canonical_values() -> None:
    """_DECISION_MAP must avoid unsupported non-blocking decisions.

    Codex rejects legacy ``decision:"approve"``. Non-blocking internal names
    should produce no permission decision; blocking names should produce
    ``permissionDecision:"deny"``.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    expected = {
        "allow": None,
        "warn": None,
        "approve": None,
        "deny": "deny",
        "block": "deny",
    }
    for internal, canonical in expected.items():
        p = module._decision_payload(internal, "test reason")
        assert "decision" not in p
        if canonical is None:
            assert "permissionDecision" not in p["hookSpecificOutput"]
            assert p["hookSpecificOutput"]["additionalContext"] == "test reason"
        else:
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


def test_runtime_specific_output_schemas() -> None:
    """Gemini and Cursor must not receive Claude/Codex-only fields."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._decision_payload("deny", "blocked", "gemini") == {
        "decision": "deny",
        "reason": "blocked",
    }
    assert module._decision_payload("allow", "warning", "gemini") == {
        "systemMessage": "warning"
    }
    assert module._decision_payload("deny", "blocked", "cursor") == {
        "permission": "deny",
        "user_message": "blocked",
        "agent_message": "blocked",
    }
    assert module._decision_payload("allow", "warning", "cursor") == {
        "permission": "allow",
        "user_message": "warning",
        "agent_message": "warning",
    }


def test_opencode_edit_family_paths_are_normalized() -> None:
    """OpenCode's plugin payloads must reach the shared conflict checker."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for tool in ("multiedit", "patch", "apply_patch"):
        assert tool in module._MUTATION_TOOLS
        assert module._extract_paths(
            tool,
            {"file_path": "src/example.py"},
            {},
        ) == ["src/example.py"]


def test_codex_apply_patch_uses_patch_field() -> None:
    """Codex 0.146 sends patch text in ``tool_input.patch``."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    patch = "*** Begin Patch\n*** Update File: src/example.py\n*** End Patch"
    assert module._extract_paths("apply_patch", {"patch": patch}, {}) == [
        "src/example.py"
    ]


def test_cursor_mutation_tools_are_recognized() -> None:
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for tool in ("Edit", "Write", "StrReplace", "Delete", "EditNotebook"):
        assert tool in module._MUTATION_TOOLS


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


def test_issue42_mutation_tool_outside_git_repo_emits_no_approve(tmp_path: Path) -> None:
    """Regression for GitHub issue #42.

    The exact repro from the bug report:
      echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/x.txt","content":"hi"}}' \\
        | python conflict_check_helper.py

    Before the fix (origin/main): emitted ``permissionDecision: "approve"`` which fails
    Claude Code 2.1+ schema validation (enum must be "allow" | "deny" | "ask").
    After the fix: must emit empty stdout (silent approve, exit 0).

    Additionally validates that no valid hook output payload ever contains
    ``permissionDecision: "approve"`` anywhere in the JSON tree.
    """
    helper = _helper_path_for_test()
    # Use a tmp_path that is NOT inside any git repo so the helper
    # hits the "not inside a git repo; allowing" branch — the exact path
    # the issue reporter exercised. We ensure it is not accidentally inside
    # one by testing a path under /tmp directly (same as the issue's /tmp/x.txt).
    outside_git = "/tmp/issue42_regression_test.txt"

    tool_input = json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": outside_git, "content": "hi"},
    })
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=tool_input.encode(),
        capture_output=True,
        cwd="/tmp",  # run from /tmp — definitely not a git repo
    )
    assert result.returncode == 0, (
        f"expected exit 0 (allow); got {result.returncode}\n"
        f"stderr: {result.stderr.decode()}"
    )
    stdout_text = result.stdout.decode().strip()
    if stdout_text:
        # If any JSON was emitted, verify it never contains permissionDecision:"approve"
        # (the invalid value that triggered the Claude Code schema error in issue #42)
        for line in stdout_text.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            hook_out = payload.get("hookSpecificOutput", {})
            perm_decision = hook_out.get("permissionDecision")
            assert perm_decision != "approve", (
                f"permissionDecision='approve' emitted — this is the issue #42 bug.\n"
                f"Claude Code 2.1+ schema only accepts 'allow'|'deny'|'ask'.\n"
                f"Full payload: {payload}"
            )
            # Also assert no legacy top-level "decision" field
            assert "decision" not in payload, (
                f"Legacy top-level 'decision' field found — unsupported by Codex/modern runtimes.\n"
                f"Full payload: {payload}"
            )


def test_absolute_mutation_target_disambiguates_outer_payload_cwd(tmp_path: Path) -> None:
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer"
    target_repo = tmp_path / "target"
    for repo in (outer_repo, target_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    assert module._resolve_repo_root(
        {"cwd": str(outer_repo)}, {}, [str(target_repo / "changed.py")]
    ) == str(target_repo)
    assert module._resolve_repo_root(
        {"cwd": str(outer_repo)}, {},
        [str(target_repo / "changed.py"), str(outer_repo / "other.py")],
    ) is None


def test_mixed_absolute_and_relative_targets_outside_root_fail_open(tmp_path: Path) -> None:
    """Every target must normalize inside the selected repository."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "merge_train"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    payload = {"cwd": str(tmp_path)}
    paths = [str(repo / "changed.py"), "../outside/hook_install.py"]
    assert module._resolve_repo_root(payload, {}, paths) is None
    assert module._normalize_rel(paths[1], repo, [str(tmp_path)]) is None


def test_relative_mutation_outside_implicit_cwd_repo_uses_target_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative path escaping cwd must be checked in the target checkout."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer"
    target_repo = tmp_path / "target"
    for repo in (outer_repo, target_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    monkeypatch.chdir(outer_repo)
    target = "../target/changed.py"
    assert module._resolve_repo_root({}, {}, [target]) == str(target_repo)
    assert module._normalize_rel(
        target, target_repo, module._mutation_context_directories({}, {})
    ) == "changed.py"


def test_absolute_target_in_new_directory_checks_target_repo(tmp_path: Path) -> None:
    """An Add-file target identifies its checkout before its parent exists."""
    import os
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer"
    target_repo = tmp_path / "merge_train"
    for repo in (outer_repo, target_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    target = target_repo / "newdir" / "changed.py"
    assert not target.parent.exists()
    assert module._resolve_repo_root(
        {"cwd": str(outer_repo)}, {}, [str(target)]
    ) == str(target_repo)

    cache_file = Path(f"/tmp/merge_train_cache_{target_repo.name}.json")
    cache_file.unlink(missing_ok=True)
    test_home = tmp_path / "home"
    (test_home / "merge_train").mkdir(parents=True)
    (test_home / "merge_train" / "config.json").write_text(json.dumps({
        "default_enforcement": "warn",
        "repos": {str(target_repo): {"alias": "merge_train", "enforcement": "block"}},
    }))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = \"list\" ]; then\n"
        "  printf '%s\\n' '[{\"number\":123,\"headRefName\":\"other-branch\",\"headRefOid\":\"oid-123\"}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$2\" = \"diff\" ]; then\n"
        "  printf '%s\\n' 'newdir/changed.py'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_gh.chmod(0o755)
    payload = json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "new file\n"},
    })
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=payload.encode(),
        capture_output=True,
        cwd=str(outer_repo),
        env={
            **os.environ,
            "HOME": str(test_home),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0
    assert "CONFLICT" in result.stderr.decode()
    output = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "PR#123" in output["systemMessage"]


def test_notebook_edit_extracts_notebook_path(tmp_path: Path) -> None:
    """Claude NotebookEdit identifies its mutation target as notebook_path."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    notebook = str(tmp_path / "analysis.ipynb")
    assert module._extract_paths(
        "NotebookEdit", {"notebook_path": notebook, "new_source": "x"}, {}
    ) == [notebook]


def test_resolve_repo_root_uses_nested_exec_command_workdir(tmp_path: Path) -> None:
    """A nested command's explicit workdir wins over the outer non-repo cwd."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "nested-command-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = (
        'await tools.exec_command({cmd: "git status", '
        f'workdir: "{repo}"' + '});'
    )
    payload = {"code": source, "cwd": str(tmp_path)}

    assert module._resolve_repo_root(payload, {}, []) == str(repo)
    # A global hook can also receive only the outer cwd plus an absolute
    # mutation target; that target identifies the checkout without guessing.
    assert module._resolve_repo_root(
        {"cwd": str(tmp_path)}, {}, [str(repo / "target.py")]
    ) == str(repo)


def test_distinct_nested_dirs_in_one_repo_make_relative_target_ambiguous(
    tmp_path: Path,
) -> None:
    """Same-repo nested dirs cannot pick an arbitrary same-named file."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "nested-repo"
    (repo / "left").mkdir(parents=True)
    (repo / "right").mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = (
        f'await tools.exec_command({{cmd: "edit", workdir: "{repo / "left"}"}}); '
        f'await tools.exec_command({{cmd: "edit", workdir: "{repo / "right"}"}});'
    )
    payload = {"code": source, "cwd": str(repo)}

    assert module._mutation_context_directories(payload, {}) == []
    assert module._resolve_repo_root(payload, {}, ["shared.py"]) is None


def test_explicit_and_implicit_nested_calls_are_ambiguous_in_both_orders(
    tmp_path: Path,
) -> None:
    """An omitted workdir means outer cwd, not the previous call's cwd."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    explicit = f'tools.exec_command({{cmd: "edit", workdir: "{nested}"}})'
    implicit = 'tools.exec_command({cmd: "edit"})'
    for source in (f"{explicit}; {implicit};", f"{implicit}; {explicit};"):
        payload = {"code": source, "cwd": str(repo)}
        assert module._mutation_context_directories(payload, {}) == []
        assert module._resolve_repo_root(payload, {}, ["hook_install.py"]) is None


def test_relative_mutation_under_nested_workdir_uses_repo_relative_path(tmp_path: Path) -> None:
    """A relative mutation is rooted at the nested command's workdir."""
    import os
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "merge_train"
    package = repo / "pkg"
    package.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)
    test_home = tmp_path / "home"
    (test_home / "merge_train").mkdir(parents=True)
    (test_home / "merge_train" / "config.json").write_text(json.dumps({
        "default_enforcement": "warn",
        "repos": {str(repo): {"alias": "merge_train", "enforcement": "block"}},
    }))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = \"list\" ]; then\n"
        "  printf '%s\\n' '[{\"number\":123,\"headRefName\":\"other-branch\",\"headRefOid\":\"oid-123\"}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$2\" = \"diff\" ]; then\n"
        "  printf '%s\\n' 'pkg/changed.py'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_gh.chmod(0o755)
    payload = json.dumps({
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "code": (
            'await tools.exec_command({cmd: "git status", '
            f'workdir: "{package}"' + '});'
        ),
        "tool_input": {"file_path": "changed.py", "content": "new file\n"},
    })
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=payload.encode(),
        capture_output=True,
        cwd=str(tmp_path),
        env={
            **os.environ,
            "HOME": str(test_home),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0
    assert "CONFLICT" in result.stderr.decode(), (
        f"stdout={result.stdout.decode()!r} stderr={result.stderr.decode()!r}"
    )
    output = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "PR#123" in output["systemMessage"]


def test_nested_literal_workdir_does_not_override_absolute_target_repo(tmp_path: Path) -> None:
    """An absolute mutation target identifies its repo over nested context."""
    import os
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer"
    target_repo = tmp_path / "target"
    for repo in (outer_repo, target_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    target = target_repo / "changed.py"
    Path(f"/tmp/merge_train_cache_{target_repo.name}.json").unlink(missing_ok=True)
    test_home = tmp_path / "home"
    (test_home / "merge_train").mkdir(parents=True)
    (test_home / "merge_train" / "config.json").write_text(json.dumps({
        "default_enforcement": "warn",
        "repos": {
            str(outer_repo): {"alias": "outer", "enforcement": "block"},
            str(target_repo): {"alias": "merge_train", "enforcement": "block"},
        },
    }))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = \"list\" ]; then\n"
        "  printf '%s\\n' '[{\"number\":123,\"headRefName\":\"other-branch\",\"headRefOid\":\"oid-123\"}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$2\" = \"diff\" ]; then\n"
        "  printf '%s\\n' 'changed.py'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_gh.chmod(0o755)
    payload = json.dumps({
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "code": (
            'await tools.exec_command({cmd: "git status", '
            f'workdir: "{outer_repo}"' + '});'
        ),
        "tool_input": {"file_path": str(target), "content": "new file\n"},
    })
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=payload.encode(),
        capture_output=True,
        cwd=str(tmp_path),
        env={
            **os.environ,
            "HOME": str(test_home),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0
    assert "CONFLICT" in result.stderr.decode()
    output = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "PR#123" in output["systemMessage"]


def test_relative_target_escaping_nested_workdir_uses_target_repo(tmp_path: Path) -> None:
    """Relative targets are resolved from workdir before selecting the repo."""
    import os
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    workdir_repo = tmp_path / "workdir-repo"
    target_repo = tmp_path / "target-repo"
    for repo in (workdir_repo, target_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (target_repo / "target").mkdir()
    target = target_repo / "target" / "changed.py"
    Path(f"/tmp/merge_train_cache_{target_repo.name}.json").unlink(missing_ok=True)
    test_home = tmp_path / "home"
    (test_home / "merge_train").mkdir(parents=True)
    (test_home / "merge_train" / "config.json").write_text(json.dumps({
        "default_enforcement": "warn",
        "repos": {str(target_repo): {"alias": "merge_train", "enforcement": "block"}},
    }))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = \"list\" ]; then\n"
        "  printf '%s\\n' '[{\"number\":123,\"headRefName\":\"other-branch\",\"headRefOid\":\"oid-123\"}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$2\" = \"diff\" ]; then\n"
        "  printf '%s\\n' 'target/changed.py'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_gh.chmod(0o755)

    for file_path in ("../target-repo/target/changed.py", str(target)):
        payload = json.dumps({
            "tool_name": "Write",
            "cwd": str(tmp_path),
            "code": (
                'await tools.exec_command({cmd: "git status", '
                f'workdir: "{workdir_repo}"' + '});'
            ),
            "tool_input": {"file_path": file_path, "content": "new file\\n"},
        })
        result = subprocess.run(
            [sys.executable, str(helper)],
            input=payload.encode(),
            capture_output=True,
            cwd=str(tmp_path),
            env={
                **os.environ,
                "HOME": str(test_home),
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
            },
        )
        assert result.returncode == 0
        assert "CONFLICT" in result.stderr.decode(), (
            f"file_path={file_path!r} stdout={result.stdout.decode()!r} "
            f"stderr={result.stderr.decode()!r}"
        )
        output = json.loads(result.stdout.decode().strip().splitlines()[-1])
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "PR#123" in output["systemMessage"]


def test_nested_exec_workdir_ignores_cmd_comments_and_nested_properties(tmp_path: Path) -> None:
    """Only a top-level exec argument can identify the nested command repo."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "actual-repo"
    decoy = tmp_path / "decoy"
    repo.mkdir()
    decoy.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    cmd_decoy = (
        'await tools.exec_command({cmd: \'echo workdir: "' + str(decoy) + '\', '
        'workdir: "' + str(repo) + '"});'
    )
    comment_decoy = (
        'await tools.exec_command({cmd: "git status", '
        '/* workdir: "' + str(decoy) + '" */ workdir: "' + str(repo) + '" '
        '/* trailing comment */});'
    )
    nested_decoy = (
        'await tools.exec_command({options: {workdir: "' + str(decoy) + '"}, '
        'cmd: "git status", workdir: "' + str(repo) + '"});'
    )

    for source in (cmd_decoy, comment_decoy, nested_decoy):
        assert module._literal_exec_command_workdirs(source) == [str(repo)]
        assert module._resolve_repo_root(
            {"code": source, "cwd": str(tmp_path)}, {}, []
        ) == str(repo)

    # Expressions stay out of scope, and distinct actual call roots stay ambiguous.
    assert module._literal_exec_command_workdirs(
        'await tools.exec_command({workdir: repo_path});'
    ) == []
    assert module._literal_exec_command_workdirs(
        'await tools.exec_command({workdir: (globalThis.sideEffect = true)});'
    ) == []
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init", str(other_repo)], check=True, capture_output=True)
    ambiguous = (
        'await tools.exec_command({workdir: "' + str(repo) + '"}); '
        'await tools.exec_command({workdir: "' + str(other_repo) + '"});'
    )
    assert module._resolve_repo_root(
        {"code": ambiguous, "cwd": str(tmp_path)}, {}, []
    ) is None


def test_resolve_repo_root_keeps_true_nonrepo_silent(tmp_path: Path) -> None:
    """Explicit non-repo context must not fall back to a different cwd."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    payload = {"cwd": str(tmp_path)}
    assert module._resolve_repo_root(payload, {}, []) is None


def test_unresolved_nested_exec_workdir_blocks_outer_repo_fallback(tmp_path: Path) -> None:
    """An unparseable nested context must not attribute the outer repo."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer-repo"
    outer_repo.mkdir()
    subprocess.run(["git", "init", str(outer_repo)], check=True, capture_output=True)
    source = 'await tools.exec_command({cmd: "git status", workdir: repo_path});'

    assert module._resolve_repo_root(
        {"code": source, "cwd": str(outer_repo)}, {}, []
    ) is None
    assert module._resolve_repo_root(
        {"code": source, "cwd": str(outer_repo)}, {},
        [str(outer_repo / "target.py")],
    ) == str(outer_repo)


def test_duplicate_nested_exec_workdir_blocks_outer_repo_fallback(tmp_path: Path) -> None:
    """Duplicate JS keys are ambiguous because runtime semantics use the last."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer-repo"
    first_repo = tmp_path / "first-repo"
    last_repo = tmp_path / "last-repo"
    for repo in (outer_repo, first_repo, last_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = (
        'await tools.exec_command({cmd: "git status", '
        f'workdir: "{first_repo}", workdir: "{last_repo}"' + '});'
    )

    assert module._resolve_repo_root(
        {"code": source, "cwd": str(outer_repo)}, {}, []
    ) is None


def test_shorthand_nested_exec_workdir_blocks_outer_repo_fallback(tmp_path: Path) -> None:
    """JS shorthand workdir is unresolved and cannot use the hook cwd."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer-repo"
    outer_repo.mkdir()
    subprocess.run(["git", "init", str(outer_repo)], check=True, capture_output=True)
    source = 'await tools.exec_command({cmd: "git status", workdir});'

    assert module._resolve_repo_root(
        {"code": source, "cwd": str(outer_repo)}, {}, []
    ) is None


def test_computed_workdir_and_spread_block_outer_repo_fallback(tmp_path: Path) -> None:
    """Computed keys and spreads can supply a workdir and stay ambiguous."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer-repo"
    explicit_repo = tmp_path / "explicit-repo"
    for repo in (outer_repo, explicit_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    sources = (
        f'await tools.exec_command({{cmd: "git status", ["workdir"]: "{explicit_repo}"}});',
        f'await tools.exec_command({{cmd: "git status", ...options, workdir: "{explicit_repo}"}});',
    )
    for source in sources:
        assert module._resolve_repo_root(
            {"code": source, "cwd": str(outer_repo)}, {}, []
        ) is None


def test_exec_command_options_argument_blocks_outer_repo_fallback(tmp_path: Path) -> None:
    """A named options object may carry a dynamic workdir expression."""
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    outer_repo = tmp_path / "outer-repo"
    outer_repo.mkdir()
    subprocess.run(["git", "init", str(outer_repo)], check=True, capture_output=True)
    source = 'const options = {workdir: target}; await tools.exec_command(options);'

    assert module._resolve_repo_root(
        {"code": source, "cwd": str(outer_repo)}, {}, []
    ) is None



def test_issue42_decision_payload_never_emits_approve_as_permission_decision() -> None:
    """Unit-level regression for issue #42: _decision_payload must never set
    hookSpecificOutput.permissionDecision to 'approve'.

    Claude Code 2.1+ validates permissionDecision strictly as 'allow'|'deny'|'ask'.
    The old _DECISION_MAP mapped 'allow'/'warn' → 'approve' which caused:
      'Hook JSON output validation failed — (root): Invalid input'
    on every file write in sessions with merge_train hooks installed.
    """
    import importlib.util

    helper = _helper_path_for_test()
    spec = importlib.util.spec_from_file_location("conflict_check_helper", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    VALID_PERMISSION_DECISIONS = {"allow", "deny", "ask", None}

    for internal_decision in ("allow", "warn", "approve", "deny", "block"):
        p = module._decision_payload(internal_decision, f"test for {internal_decision!r}")
        hook_out = p.get("hookSpecificOutput", {})
        perm_decision = hook_out.get("permissionDecision")
        assert perm_decision in VALID_PERMISSION_DECISIONS, (
            f"_decision_payload('{internal_decision}', ...) emitted "
            f"permissionDecision={perm_decision!r} which is not in the "
            f"Claude Code 2.1+ schema enum {{allow|deny|ask}}.\n"
            f"This is the issue #42 bug. Full payload: {p}"
        )


def test_warn_only_conflict_exits_zero_with_context(tmp_path: Path) -> None:
    """Warn-only conflicts must not make the hook itself look failed.

    Regression for: warn-only conflicts emitted legacy ``decision:"approve"``
    and non-zero exit noise. Current runtimes accept ``systemMessage`` plus
    hook-specific additional context for non-blocking warnings.
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
    assert "decision" not in payload
    assert "systemMessage" in payload
    assert payload["hookSpecificOutput"]["additionalContext"] == payload["systemMessage"]

    # CRITICAL: the FIRST line of stderr is what Claude Code surfaces in the
    # TUI "hook error" banner. It MUST contain "CONFLICT" — not a generic
    # "checking conflicts..." status message — otherwise the user sees noise
    # instead of the actual warning.
    stderr_lines = [ln for ln in result.stderr.decode().splitlines() if ln.strip()]
    assert stderr_lines, "expected non-empty stderr (conflict banner)"
    first_line = stderr_lines[0]
    assert "CONFLICT" in first_line, (
        f"first stderr line must contain 'CONFLICT' (this is the TUI banner); "
        f"got: {first_line!r}\nfull stderr: {result.stderr.decode()!r}"
    )


def _make_counting_gh(tmp_path: Path, prs: list) -> Path:
    """Fake ``gh`` that lists ``prs`` and counts ``pr diff`` invocations.

    ``prs`` is a list of ``{"number": N, "headRefName": "b", "headRefOid": "o",
    "files": ["a.py"]}``.
    """
    import json as _json

    counter = tmp_path / "gh-diff-calls"
    counter.write_text("")
    fake_gh = tmp_path / "gh"
    spec = _json.dumps({str(p["number"]): p for p in prs})
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"spec = json.loads({spec!r})\n"
        f"counter = {str(counter)!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'list']:\n"
        "    print(json.dumps([{ 'number': int(k), 'headRefName': v['headRefName'],\n"
        "                        'headRefOid': v['headRefOid'] } for k, v in spec.items()]))\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['pr', 'diff']:\n"
        # Append one line per call: the helper fetches diffs concurrently, so a
        # read-modify-write counter loses updates and makes the test flaky.
        "    open(counter, 'a').write(args[2] + '\\n')\n"
        "    print('\\n'.join(spec[args[2]]['files']))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n"
    )
    fake_gh.chmod(0o755)
    return counter


def test_failed_pr_list_does_not_deny_from_expired_cache(tmp_path: Path) -> None:
    import os
    import time

    helper = _helper_path_for_test()
    repo = tmp_path / "expiredcache"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/expiredcache.git"],
        cwd=str(repo), check=True, capture_output=True,
    )
    cache_file = Path(f"/tmp/merge_train_cache_{repo.name}.json")
    cache_file.write_text(json.dumps({
        "timestamp": time.time() - 10_000,
        "prs": {"91": {
            "branch": "old-conflict", "oid": "old", "files": ["target.py"],
            "symbols": {},
        }},
    }))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\nexit 1\n")
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "target.py")},
    }).encode()
    result = subprocess.run(
        [sys.executable, str(helper)], input=payload, capture_output=True,
        cwd=str(repo), env=env,
    )

    assert result.returncode == 0
    assert "deny" not in result.stdout.decode()
    assert "allow" in result.stdout.decode()
    cache_file.unlink(missing_ok=True)


def test_pr_cache_reuses_files_until_head_sha_changes(tmp_path: Path) -> None:
    """The hook must not re-run one `gh pr diff` per open PR on every call.

    Regression: with ~100 open PRs the serial diff loop took ~13-15s, so Codex
    killed the hook at its 10s bound and reported `hook exited with code 1`
    while the conflict check never ran. Reuse must be keyed on the PR head SHA
    so an unchanged PR costs zero diffs and a pushed PR refreshes exactly once.
    """
    import os

    helper = _helper_path_for_test()
    repo = tmp_path / "cacheprobe"
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/cacheprobe.git"],
        cwd=str(repo), check=True, capture_output=True,
    )

    prs = [
        {"number": 11, "headRefName": "feat-a", "headRefOid": "aaa", "files": ["foo.py"]},
        {"number": 12, "headRefName": "feat-b", "headRefOid": "bbb", "files": ["bar.py"]},
    ]
    counter = _make_counting_gh(tmp_path, prs)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / "foo.py")},
    }).encode()

    def _run():
        return subprocess.run(
            [sys.executable, str(helper)],
            input=payload, capture_output=True, cwd=str(repo), env=env,
        )

    def _diff_calls() -> int:
        return len([ln for ln in counter.read_text().splitlines() if ln.strip()])

    first = _run()
    assert first.returncode == 0
    assert _diff_calls() == 2, "cold run must diff each foreign PR once"
    assert "PR#11" in first.stdout.decode()

    second = _run()
    assert second.returncode == 0
    assert _diff_calls() == 2, (
        "warm run must reuse cached files when head SHAs are unchanged; "
        f"expected no new diffs, saw {_diff_calls()}"
    )
    assert "PR#11" in second.stdout.decode(), "warm run must still report the conflict"

    # One PR moves: only that PR is re-diffed.
    prs[1]["headRefOid"] = "ccc"
    _make_counting_gh(tmp_path, prs)
    counter.write_text(counter.read_text())
    third = _run()
    assert third.returncode == 0
    assert _diff_calls() == 1, (
        f"only the PR whose head SHA moved may be re-diffed; saw {counter.read_text()}"
    )


def test_opencode_apply_patch_text_cannot_override_target_repo_with_nested_workdir(
    tmp_path: Path,
) -> None:
    """Patch contents are mutation data, not executable orchestration context."""
    import os

    helper = _helper_path_for_test()
    target_repo = tmp_path / "target-repo"
    nested_repo = tmp_path / "nested-repo"
    for repo in (target_repo, nested_repo):
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/test/{repo.name}.git"],
            cwd=str(repo), check=True, capture_output=True,
        )

    Path(f"/tmp/merge_train_cache_{target_repo.name}.json").unlink(missing_ok=True)
    test_home = tmp_path / "home"
    config_dir = test_home / "merge_train"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({
        "default_enforcement": "warn",
        "repos": {str(target_repo): {"alias": "target", "enforcement": "block"}},
    }))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = \"list\" ]; then\n"
        "  printf '%s\\n' '[{\"number\":123,\"headRefName\":\"other-branch\",\"headRefOid\":\"oid-123\"}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$2\" = \"diff\" ]; then\n"
        "  printf '%s\\n' 'changed.py'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_gh.chmod(0o755)

    # This mirrors opencode-conflict-plugin.js: patchText is passed as the
    # apply_patch command field. The added JS happens to contain an
    # orchestration helper call naming another repository's workdir.
    patch_text = (
        "*** Begin Patch\n*** Update File: changed.py\n"
        "+await tools.exec_command({cmd: \"git status\", "
        f"workdir: \"{nested_repo}\"" + "});\n"
        "*** End Patch\n"
    )
    payload = json.dumps({
        "tool_name": "apply_patch",
        "tool_input": {"command": patch_text},
    })
    result = subprocess.run(
        [sys.executable, str(helper)], input=payload.encode(), capture_output=True,
        cwd=str(target_repo),
        env={**os.environ, "HOME": str(test_home), "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0
    assert "CONFLICT" in result.stderr.decode(), (
        f"stdout={result.stdout.decode()!r} stderr={result.stderr.decode()!r}"
    )
    decision = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "PR#123" in decision["systemMessage"]


def test_valid_json_with_unexpected_tool_input_shape_allows_cleanly(tmp_path: Path) -> None:
    """Malformed hook fields must not leave Codex with a failed helper exit."""
    import os

    helper = _helper_path_for_test()
    payload = json.dumps({"tool_name": "Edit", "tool_input": ["unexpected"]})
    result = subprocess.run(
        [sys.executable, str(helper), "--runtime", "codex"],
        input=payload.encode(), capture_output=True, cwd=str(tmp_path),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0, result.stderr.decode()
    output = json.loads(result.stdout.decode().strip().splitlines()[-1])
    hook_output = output["hookSpecificOutput"]
    assert hook_output.get("permissionDecision") != "deny"
    assert "helper failed" in hook_output["additionalContext"]
    assert "helper failed" in output["systemMessage"]
