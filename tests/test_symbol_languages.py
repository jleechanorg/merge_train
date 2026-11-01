"""Symbol-level precision must not be Python-only.

``_collect_conflicts_for_path`` gated the *local* side of the symbol comparison on
``is_python_path``, while the *PR* side already resolved symbols through
``lang_extractors`` for every supported language. The asymmetry meant a ``.tsx``,
``.ts``, ``.go`` or ``.md`` edit could never be narrowed to a symbol even when the
open PR's touched symbols were known: 44 of 75 post-install whole-file warnings
were non-Python files.

These tests pin the widened gate and, just as importantly, the naming parity it
depends on: if the local side and the PR side named the same symbol differently
the intersection would be empty, ``whole_file_lock`` would already be cleared,
and the hook would emit **nothing** — the silent-suppression failure mode.
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

TS_SOURCE = """export function alpha(x: number): number {
  return x + 1;
}

export function beta(y: number): number {
  return y * 2;
}
"""

GO_SOURCE = """package main

func alpha(x int) int {
	return x + 1
}

func beta(y int) int {
	return y * 2
}
"""

MD_SOURCE = """# Title

## Alpha

alpha body

## Beta

beta body
"""

# Per language: (relative path, file body, anchor inside the PR-touched symbol,
# anchor inside an untouched symbol, a PR diff whose hunk lands in `beta`).
CASES = [
    (
        "typescript",
        "mod.ts",
        TS_SOURCE,
        "  return y * 2;",
        "  return x + 1;",
        """diff --git a/mod.ts b/mod.ts
--- a/mod.ts
+++ b/mod.ts
@@ -5,3 +5,3 @@
 export function beta(y: number): number {
-  return y * 2;
+  return y * 3;
 }
""",
        "beta",
    ),
    (
        "go",
        "mod.go",
        GO_SOURCE,
        "\treturn y * 2",
        "\treturn x + 1",
        """diff --git a/mod.go b/mod.go
--- a/mod.go
+++ b/mod.go
@@ -7,3 +7,3 @@
 func beta(y int) int {
-	return y * 2
+	return y * 3
 }
""",
        "beta",
    ),
]


def _helper_path_for_test() -> Path:
    if HELPER_PATH.is_file():
        return HELPER_PATH
    installed = Path.home() / ".local" / "bin" / "conflict_check_helper.py"
    if installed.is_file():
        return installed
    pytest.skip("conflict_check_helper.py not found")


def _make_repo(tmp_path: Path, name: str, rel_path: str, body: str) -> Path:
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
        ["git", "remote", "add", "origin", "https://github.com/jleechanorg/langprobe.git"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / rel_path).write_text(body)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    Path(f"/tmp/merge_train_cache_{repo.name}.json").unlink(missing_ok=True)
    return repo


def _fake_gh(tmp_path: Path, rel_path: str, diff: str, head_body: str) -> dict:
    script = tmp_path / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "pr_list = '[{\"number\": 900, \"headRefName\": \"other\", \"headRefOid\": \"abc\"}]'\n"
        f"diff = {diff!r}\n"
        f"head = {head_body!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'list']:\n"
        "    print(pr_list); raise SystemExit(0)\n"
        "if args[:2] == ['pr', 'diff'] and '--name-only' in args:\n"
        f"    print({rel_path!r}); raise SystemExit(0)\n"
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


def _run(repo: Path, payload: dict, env: dict) -> str:
    result = subprocess.run(
        [sys.executable, str(_helper_path_for_test())],
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode()


def _edit(repo: Path, rel_path: str, old: str, new: str = "    changed") -> dict:
    return {
        "tool_name": "Edit",
        "cwd": str(repo),
        "tool_input": {
            "file_path": str(repo / rel_path),
            "old_string": old,
            "new_string": new,
        },
    }


@pytest.mark.parametrize("lang,rel_path,body,touched,untouched,diff,symbol", CASES)
def test_non_python_edit_in_touched_symbol_names_it(
    tmp_path: Path, lang: str, rel_path: str, body: str, touched: str, untouched: str, diff: str, symbol: str
) -> None:
    """A TypeScript or Go edit lands in the same symbol the open PR touches."""
    repo = _make_repo(tmp_path, f"lang_{lang}", rel_path, body)
    env = _fake_gh(tmp_path, rel_path, diff, body)
    stdout = _run(repo, _edit(repo, rel_path, touched), env)

    assert stdout.strip(), f"{lang}: expected a conflict warning"
    assert f"symbols in '{rel_path}'" in stdout, stdout
    assert symbol in stdout, stdout


@pytest.mark.parametrize("lang,rel_path,body,touched,untouched,diff,symbol", CASES)
def test_non_python_edit_in_untouched_symbol_stays_silent(
    tmp_path: Path, lang: str, rel_path: str, body: str, touched: str, untouched: str, diff: str, symbol: str
) -> None:
    """The precision half: an edit to a different symbol in the same file is silent."""
    repo = _make_repo(tmp_path, f"langsilent_{lang}", rel_path, body)
    env = _fake_gh(tmp_path, rel_path, diff, body)
    stdout = _run(repo, _edit(repo, rel_path, untouched), env)

    assert stdout.strip() == "", f"{lang}: unrelated symbol must not warn; got {stdout!r}"


def test_markdown_heading_gets_symbol_precision(tmp_path: Path) -> None:
    """Markdown resolves through its own heading symbols, matching the PR side."""
    rel_path = "doc.md"
    diff = """diff --git a/doc.md b/doc.md
