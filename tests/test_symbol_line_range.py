"""Symbol-level conflict warnings must fire from real runtime payloads.

Regression under guard
----------------------
``conflict_check_helper.py`` only ever entered its symbol-level branch when a
runtime supplied ``StartLine``/``EndLine`` in the tool input. Claude Code's
``Edit`` sends ``old_string``/``new_string`` and ``Write`` sends ``content``;
Codex's ``apply_patch`` sends a patch body. No shipping runtime sends line
numbers, so every real warning degraded to whole-file: 0 of 1,972 conflict
warnings recovered from agent transcripts were symbol-level.

These tests pin the derived-line-range behaviour that makes the symbol branch
reachable, and keep the whole-file fallback honest when the anchor is
ambiguous or unresolvable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER_PATH = (
    Path(__file__).resolve().parents[1] / "merge_train" / "hooks" / "conflict_check_helper.py"
)

# ``alpha`` occupies lines 1-2, ``beta`` lines 5-6. The open PR touches beta
# only, so an edit anchored in alpha must stay silent and an edit anchored in
# beta must name exactly ``beta``.
MODULE = """def alpha(x):
    return x + 1


def beta(y):
    return y * 2
"""

MODULE_AT_HEAD = """def alpha(x):
    return x + 1


def beta(y):
    return y * 3
"""

PR_DIFF = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -5,2 +5,2 @@
 def beta(y):
-    return y * 2
+    return y * 3
"""


def _helper_path_for_test() -> Path:
    if HELPER_PATH.is_file():
        return HELPER_PATH
    installed = Path.home() / ".local" / "bin" / "conflict_check_helper.py"
    if installed.is_file():
        return installed
    pytest.skip("conflict_check_helper.py not found")


def _make_repo(tmp_path: Path, name: str, module: str = MODULE) -> Path:
    """Git repo with one python module and an origin remote the helper can read."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/jleechanorg/symbolrange.git"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "mod.py").write_text(module)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True
    )
    # The helper memoises PR data per repo name under /tmp; a stale entry from
    # an earlier run would mask the fake gh response.
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)
    return repo


def _fake_gh(tmp_path: Path, pr_number: int = 700, branch: str = "other") -> dict:
    """A ``gh`` that reports one open PR touching ``mod.py`` inside ``beta``."""
    pr_list = json.dumps([{"number": pr_number, "headRefName": branch, "headRefOid": "abc123"}])
    script = tmp_path / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"pr_list = {pr_list!r}\n"
        f"diff = {PR_DIFF!r}\n"
        f"head = {MODULE_AT_HEAD!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'list']:\n"
        "    print(pr_list); raise SystemExit(0)\n"
        "if args[:2] == ['pr', 'diff'] and '--name-only' in args:\n"
        "    print('mod.py'); raise SystemExit(0)\n"
        "if args[:2] == ['pr', 'diff']:\n"
        "    print(diff); raise SystemExit(0)\n"
        "if args[:2] == ['pr', 'view']:\n"
        "    print('other'); raise SystemExit(0)\n"
        "if args[:1] == ['api']:\n"
        "    print(head); raise SystemExit(0)\n"
        "raise SystemExit(0)\n"
    )
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def _run(repo: Path, payload: dict, env: dict) -> tuple[str, str, int]:
    result = subprocess.run(
        [sys.executable, str(_helper_path_for_test())],
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    return result.stdout.decode(), result.stderr.decode(), result.returncode


def _edit_payload(repo: Path, old: str, new: str = "    return y * 4", **extra) -> dict:
    return {
        "tool_name": "Edit",
        "cwd": str(repo),
        "tool_input": {
            "file_path": str(repo / "mod.py"),
            "old_string": old,
            "new_string": new,
            **extra,
        },
    }


def test_claude_edit_anchor_yields_symbol_level_warning(tmp_path: Path) -> None:
    """An unambiguous Claude Edit anchor inside a PR-touched symbol names it.

    ``Edit`` carries no line numbers, so the helper must locate ``old_string``
    in the on-disk file to learn which symbol the edit lands in.
    """
    repo = _make_repo(tmp_path, "symanchor")
    env = _fake_gh(tmp_path)
    stdout, stderr, rc = _run(repo, _edit_payload(repo, "    return y * 2"), env)

    assert rc == 0
    assert stdout.strip(), f"expected a conflict warning; stderr={stderr!r}"
    assert "symbols in 'mod.py'" in stdout, stdout
    assert "beta" in stdout, stdout
    assert "whole-file" not in stdout, f"symbol branch not reached: {stdout}"


def test_claude_edit_anchor_in_untouched_symbol_stays_silent(tmp_path: Path) -> None:
    """An anchor inside ``alpha`` does not collide with a PR touching ``beta``.

    This is the precision the whole feature exists for: without it every edit
    to the file is a false positive.
    """
    repo = _make_repo(tmp_path, "symsilent")
    env = _fake_gh(tmp_path)
    stdout, stderr, rc = _run(repo, _edit_payload(repo, "    return x + 1", "    return x + 9"), env)

    assert rc == 0
    assert stdout.strip() == "", f"unrelated symbol must not warn; got {stdout!r} stderr={stderr!r}"


def test_ambiguous_anchor_falls_back_to_whole_file(tmp_path: Path) -> None:
    """A repeated anchor cannot identify a symbol, so the warning stays whole-file."""
    module = """def alpha(x):
    pass


