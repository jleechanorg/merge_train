"""TDD tests for domain_lock check report formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

from merge_train.domain_lock import (
    LockLog,
    Registry,
    _format_check_report,
    check,
    reserve,
)


def _registry() -> Registry:
    return Registry.from_dict({
        "domains": {
            "docs": {"paths": ["docs/**"], "advisory": True},
            "mvp-llm": {"paths": ["mvp/llm/**"]},
            "mvp-frontend": {"paths": ["mvp/frontend/**"]},
            "d2": {"paths": ["b.py"]},
        }
    })


def _held_symbol_conflict(tmp_path: Path):
    """Symbol-level HELD conflict on a.py / d1 domain."""
    reg = Registry.from_dict({
        "domains": {"d1": {"paths": ["a.py"]}},
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(
        log, reg, domain="d1", pr=7178,
        agent="agent-jleechan-3880", branch="fix/conclude-finalize-prompt",
        symbols=("conclude_finalize",),
    )
    touched = {"a.py": {"conclude_finalize", "other_fn"}}
    result = check(
        log, reg, files=["a.py"], pr=2,
        touched_symbols_by_path=touched,
    )
    return log, reg, result, touched


def test_report_headline_is_file_path_not_domain_name(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = _registry()
    reserve(
        log, reg, domain="docs", pr=7178,
        agent="claude-docs", branch="level_creation_refactor_factory",
    )
    touched = {"docs/design.md": {"SectionHeader"}}
    result = check(
        log, reg,
        files=["docs/design.md"],
        pr=999,
        touched_symbols_by_path=touched,
    )

    report = _format_check_report(
        result, reg, ["docs/design.md"], touched,
    )

    assert "docs/design.md  symbols: SectionHeader" in report
    assert "  docs — held by" not in report
    assert "  docs\n" not in report


def test_report_omits_agent_name(tmp_path: Path):
    _, reg, result, touched = _held_symbol_conflict(tmp_path)

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "agent-jleechan" not in report
    assert "agent=" not in report


def test_report_shows_pr_and_branch_for_holder(tmp_path: Path):
    _, reg, result, touched = _held_symbol_conflict(tmp_path)

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "Held by: PR#7178 (fix/conclude-finalize-prompt)" in report


def test_report_shows_your_symbols_on_file_line(tmp_path: Path):
    _, reg, result, touched = _held_symbol_conflict(tmp_path)

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "a.py  symbols: conclude_finalize, other_fn" in report


def test_report_shows_holder_symbols_for_symbol_lock(tmp_path: Path):
    _, reg, result, touched = _held_symbol_conflict(tmp_path)

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "holder symbols: conclude_finalize" in report


def test_report_shows_symbol_overlap(tmp_path: Path):
    _, reg, result, touched = _held_symbol_conflict(tmp_path)

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "overlap: conclude_finalize" in report


def test_report_whole_domain_holder_notes_whole_domain_lock(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = _registry()
    reserve(
        log, reg, domain="mvp-frontend", pr=974981,
        agent="agent-y", branch="feat/ui",
    )
    touched = {"mvp/frontend/App.tsx": {"AppRoot"}}
    result = check(
        log, reg,
        files=["mvp/frontend/App.tsx"],
        pr=999,
        touched_symbols_by_path=touched,
    )

    report = _format_check_report(
        result, reg, ["mvp/frontend/App.tsx"], touched,
    )

    assert "mvp/frontend/App.tsx  symbols: AppRoot" in report
    assert "holder lock: whole domain" in report


def test_report_labels_advisory_conflicts(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = _registry()
    reserve(
        log, reg, domain="docs", pr=7178,
        agent="claude-docs", branch="level_creation_refactor_factory",
    )
    touched = {"docs/design.md": {"SectionHeader"}}
    result = check(
        log, reg,
        files=["docs/design.md"],
        pr=999,
        touched_symbols_by_path=touched,
    )

    report = _format_check_report(
        result, reg, ["docs/design.md"], touched,
    )

    assert "ADVISORY (informational, not blocking):" in report
    assert "HELD (blocking):" not in report


def test_report_labels_blocking_conflicts(tmp_path: Path):
    _, reg, result, touched = _held_symbol_conflict(tmp_path)

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "HELD (blocking):" in report


def test_report_free_lists_file_paths_not_domain_names(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = _registry()
    reserve(log, reg, domain="mvp-llm", pr=1, agent="a", branch="b")
    result = check(
        log, reg,
        files=["b.py"],
        pr=2,
        touched_symbols_by_path={"b.py": {"beta"}},
    )

    report = _format_check_report(
        result, reg, ["b.py"], {"b.py": {"beta"}},
    )

    assert "FREE: 1 file(s) clear (b.py)" in report
    assert "d2" not in report


def test_report_groups_conflicting_files_under_single_holder(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = Registry.from_dict({
        "domains": {"mvp-llm": {"paths": ["mvp/llm/**"]}},
    })
    reserve(
        log, reg, domain="mvp-llm", pr=7178,
        agent="x", branch="fix/llm",
    )
    files = ["mvp/llm/handler.py", "mvp/llm/prompts.py"]
    touched = {
        "mvp/llm/handler.py": {"handle_request"},
        "mvp/llm/prompts.py": {"build_prompt"},
    }
    result = check(
        log, reg, files=files, pr=999,
        touched_symbols_by_path=touched,
    )

    report = _format_check_report(result, reg, files, touched)

    assert report.count("Held by: PR#7178 (fix/llm)") == 1
    assert "mvp/llm/handler.py  symbols: handle_request" in report
    assert "mvp/llm/prompts.py  symbols: build_prompt" in report


def test_report_notes_whole_file_when_symbols_unavailable(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = Registry.from_dict({"domains": {"d1": {"paths": ["a.py"]}}})
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    touched = {"a.py": None}
    result = check(
        log, reg, files=["a.py"], pr=2,
        touched_symbols_by_path=touched,
    )

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "a.py  (whole file — symbols unavailable)" in report


def test_report_notes_no_symbols_in_diff(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = Registry.from_dict({"domains": {"d1": {"paths": ["a.py"]}}})
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    touched = {"a.py": set()}
    result = check(
        log, reg, files=["a.py"], pr=2,
        touched_symbols_by_path=touched,
    )

    report = _format_check_report(result, reg, ["a.py"], touched)

    assert "a.py  (no symbols in diff)" in report


def test_report_file_level_mode_without_touched_map(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = Registry.from_dict({"domains": {"d1": {"paths": ["a.py"]}}})
    reserve(log, reg, domain="d1", pr=1, agent="a", branch="b")
    result = check(log, reg, files=["a.py"], pr=2)

    report = _format_check_report(result, reg, ["a.py"])

    assert "• Domain: d1" in report
    assert "Held by: PR#1 (b)" in report
    assert "- a.py" in report


def test_report_summarizes_large_lists_of_assets(tmp_path: Path):
    log = LockLog(tmp_path / "log.jsonl")
    reg = Registry.from_dict({"domains": {"docs": {"paths": ["docs/**"]}}})
    reserve(log, reg, domain="docs", pr=7178, agent="x", branch="fix/docs")
    
    files = [
        f"docs/evidence/pr-7173/{i}_story_action.png" for i in range(10)
    ]
    result = check(log, reg, files=files, pr=999)
    
    report = _format_check_report(result, reg, files)
    
    assert "10 asset & log files" in report
    assert "e.g., docs/evidence/pr-7173/0_story_action.png" in report
    assert "and 8 more" in report