--- a/doc.md
+++ b/doc.md
@@ -7,2 +7,2 @@
 ## Beta
-before text
+beta body changed
"""
    repo = _make_repo(tmp_path, "langmd", rel_path, MD_SOURCE)
    env = _fake_gh(tmp_path, rel_path, diff, MD_SOURCE)
    stdout = _run(repo, _edit(repo, rel_path, "beta body"), env)

    assert stdout.strip(), "markdown: expected a conflict warning"
    assert "symbols in 'doc.md'" in stdout, stdout
    assert "beta" in stdout.lower(), stdout


def test_unsupported_extension_stays_whole_file(tmp_path: Path) -> None:
    """A language with no extractor keeps the honest whole-file fallback."""
    rel_path = "data.txt"
    diff = """diff --git a/data.txt b/data.txt
--- a/data.txt
+++ b/data.txt
@@ -1,2 +1,2 @@
 line one
-line two
+line three
"""
    repo = _make_repo(tmp_path, "langtxt", rel_path, "line one\nline two\n")
    env = _fake_gh(tmp_path, rel_path, diff, "line one\nline three\n")
    stdout = _run(repo, _edit(repo, rel_path, "line two"), env)

    assert f"whole-file '{rel_path}'" in stdout, stdout


def test_local_and_pr_side_symbol_names_agree_per_language() -> None:
    """Both sides must extract identical names, or the overlap is silently empty.

    ``_collect_conflicts_for_path`` clears ``whole_file_lock`` as soon as the local
    side finds a symbol. If the PR side used a different naming scheme, the
    intersection would be empty and the hook would emit nothing at all instead of
    warning. This test is the guard for that failure mode.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    try:
        from merge_train.lang_extractors import extract_symbols_for_language
        from merge_train.symbols import extract_markdown_symbols, language_for_path

        samples = {
            "mod.ts": TS_SOURCE,
            "mod.go": GO_SOURCE,
        }
        for rel_path, body in samples.items():
            language = language_for_path(rel_path)
            assert language is not None
            local = {s.name for s in extract_symbols_for_language(body, language)}
            assert local, f"{rel_path}: extractor returned no symbols"
            assert "beta" in local, f"{rel_path}: expected beta, got {local}"

        headings = {s.name for s in extract_markdown_symbols(MD_SOURCE, file_stem="doc")}
        assert any("beta" in name for name in headings), headings
    finally:
        sys.path.remove(str(repo_root))


# A long body whose last line is within the diff's 3-line context of the NEXT
# declaration. Both sides must agree on the extended range, or the PR side
# attributes the hunk to `beta` while the local side attributes the edit to
# `alpha` — the intersection is then empty and the hook emits nothing.
LONG_TS = """export function alpha(x: number): number {
  const a = x + 1;
  const b = a * 2;
  const c = b - 1;
  const d = c / 2;
  const e = d + 3;
  const f = e * e;
  const g = f - 4;
  return g;
}

export function beta(y: number): number {
  return y * 2;
}
"""

LONG_TS_DIFF = """diff --git a/long.ts b/long.ts
--- a/long.ts
+++ b/long.ts
@@ -6,7 +6,7 @@
   const e = d + 3;
   const f = e * e;
   const g = f - 4;
-  return g;
+  return g + 1;
 }
 
 export function beta(y: number): number {
"""


def test_body_edit_next_to_another_declaration_still_warns(tmp_path: Path) -> None:
    """Regression: a body edit must not be silenced by neighbour attribution.

    The PR's hunk context reaches ``beta``'s declaration but never ``alpha``'s, so
    a PR side using raw declaration-only ranges resolves ``{beta}`` while the local
    side resolves ``{alpha}``. Empty intersection plus a cleared ``whole_file_lock``
    means no warning at all - a real same-line conflict hidden. Both sides now share
    one range normaliser, so the hunk overlaps ``alpha`` as well.
    """
    rel_path = "long.ts"
    repo = _make_repo(tmp_path, "langneighbour", rel_path, LONG_TS)
    env = _fake_gh(tmp_path, rel_path, LONG_TS_DIFF, LONG_TS)
    stdout = _run(repo, _edit(repo, rel_path, "  return g;", "  return g + 1;"), env)

    assert stdout.strip(), "body edit was silently suppressed (neighbour attribution)"
    assert f"symbols in '{rel_path}'" in stdout, stdout
    assert "alpha" in stdout, stdout


def test_both_sides_share_one_range_normaliser() -> None:
    """The local and PR sides must normalise ranges through the same function.

    A duplicated normaliser is how the two sides drift apart; this asserts the
    shared helper exists and is the one symbol_discovery imports.
    """
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    try:
        from merge_train import symbol_discovery, symbols

        assert hasattr(symbols, "extend_declaration_only_symbols")
        source = Path(symbol_discovery.__file__).read_text()
        assert "extend_declaration_only_symbols" in source, (
            "symbol_discovery must normalise PR-side ranges with the shared helper"
        )
    finally:
        sys.path.remove(str(repo_root))