def beta(y):
    pass
"""
    repo = _make_repo(tmp_path, "symambiguous", module=module)
    env = _fake_gh(tmp_path)
    stdout, _stderr, rc = _run(repo, _edit_payload(repo, "    pass", "    return 1"), env)

    assert rc == 0
    assert "whole-file 'mod.py'" in stdout, stdout


def test_missing_anchor_falls_back_to_whole_file(tmp_path: Path) -> None:
    """An anchor that is not in the file must not suppress the warning."""
    repo = _make_repo(tmp_path, "symmissing")
    env = _fake_gh(tmp_path)
    stdout, _stderr, rc = _run(repo, _edit_payload(repo, "not in this file at all"), env)

    assert rc == 0
    assert "whole-file 'mod.py'" in stdout, stdout


def test_single_path_apply_patch_bare_hunk_yields_symbol_level_warning(tmp_path: Path) -> None:
    """Codex patches use bare ``@@`` markers, so the hunk body is the anchor."""
    repo = _make_repo(tmp_path, "sympatch")
    env = _fake_gh(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: mod.py\n"
        "@@\n"
        " def beta(y):\n"
        "-    return y * 2\n"
        "+    return y * 5\n"
        "*** End Patch\n"
    )
    payload = {"tool_name": "apply_patch", "cwd": str(repo), "tool_input": {"patch": patch}}
    stdout, stderr, rc = _run(repo, payload, env)

    assert rc == 0
    assert stdout.strip(), f"expected a conflict warning; stderr={stderr!r}"
    assert "symbols in 'mod.py'" in stdout, stdout
    assert "beta" in stdout, stdout


def test_multi_path_apply_patch_stays_whole_file(tmp_path: Path) -> None:
    """A patch touching several files has no single meaningful line range."""
    repo = _make_repo(tmp_path, "symmultipatch")
    (repo / "other.py").write_text("def gamma():\n    return 1\n")
    env = _fake_gh(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: mod.py\n"
        "@@\n"
        " def beta(y):\n"
        "-    return y * 2\n"
        "+    return y * 5\n"
        "*** Update File: other.py\n"
        "@@\n"
        " def gamma():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch\n"
    )
    payload = {"tool_name": "apply_patch", "cwd": str(repo), "tool_input": {"patch": patch}}
    stdout, _stderr, rc = _run(repo, payload, env)

    assert rc == 0
    assert "whole-file 'mod.py'" in stdout, stdout


def test_explicit_line_range_still_wins(tmp_path: Path) -> None:
    """Runtimes that do send line numbers keep their exact-range behaviour."""
    repo = _make_repo(tmp_path, "symexplicit")
    env = _fake_gh(tmp_path)
    stdout, _stderr, rc = _run(
        repo,
        _edit_payload(repo, "", "", StartLine=5, EndLine=6),
        env,
    )

    assert rc == 0
    assert "symbols in 'mod.py'" in stdout, stdout
    assert "beta" in stdout, stdout


def test_write_payload_stays_whole_file(tmp_path: Path) -> None:
    """``Write`` replaces the whole file, so it has no symbol-level precision."""
    repo = _make_repo(tmp_path, "symwrite")
    env = _fake_gh(tmp_path)
    payload = {
        "tool_name": "Write",
        "cwd": str(repo),
        "tool_input": {"file_path": str(repo / "mod.py"), "content": MODULE},
    }
    stdout, _stderr, rc = _run(repo, payload, env)

    assert rc == 0
    assert "whole-file 'mod.py'" in stdout, stdout
