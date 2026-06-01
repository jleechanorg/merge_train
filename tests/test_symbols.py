"""Tests for merge_train.symbols: AST extractor, diff hunk parser, git driver."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from merge_train.symbols import (
    HunkRange,
    Symbol,
    SymbolResolutionError,
    UnsupportedLanguageError,
    extract_symbols,
    extract_markdown_symbols,
    is_python_path,
    is_markdown_path,
    parse_hunks,
    resolve_touched_symbols,
    touched_symbols,
    touched_symbols_for_staged_file,
    _touched_markdown_symbols,
)


# --------------------------------------------------------------------------- #
# extract_symbols
# --------------------------------------------------------------------------- #


def test_extract_symbols_top_level_def():
    src = "def foo():\n    return 1\n"
    syms = extract_symbols(src)
    assert syms == [Symbol("foo", 1, 2)]


def test_extract_symbols_async_def():
    src = "async def foo():\n    return 1\n"
    syms = extract_symbols(src)
    assert syms[0].name == "foo"
    assert syms[0].start == 1


def test_extract_symbols_class_and_methods():
    src = (
        "class A:\n"
        "    def m1(self):\n"
        "        return 1\n"
        "    def m2(self):\n"
        "        return 2\n"
    )
    syms = extract_symbols(src)
    names = [s.name for s in syms]
    assert names == ["A", "A.m1", "A.m2"]


def test_extract_symbols_nested_def_not_emitted_separately():
    src = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner\n"
    )
    syms = extract_symbols(src)
    assert [s.name for s in syms] == ["outer"]


def test_extract_symbols_decorator_extends_range_upward():
    src = (
        "@decorator\n"
        "def foo():\n"
        "    return 1\n"
    )
    syms = extract_symbols(src)
    assert syms[0].name == "foo"
    assert syms[0].start == 1  # includes decorator line


def test_extract_symbols_multiple_top_level():
    src = (
        "def a():\n"
        "    pass\n"
        "\n"
        "def b():\n"
        "    pass\n"
        "\n"
        "class C:\n"
        "    pass\n"
    )
    syms = extract_symbols(src)
    names = [s.name for s in syms]
    assert names == ["a", "b", "C"]


def test_extract_symbols_empty_file():
    assert extract_symbols("") == []


def test_extract_symbols_only_module_level_code():
    src = "x = 1\ny = 2\n"
    assert extract_symbols(src) == []


def test_extract_symbols_syntax_error_propagates():
    with pytest.raises(SyntaxError):
        extract_symbols("def broken(:\n    pass\n")


def test_symbol_contains_line():
    s = Symbol("x", 5, 10)
    assert s.contains_line(5)
    assert s.contains_line(10)
    assert not s.contains_line(4)
    assert not s.contains_line(11)


def test_symbol_overlaps():
    s = Symbol("x", 5, 10)
    assert s.overlaps(5, 10)
    assert s.overlaps(1, 5)
    assert s.overlaps(10, 15)
    assert s.overlaps(1, 100)
    assert not s.overlaps(1, 4)
    assert not s.overlaps(11, 20)


# --------------------------------------------------------------------------- #
# parse_hunks
# --------------------------------------------------------------------------- #


def test_parse_hunks_simple_add():
    diff = "@@ -10,0 +11,3 @@\n+a\n+b\n+c\n"
    hunks = parse_hunks(diff)
    assert hunks == [HunkRange(start=11, end=13)]


def test_parse_hunks_single_line_default_count():
    diff = "@@ -5 +5 @@\n-old\n+new\n"
    hunks = parse_hunks(diff)
    assert hunks == [HunkRange(start=5, end=5)]


def test_parse_hunks_pure_deletion_widens():
    # new_count=0 => widen by 1 line so we catch adjacent symbol
    diff = "@@ -10,3 +9,0 @@\n-a\n-b\n-c\n"
    hunks = parse_hunks(diff)
    assert hunks == [HunkRange(start=9, end=10)]


def test_parse_hunks_multiple_hunks():
    diff = (
        "@@ -1,0 +1,1 @@\n+a\n"
        "@@ -10,0 +20,2 @@\n+b\n+c\n"
    )
    hunks = parse_hunks(diff)
    assert hunks == [
        HunkRange(start=1, end=1),
        HunkRange(start=20, end=21),
    ]


def test_parse_hunks_ignores_non_header_lines():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "index abc..def 100644\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,0 +1,1 @@\n+hello\n"
    )
    hunks = parse_hunks(diff)
    assert hunks == [HunkRange(start=1, end=1)]


def test_parse_hunks_empty_diff():
    assert parse_hunks("") == []


# --------------------------------------------------------------------------- #
# touched_symbols (function under test)
# --------------------------------------------------------------------------- #


def _src_two_funcs() -> str:
    return (
        "def alpha():\n"      # line 1-2
        "    return 1\n"
        "\n"                  # line 3
        "def beta():\n"       # line 4-5
        "    return 2\n"
    )


def test_touched_symbols_first_function_only():
    src = _src_two_funcs()
    diff = "@@ -2 +2 @@\n-    return 1\n+    return 11\n"
    assert touched_symbols(new_source=src, diff_text=diff) == {"alpha"}


def test_touched_symbols_second_function_only():
    src = _src_two_funcs()
    diff = "@@ -5 +5 @@\n-    return 2\n+    return 22\n"
    assert touched_symbols(new_source=src, diff_text=diff) == {"beta"}


def test_touched_symbols_both_when_both_hunks():
    src = _src_two_funcs()
    diff = (
        "@@ -2 +2 @@\n-    return 1\n+    return 11\n"
        "@@ -5 +5 @@\n-    return 2\n+    return 22\n"
    )
    assert touched_symbols(new_source=src, diff_text=diff) == {"alpha", "beta"}


def test_touched_symbols_empty_diff():
    assert touched_symbols(new_source=_src_two_funcs(), diff_text="") == set()


def test_touched_symbols_syntax_error_raises():
    with pytest.raises(SymbolResolutionError):
        touched_symbols(
            new_source="def broken(:\n    pass",
            diff_text="@@ -1 +1 @@\n+def broken(:\n",
        )


def test_touched_symbols_empty_symbols_nonempty_source_raises():
    with pytest.raises(SymbolResolutionError):
        touched_symbols(
            new_source="x = 1\ny = 2\n",
            diff_text="@@ -1 +1 @@\n-x = 1\n+x = 10\n",
        )


def test_touched_symbols_empty_source_empty_diff_ok():
    assert touched_symbols(new_source="", diff_text="") == set()


def test_resolve_touched_symbols_parse_failure_goes_to_fallback(git_repo: Path):
    src_broken = "def broken(:\n    pass\n"
    (git_repo / "broken.py").write_text(src_broken)
    _git(git_repo, "add", "broken.py")
    _git(git_repo, "commit", "-q", "-m", "init")

    src_still_broken = "def broken(:\n    pass\nx = 1\n"
    (git_repo / "broken.py").write_text(src_still_broken)
    _git(git_repo, "add", "broken.py")

    per_file, fallback = resolve_touched_symbols(["broken.py"], cwd=git_repo)
    assert "broken.py" not in per_file
    assert "broken.py" in fallback


def test_touched_symbols_class_method_only():
    src = (
        "class A:\n"          # 1
        "    def m1(self):\n" # 2-3
        "        return 1\n"
        "    def m2(self):\n" # 4-5
        "        return 2\n"
    )
    # Touch line 5 (inside m2)
    diff = "@@ -5 +5 @@\n-        return 2\n+        return 22\n"
    touched = touched_symbols(new_source=src, diff_text=diff)
    # Both A (the class) AND A.m2 (the method) contain line 5
    assert "A.m2" in touched
    assert "A" in touched
    assert "A.m1" not in touched


# --------------------------------------------------------------------------- #
# is_python_path
# --------------------------------------------------------------------------- #


def test_is_python_path():
    assert is_python_path("foo.py")
    assert is_python_path("a/b/c.py")
    assert not is_python_path("foo.txt")
    assert not is_python_path("foo")
    assert not is_python_path("foo.yaml")


# --------------------------------------------------------------------------- #
# git driver integration
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    return repo


def test_touched_symbols_for_staged_file_detects_changed_function(git_repo: Path):
    src_orig = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    (git_repo / "m.py").write_text(src_orig)
    _git(git_repo, "add", "m.py")
    _git(git_repo, "commit", "-q", "-m", "init")

    # Modify only beta
    src_new = src_orig.replace("    return 2\n", "    return 22\n")
    (git_repo / "m.py").write_text(src_new)
    _git(git_repo, "add", "m.py")

    touched = touched_symbols_for_staged_file("m.py", cwd=git_repo)
    assert touched == {"m.py:beta"}


def test_touched_symbols_for_staged_file_non_python_raises(git_repo: Path):
    (git_repo / "x.txt").write_text("hi")
    _git(git_repo, "add", "x.txt")
    with pytest.raises(UnsupportedLanguageError):
        touched_symbols_for_staged_file("x.txt", cwd=git_repo)


def test_resolve_touched_symbols_mixed_python_and_other(git_repo: Path):
    (git_repo / "m.py").write_text("def alpha():\n    return 1\n")
    (git_repo / "x.txt").write_text("hi\n")
    _git(git_repo, "add", "m.py", "x.txt")
    _git(git_repo, "commit", "-q", "-m", "init")

    (git_repo / "m.py").write_text("def alpha():\n    return 11\n")
    (git_repo / "x.txt").write_text("bye\n")
    _git(git_repo, "add", "m.py", "x.txt")

    per_file, fallback = resolve_touched_symbols(["m.py", "x.txt"], cwd=git_repo)
    assert per_file == {"m.py": {"m.py:alpha"}, "x.txt": {"file:x.txt"}}
    assert fallback == []


def test_resolve_touched_symbols_missing_file_falls_back(git_repo: Path):
    per_file, fallback = resolve_touched_symbols(["does_not_exist.py"], cwd=git_repo)
    # No staged diff for a nonexistent file => empty diff => empty set,
    # NOT fallback (this is "unchanged" not "untranslatable").
    assert per_file == {"does_not_exist.py": set()}
    assert fallback == []


def test_resolve_touched_symbols_outside_git_repo(tmp_path: Path):
    per_file, fallback = resolve_touched_symbols(["foo.py"], cwd=tmp_path)
    assert per_file == {}
    assert fallback == ["foo.py"]


# --------------------------------------------------------------------------- #
# Markdown symbol extraction
# --------------------------------------------------------------------------- #


def test_extract_markdown_symbols_basic():
    src = "# Title\n\n## slot-01\ncontent\n\n## slot-02\nmore\n"
    syms = extract_markdown_symbols(src)
    assert len(syms) == 2
    assert syms[0].name == "md:slot_01"
    assert syms[0].start == 3
    assert syms[1].name == "md:slot_02"
    assert syms[1].start == 6


def test_extract_markdown_symbols_with_file_stem():
    src = "# Title\n\n## slot-01\ncontent\n\n## slot-02\nmore\n"
    syms = extract_markdown_symbols(src, file_stem="shared_plan")
    assert len(syms) == 2
    assert syms[0].name == "md:shared_plan.slot_01"
    assert syms[1].name == "md:shared_plan.slot_02"


def test_extract_markdown_symbols_h2_only():
    src = "# Title\n\n## alpha\na\n\n### sub\nb\n\n## beta\nc\n"
    syms = extract_markdown_symbols(src)
    names = [s.name for s in syms]
    assert "md:alpha" in names
    assert "md:beta" in names


def test_extract_markdown_symbols_range_to_next_heading():
    src = "## slot-01\na\nb\n## slot-02\nc\n"
    syms = extract_markdown_symbols(src)
    assert syms[0].end == 3
    assert syms[1].start == 4


def test_is_markdown_path():
    assert is_markdown_path("foo.md")
    assert not is_markdown_path("foo.py")
    assert not is_markdown_path("foo.txt")


def test_touched_markdown_symbols_single_hunk():
    src = "# Plan\n\n## slot-01\nstatus: pending\n\n## slot-02\nstatus: pending\n"
    diff = "@@ -4,1 +4,1 @@\n-status: pending\n+status: done"
    syms = _touched_markdown_symbols(new_source=src, diff_text=diff)
    assert syms == {"md:slot_01"}


def test_touched_markdown_symbols_different_slot():
    src = "# Plan\n\n## slot-01\nstatus: done\n\n## slot-02\nstatus: pending\n"
    diff = "@@ -7,1 +7,1 @@\n-status: pending\n+status: done"
    syms = _touched_markdown_symbols(new_source=src, diff_text=diff)
    assert syms == {"md:slot_02"}


def test_touched_markdown_symbols_no_overlap():
    src = "# Plan\n\n## slot-01\ncontent\n\n## slot-02\nother\n"
    diff = "@@ -1,1 +1,1 @@\n-# Plan\n+# Changed Plan"
    syms = _touched_markdown_symbols(new_source=src, diff_text=diff)
    assert len(syms) == 0


def test_resolve_touched_symbols_general_exception_goes_to_fallback(git_repo: Path, monkeypatch):
    import merge_train.symbols
    def mock_raise(*args, **kwargs):
        raise ValueError("unexpected parse error")
    
    monkeypatch.setattr(merge_train.symbols, "touched_symbols_for_staged_file", mock_raise)
    
    per_file, fallback = resolve_touched_symbols(["any_file.py"], cwd=git_repo)
    assert "any_file.py" not in per_file
    assert "any_file.py" in fallback


def test_touched_symbols_committed_changes(git_repo: Path):
    src_orig = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    (git_repo / "m.py").write_text(src_orig)
    _git(git_repo, "add", "m.py")
    _git(git_repo, "commit", "-q", "-m", "init")

    # Create a feature branch and make committed changes compared to main
    _git(git_repo, "checkout", "-b", "feature")
    
    src_new = src_orig.replace("    return 2\n", "    return 22\n")
    (git_repo / "m.py").write_text(src_new)
    _git(git_repo, "add", "m.py")
    _git(git_repo, "commit", "-q", "-m", "update beta")

    # Now there are NO staged changes (git diff --cached is empty).
    # But there is a committed change on the feature branch compared to main.
    touched = touched_symbols_for_staged_file("m.py", cwd=git_repo)
    assert touched == {"m.py:beta"}


def test_touched_symbols_unstaged_changes(git_repo: Path):
    src_orig = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    (git_repo / "m.py").write_text(src_orig)
    _git(git_repo, "add", "m.py")
    _git(git_repo, "commit", "-q", "-m", "init")

    # Make unstaged changes in the working tree
    src_new = src_orig.replace("    return 1\n", "    return 11\n")
    (git_repo / "m.py").write_text(src_new)

    # Staged diff is empty. Unstaged diff has the change.
    touched = touched_symbols_for_staged_file("m.py", cwd=git_repo)
    assert touched == {"m.py:alpha"}


def test_touched_symbols_staged_and_unstaged_combined(git_repo: Path):
    src_orig = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    (git_repo / "m.py").write_text(src_orig)
    _git(git_repo, "add", "m.py")
    _git(git_repo, "commit", "-q", "-m", "init")

    # Stage a change in alpha
    src_staged = src_orig.replace("    return 1\n", "    return 11\n")
    (git_repo / "m.py").write_text(src_staged)
    _git(git_repo, "add", "m.py")

    # Modify beta in the working tree (unstaged)
    src_unstaged = src_staged.replace("    return 2\n", "    return 22\n")
    (git_repo / "m.py").write_text(src_unstaged)

    # Since staged_diff is not empty, we extract from the staged diff/content only!
    # Staged diff only has alpha changed, not beta.
    touched = touched_symbols_for_staged_file("m.py", cwd=git_repo)
    assert touched == {"m.py:alpha"}


def test_touched_markdown_symbols_committed_and_unstaged(git_repo: Path):
    src_orig = "# Title\n\n## slot-01\ncontent 1\n\n## slot-02\ncontent 2\n"
    (git_repo / "plan.md").write_text(src_orig)
    _git(git_repo, "add", "plan.md")
    _git(git_repo, "commit", "-q", "-m", "init")

    _git(git_repo, "checkout", "-b", "feature-md")
    src_committed = src_orig.replace("content 1", "content 11")
    (git_repo / "plan.md").write_text(src_committed)
    _git(git_repo, "add", "plan.md")
    _git(git_repo, "commit", "-q", "-m", "commit md change")

    # Unstaged change in slot-02
    src_unstaged = src_committed.replace("content 2", "content 22")
    (git_repo / "plan.md").write_text(src_unstaged)

    # Since there are no staged changes, we fall back to git diff against main (the merge base)
    # and read plan.md on disk.
    # The diff against main compares main vs working tree, so it sees changes in BOTH slot-01 and slot-02!
    touched = touched_symbols_for_staged_file("plan.md", cwd=git_repo)
    assert touched == {"md:plan.slot_01", "md:plan.slot_02"}

