"""Adversarial tests for semantic correctness of area-lock primitives.

Exercises boundary conditions in extract_markdown_symbols, extract_symbols,
touched_symbols, and domain_lock reserve/release beyond controlled fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from merge_train.domain_lock import (
    Domain,
    DomainHeldError,
    LockLog,
    LockEntry,
    Registry,
    PlanItem,
    release,
    reserve,
    reserve_plan,
)
from merge_train.symbols import (
    HunkRange,
    Symbol,
    SymbolResolutionError,
    extract_markdown_symbols,
    extract_symbols,
    parse_hunks,
    touched_symbols,
    _touched_markdown_symbols,
)


def _registry(*names: str) -> Registry:
    domains = {}
    for n in names:
        domains[n] = Domain(name=n, paths=(f"{n}/*",))
    return Registry(domains=domains)


# --------------------------------------------------------------------------- #
# 1. Heading boundary edits
# --------------------------------------------------------------------------- #


def test_heading_line_edit_attributed_to_heading_not_previous():
    src = "## slot-01\nbody\n\n## slot-02\ncontent\n"
    syms = extract_markdown_symbols(src, file_stem="plan")
    slot01 = syms[0]
    slot02 = syms[1]
    assert slot01.name == "md:plan.slot_01"
    assert slot02.name == "md:plan.slot_02"
    diff = f"@@ -{slot02.start} +{slot02.start} @@\n-## slot-02\n+## slot-02 (revised)"
    touched = _touched_markdown_symbols(new_source=src, diff_text=diff, file_stem="plan")
    assert "md:plan.slot_02" in touched
    assert "md:plan.slot_01" not in touched


def test_heading_line_is_start_of_symbol():
    src = "## alpha\na\n\n## beta\nb\n"
    syms = extract_markdown_symbols(src)
    beta = [s for s in syms if s.name == "md:beta"][0]
    assert beta.start == 4
    assert beta.contains_line(4)


# --------------------------------------------------------------------------- #
# 2. Malformed markdown
# --------------------------------------------------------------------------- #


def test_markdown_no_headings_returns_empty():
    src = "just some text\nno headings here\n"
    assert extract_markdown_symbols(src) == []


def test_markdown_only_h1_not_extracted():
    src = "# Title Only\nsome body\n"
    assert extract_markdown_symbols(src) == []


def test_markdown_h6_deep_nesting():
    src = "###### deep\ncontent\n"
    syms = extract_markdown_symbols(src)
    assert len(syms) == 1
    assert syms[0].name == "md:deep"


def test_markdown_duplicate_heading_names_produce_duplicate_symbols():
    src = "## slot-01\na\n\n## slot-01\nb\n"
    syms = extract_markdown_symbols(src)
    names = [s.name for s in syms]
    assert names.count("md:slot_01") == 2
    assert syms[0].start != syms[1].start


def test_touched_markdown_no_headings_nonempty_raises():
    src = "just prose\nno headings\n"
    diff = "@@ -1 +1 @@\n-just prose\n+just prose!"
    with pytest.raises(SymbolResolutionError):
        _touched_markdown_symbols(new_source=src, diff_text=diff)


# --------------------------------------------------------------------------- #
# 3. Off-by-one in symbol ranges
# --------------------------------------------------------------------------- #


def test_edit_on_last_line_of_section_attributed_correctly():
    src = "## slot-01\nline1\nline2\n## slot-02\nline3\n"
    syms = extract_markdown_symbols(src)
    slot01 = syms[0]
    slot02 = syms[1]
    last_line_of_slot01 = slot02.start - 1
    assert slot01.contains_line(last_line_of_slot01)
    assert not slot02.contains_line(last_line_of_slot01)
    diff = f"@@ -{last_line_of_slot01} +{last_line_of_slot01} @@\n-line2\n+line2-edited"
    touched = _touched_markdown_symbols(new_source=src, diff_text=diff)
    assert "md:slot_01" in touched
    assert "md:slot_02" not in touched


def test_edit_on_first_line_of_next_section_not_in_previous():
    src = "## slot-01\nbody\n## slot-02\ncontent\n"
    syms = extract_markdown_symbols(src)
    slot01 = syms[0]
    slot02 = syms[1]
    assert not slot01.contains_line(slot02.start)
    assert slot02.contains_line(slot02.start)


# --------------------------------------------------------------------------- #
# 4. Empty heading sections
# --------------------------------------------------------------------------- #


def test_empty_heading_section_start_equals_end():
    src = "## slot-01\n## slot-02\nbody\n"
    syms = extract_markdown_symbols(src)
    slot01 = syms[0]
    assert slot01.start == slot01.end
    assert slot01.start == 1


def test_edit_on_empty_heading_line_matches_symbol():
    src = "## slot-01\n## slot-02\nbody\n"
    syms = extract_markdown_symbols(src)
    slot01 = syms[0]
    diff = f"@@ -{slot01.start} +{slot01.start} @@\n-## slot-01\n+## slot-01 (updated)"
    touched = _touched_markdown_symbols(new_source=src, diff_text=diff)
    assert "md:slot_01" in touched


# --------------------------------------------------------------------------- #
# 5. Python symbol boundary — adjacent functions, no blank line
# --------------------------------------------------------------------------- #


def test_adjacent_functions_boundary():
    src = "def alpha():\n    return 1\ndef beta():\n    return 2\n"
    syms = extract_symbols(src)
    alpha = [s for s in syms if s.name == "alpha"][0]
    beta = [s for s in syms if s.name == "beta"][0]
    assert alpha.end < beta.start
    assert beta.start == 3


def test_adjacent_functions_no_false_overlap():
    src = "def alpha():\n    return 1\ndef beta():\n    return 2\n"
    diff = "@@ -4 +4 @@\n-    return 2\n+    return 22\n"
    touched = touched_symbols(new_source=src, diff_text=diff)
    assert touched == {"beta"}


def test_edit_on_last_line_of_first_function():
    src = "def alpha():\n    return 1\ndef beta():\n    return 2\n"
    syms = extract_symbols(src)
    alpha = [s for s in syms if s.name == "alpha"][0]
    diff = f"@@ -{alpha.end} +{alpha.end} @@\n-    return 1\n+    return 11\n"
    touched = touched_symbols(new_source=src, diff_text=diff)
    assert "alpha" in touched
    assert "beta" not in touched


# --------------------------------------------------------------------------- #
# 6. Decorators and symbol ranges
# --------------------------------------------------------------------------- #


def test_decorator_included_in_symbol_range():
    src = "@decorator\ndef foo():\n    pass\n"
    syms = extract_symbols(src)
    assert len(syms) == 1
    assert syms[0].start == 1
    assert syms[0].end == 3


def test_edit_on_decorator_line_touches_symbol():
    src = "@decorator\ndef foo():\n    pass\n"
    diff = "@@ -1 +1 @@\n-@decorator\n+@other_decorator\n"
    touched = touched_symbols(new_source=src, diff_text=diff)
    assert "foo" in touched


def test_multiple_decorators_all_included():
    src = "@dec1\n@dec2\ndef foo():\n    pass\n"
    syms = extract_symbols(src)
    assert syms[0].start == 1
    assert syms[0].contains_line(1)
    assert syms[0].contains_line(2)


# --------------------------------------------------------------------------- #
# 7. Heading with special characters — slug generation
# --------------------------------------------------------------------------- #


def test_heading_slug_strips_special_chars():
    src = "## slot-01 (v2.0)\ncontent\n"
    syms = extract_markdown_symbols(src, file_stem="plan")
    assert syms[0].name == "md:plan.slot_01_v2_0"


def test_heading_slug_parentheses_and_dots():
    src = "## Foo: bar/baz (v3!)\ncontent\n"
    syms = extract_markdown_symbols(src)
    assert syms[0].name == "md:foo_bar_baz_v3"


def test_heading_slug_stable_across_extractions():
    src = "## slot-01 (v2.0)\ncontent\n"
    syms1 = extract_markdown_symbols(src, file_stem="plan")
    syms2 = extract_markdown_symbols(src, file_stem="plan")
    assert syms1[0].name == syms2[0].name


def test_heading_slug_leading_trailing_underscores_stripped():
    src = "## !! Foo !!\nbody\n"
    syms = extract_markdown_symbols(src)
    assert syms[0].name == "md:foo"


# --------------------------------------------------------------------------- #
# 8. Concurrent edit to adjacent but disjoint symbols
# --------------------------------------------------------------------------- #


def test_adjacent_markdown_symbols_no_false_collision(tmp_path: Path):
    log = LockLog(str(tmp_path / "locks.jsonl"))
    reg = _registry("slots")
    src = "## slot-01\nbody\n## slot-02\ncontent\n"
    syms = extract_markdown_symbols(src, file_stem="slots")
    s01 = [s for s in syms if "slot_01" in s.name][0]
    s02 = [s for s in syms if "slot_02" in s.name][0]
    assert s01.end == s02.start - 1

    reserve(
        log, reg,
        domain="slots", pr=1, agent="a1", branch="b1",
        symbols=("md:slots.slot_01",), now="t1",
    )
    reserve(
        log, reg,
        domain="slots", pr=2, agent="a2", branch="b2",
        symbols=("md:slots.slot_02",), now="t2",
    )
    active = log.active_all()
    assert len(active) == 2


def test_adjacent_python_symbols_no_false_collision(tmp_path: Path):
    log = LockLog(str(tmp_path / "locks.jsonl"))
    reg = _registry("code")
    src = "def alpha():\n    return 1\ndef beta():\n    return 2\n"
    syms = extract_symbols(src)
    alpha = [s for s in syms if s.name == "alpha"][0]
    beta = [s for s in syms if s.name == "beta"][0]
    assert alpha.end < beta.start

    reserve(
        log, reg,
        domain="code", pr=10, agent="a", branch="br1",
        symbols=("alpha",), now="t1",
    )
    reserve(
        log, reg,
        domain="code", pr=11, agent="b", branch="br2",
        symbols=("beta",), now="t2",
    )
    active = log.active_all()
    assert len(active) == 2


# --------------------------------------------------------------------------- #
# 9. Diff hunk spanning heading boundary
# --------------------------------------------------------------------------- #


def test_hunk_spanning_two_markdown_symbols():
    src = "## slot-01\nlast_line\n## slot-02\nfirst_line\n"
    syms = extract_markdown_symbols(src)
    slot01 = syms[0]
    slot02 = syms[1]
    boundary_line = slot02.start
    assert slot01.contains_line(boundary_line - 1)
    assert slot02.contains_line(boundary_line)
    hunk = HunkRange(start=boundary_line - 1, end=boundary_line)
    overlapping = []
    for sym in syms:
        if sym.overlaps(hunk.start, hunk.end):
            overlapping.append(sym.name)
    assert "md:slot_01" in overlapping
    assert "md:slot_02" in overlapping


def test_hunk_spanning_two_python_symbols():
    src = "def alpha():\n    x = 1\ndef beta():\n    y = 2\n"
    syms = extract_symbols(src)
    alpha = [s for s in syms if s.name == "alpha"][0]
    beta = [s for s in syms if s.name == "beta"][0]
    boundary = beta.start
    assert alpha.contains_line(boundary - 1)
    assert beta.contains_line(boundary)
    hunk = HunkRange(start=boundary - 1, end=boundary)
    overlapping = [s.name for s in syms if s.overlaps(hunk.start, hunk.end)]
    assert "alpha" in overlapping
    assert "beta" in overlapping


# --------------------------------------------------------------------------- #
# 10. Re-reservation after release on same symbols
# --------------------------------------------------------------------------- #


def test_reserve_release_reserve_same_symbols(tmp_path: Path):
    log = LockLog(str(tmp_path / "locks.jsonl"))
    reg = _registry("foo")
    reserve(
        log, reg,
        domain="foo", pr=1, agent="a1", branch="b1",
        symbols=("sym_a", "sym_b"), now="t1",
    )
    assert len(log.active_all()) == 1
    release(log, pr=1, now="t2")
    assert len(log.active_all()) == 0
    entry = reserve(
        log, reg,
        domain="foo", pr=1, agent="a1", branch="b1",
        symbols=("sym_a", "sym_b"), now="t3",
    )
    assert entry.status == "active"
    assert len(log.active_all()) == 1
    entries = log.entries()
    active_before_release = [e for e in entries if e.status == "active"]
    total_entries = len(entries)
    assert total_entries >= 3


def test_reserve_release_reserve_different_pr_same_domain(tmp_path: Path):
    log = LockLog(str(tmp_path / "locks.jsonl"))
    reg = _registry("zone")
    reserve(
        log, reg,
        domain="zone", pr=5, agent="a", branch="b1",
        symbols=("sym_x",), now="t1",
    )
    release(log, pr=5, now="t2")
    entry = reserve(
        log, reg,
        domain="zone", pr=6, agent="b", branch="b2",
        symbols=("sym_x",), now="t3",
    )
    assert entry.status == "active"
    assert entry.pr == 6


def test_reserve_same_symbols_while_held_raises(tmp_path: Path):
    log = LockLog(str(tmp_path / "locks.jsonl"))
    reg = _registry("zone")
    reserve(
        log, reg,
        domain="zone", pr=1, agent="a1", branch="b1",
        symbols=("sym_a",), now="t1",
    )
    with pytest.raises(DomainHeldError):
        reserve(
            log, reg,
            domain="zone", pr=2, agent="a2", branch="b2",
            symbols=("sym_a",), now="t2",
        )
