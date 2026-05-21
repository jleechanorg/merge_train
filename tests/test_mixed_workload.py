"""Mixed-file, mixed-domain, non-Markdown workload tests at scale."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from merge_train.domain_lock import (
    DomainHeldError,
    LockLog,
    PlanItem,
    Registry,
    UnknownPathError,
    check,
    reserve,
    reserve_plan,
)
from merge_train.symbols import (
    extract_markdown_symbols,
    extract_symbols,
    touched_symbols,
    _touched_markdown_symbols,
)
from merge_train.symbol_discovery import (
    symbols_from_pr_diff,
)


def _reg(data: dict) -> Registry:
    return Registry.from_dict(data)


def _generate_n_functions(n: int) -> str:
    lines: list[str] = []
    for i in range(n):
        lines.append(f"def func_{i:03d}():")
        lines.append(f"    return {i}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1. Python symbol extraction at scale
# --------------------------------------------------------------------------- #


def test_python_symbol_extraction_at_scale_55_functions():
    source = _generate_n_functions(55)
    syms = extract_symbols(source)
    assert len(syms) == 55
    names = [s.name for s in syms]
    assert names == [f"func_{i:03d}" for i in range(55)]
    for i, sym in enumerate(syms):
        assert sym.start == i * 3 + 1
        assert sym.end == i * 3 + 2


def test_python_symbol_extraction_at_scale_line_ranges():
    source = _generate_n_functions(60)
    syms = extract_symbols(source)
    for sym in syms:
        assert sym.contains_line(sym.start)
        assert sym.contains_line(sym.end)
        assert not sym.contains_line(sym.start - 1)
        assert not sym.contains_line(sym.end + 1)


def test_python_50_plus_touched_symbols_via_diff():
    source = _generate_n_functions(55)
    line_no = 3 * 50 + 2
    diff = f"@@ -{line_no},1 +{line_no},1 @@\n-    return 50\n+    return 999\n"
    touched = touched_symbols(new_source=source, diff_text=diff)
    assert touched == {"func_050"}


def test_markdown_many_sections_scale():
    lines = ["# Doc\n"]
    for i in range(60):
        lines.append(f"\n## section-{i:03d}\n")
        lines.append(f"Content for section {i}.\n")
    source = "".join(lines)
    syms = extract_markdown_symbols(source)
    assert len(syms) == 60
    for i, sym in enumerate(syms):
        assert sym.name == f"md:section_{i:03d}"


# --------------------------------------------------------------------------- #
# 2. Mixed Python + Markdown in same domain
# --------------------------------------------------------------------------- #


def test_mixed_python_markdown_same_domain_reserve_coexistence(tmp_path: Path):
    reg = _reg({"domains": {"mixed": {"paths": ["app.py", "docs.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    e1 = reserve(log, reg, domain="mixed", pr=1, agent="a", branch="b1",
                 symbols=["alpha"])
    e2 = reserve(log, reg, domain="mixed", pr=2, agent="a2", branch="b2",
                 symbols=["md:docs.slot_01"])
    assert e1.symbols == ("alpha",)
    assert e2.symbols == ("md:docs.slot_01",)
    assert len(log.active_all()) == 2


def test_mixed_python_markdown_same_domain_check_disjoint(tmp_path: Path):
    reg = _reg({"domains": {"mixed": {"paths": ["app.py", "docs.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="mixed", pr=1, agent="a", branch="b",
            symbols=["alpha"])
    result = check(
        log, reg,
        files=["docs.md"], pr=2,
        touched_symbols_by_path={"docs.md": {"md:docs.slot_01"}},
    )
    assert result.ok


def test_mixed_python_markdown_same_domain_check_overlap(tmp_path: Path):
    reg = _reg({"domains": {"mixed": {"paths": ["app.py", "docs.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="mixed", pr=1, agent="a", branch="b",
            symbols=["alpha"])
    result = check(
        log, reg,
        files=["app.py"], pr=2,
        touched_symbols_by_path={"app.py": {"alpha"}},
    )
    assert not result.ok


# --------------------------------------------------------------------------- #
# 3. Multi-domain reservations
# --------------------------------------------------------------------------- #


def test_multi_domain_reserve_plan_atomic_success(tmp_path: Path):
    reg = _reg({
        "domains": {
            "backend": {"paths": ["api.py"]},
            "frontend": {"paths": ["ui.tsx"]},
            "infra": {"paths": ["deploy.yaml"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    entries = reserve_plan(
        log, reg,
        pr=10, agent="bot", branch="feature",
        plan=[
            PlanItem(domain="backend", symbols=("handler",)),
            PlanItem(domain="frontend", symbols=()),
            PlanItem(domain="infra", symbols=("deploy_config",)),
        ],
    )
    assert len(entries) == 3
    active = log.active_all()
    assert len(active) == 3
    assert {e.domain for e in active} == {"backend", "frontend", "infra"}


def test_multi_domain_reserve_plan_rollback_on_conflict(tmp_path: Path):
    reg = _reg({
        "domains": {
            "backend": {"paths": ["api.py"]},
            "frontend": {"paths": ["ui.tsx"]},
            "infra": {"paths": ["deploy.yaml"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="frontend", pr=99, agent="other", branch="main")
    with pytest.raises(DomainHeldError):
        reserve_plan(
            log, reg,
            pr=10, agent="bot", branch="feature",
            plan=[
                PlanItem(domain="backend", symbols=("handler",)),
                PlanItem(domain="frontend", symbols=()),
                PlanItem(domain="infra", symbols=("deploy_config",)),
            ],
        )
    active = log.active_all()
    assert all(e.pr == 99 for e in active)
    assert all(e.domain != "backend" for e in active)
    assert all(e.domain != "infra" for e in active)


def test_reserve_plan_unknown_domain_rolls_back(tmp_path: Path):
    reg = _reg({"domains": {"known": {"paths": ["known.py"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    with pytest.raises(UnknownPathError):
        reserve_plan(
            log, reg,
            pr=1, agent="a", branch="b",
            plan=[
                PlanItem(domain="known", symbols=("x",)),
                PlanItem(domain="nonexistent", symbols=("y",)),
            ],
        )
    assert len(log.active_all()) == 0


# --------------------------------------------------------------------------- #
# 4. YAML/JSON fallback — whole-domain locking
# --------------------------------------------------------------------------- #


def test_yaml_json_fallback_no_holders_is_free(tmp_path: Path):
    reg = _reg({
        "domains": {
            "config": {"paths": ["config.yaml", "data.json", "settings.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    result = check(
        log, reg,
        files=["config.yaml", "data.json", "settings.py"], pr=1,
        touched_symbols_by_path={
            "config.yaml": None,
            "data.json": None,
            "settings.py": {"init_db"},
        },
    )
    assert result.ok


def test_yaml_json_fallback_none_blocks_on_symbol_holder(tmp_path: Path):
    reg = _reg({
        "domains": {
            "config": {"paths": ["config.yaml", "data.json", "settings.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="config", pr=5, agent="a", branch="b",
            symbols=["init_db"])
    result = check(
        log, reg,
        files=["config.yaml", "data.json"], pr=2,
        touched_symbols_by_path={
            "config.yaml": None,
            "data.json": None,
        },
    )
    assert not result.ok


def test_yaml_json_fallback_disjoint_python_symbols_free(tmp_path: Path):
    reg = _reg({
        "domains": {
            "config": {"paths": ["config.yaml", "data.json", "settings.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="config", pr=5, agent="a", branch="b",
            symbols=["init_db"])
    result = check(
        log, reg,
        files=["settings.py"], pr=2,
        touched_symbols_by_path={
            "settings.py": {"run_server"},
        },
    )
    assert result.ok


def test_yaml_json_fallback_file_level_check_blocks(tmp_path: Path):
    reg = _reg({"domains": {"config": {"paths": ["config.yaml"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="config", pr=1, agent="a", branch="b",
            symbols=["some_key"])
    result = check(log, reg, files=["config.yaml"], pr=2)
    assert not result.ok


def test_yaml_json_path_missing_from_touched_map_triggers_whole_domain(tmp_path: Path):
    reg = _reg({
        "domains": {
            "config": {"paths": ["config.yaml", "settings.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="config", pr=1, agent="a", branch="b",
            symbols=["init_db"])
    result = check(
        log, reg,
        files=["config.yaml", "settings.py"], pr=2,
        touched_symbols_by_path={
            "settings.py": {"run_server"},
        },
    )
    assert not result.ok


def test_yaml_json_fallback_whole_domain_holder_blocks_resolved_python(tmp_path: Path):
    reg = _reg({
        "domains": {
            "config": {"paths": ["config.yaml", "settings.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="config", pr=1, agent="a", branch="b")
    result = check(
        log, reg,
        files=["settings.py"], pr=2,
        touched_symbols_by_path={
            "settings.py": {"disjoint_func"},
        },
    )
    assert not result.ok
    assert result.held[0][1].is_whole_domain


# --------------------------------------------------------------------------- #
# 5. Mixed symbol types in one domain
# --------------------------------------------------------------------------- #


def test_mixed_symbol_types_python_and_markdown_coexist(tmp_path: Path):
    reg = _reg({"domains": {"core": {"paths": ["core.py", "README.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    e_py = reserve(log, reg, domain="core", pr=1, agent="a", branch="b1",
                   symbols=["process_data"])
    e_md = reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2",
                   symbols=["md:README.installation"])
    assert e_py.symbols == ("process_data",)
    assert e_md.symbols == ("md:README.installation",)
    active = log.active_all()
    assert len(active) == 2
    all_syms = set()
    for e in active:
        all_syms.update(e.symbols)
    assert all_syms == {"process_data", "md:README.installation"}


def test_mixed_symbol_types_disjoint_check(tmp_path: Path):
    reg = _reg({"domains": {"core": {"paths": ["core.py", "README.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b",
            symbols=["process_data"])
    result = check(
        log, reg,
        files=["README.md"], pr=2,
        touched_symbols_by_path={"README.md": {"md:README.installation"}},
    )
    assert result.ok


def test_mixed_symbol_types_overlap_check(tmp_path: Path):
    reg = _reg({"domains": {"core": {"paths": ["core.py", "README.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b",
            symbols=["process_data"])
    result = check(
        log, reg,
        files=["core.py"], pr=2,
        touched_symbols_by_path={"core.py": {"process_data"}},
    )
    assert not result.ok


def test_mixed_symbol_types_whole_domain_blocks_symbol_reserve(tmp_path: Path):
    reg = _reg({"domains": {"core": {"paths": ["core.py", "README.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b")
    with pytest.raises(DomainHeldError):
        reserve(log, reg, domain="core", pr=2, agent="a2", branch="b2",
                symbols=["md:README.installation"])


def test_mixed_symbol_types_markdown_holder_python_check_disjoint(tmp_path: Path):
    reg = _reg({"domains": {"core": {"paths": ["core.py", "README.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="core", pr=1, agent="a", branch="b",
            symbols=["md:README.installation"])
    result = check(
        log, reg,
        files=["core.py"], pr=2,
        touched_symbols_by_path={"core.py": {"process_data"}},
    )
    assert result.ok


def test_mixed_symbol_types_reserve_plan_mixed_symbol_kinds(tmp_path: Path):
    reg = _reg({"domains": {"core": {"paths": ["core.py", "README.md"]}}})
    log = LockLog(tmp_path / "log.jsonl")
    entries = reserve_plan(
        log, reg,
        pr=10, agent="bot", branch="feature",
        plan=[
            PlanItem(domain="core", symbols=("process_data",)),
            PlanItem(domain="core", symbols=("md:README.installation",)),
        ],
    )
    assert len(entries) == 2
    active = log.active_all()
    assert len(active) == 2
    sym_sets = {e.symbols for e in active}
    assert ("md:README.installation",) in sym_sets
    assert ("process_data",) in sym_sets


# --------------------------------------------------------------------------- #
# 6. Cross-domain plan with partial conflict
# --------------------------------------------------------------------------- #


def test_cross_domain_plan_partial_conflict_rolls_back_all(tmp_path: Path):
    reg = _reg({
        "domains": {
            "alpha": {"paths": ["alpha.py"]},
            "beta": {"paths": ["beta.py"]},
            "gamma": {"paths": ["gamma.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="beta", pr=50, agent="holder", branch="main",
            symbols=["conflicting_func"])
    with pytest.raises(DomainHeldError):
        reserve_plan(
            log, reg,
            pr=10, agent="bot", branch="feature",
            plan=[
                {"domain": "alpha", "symbols": ["func_a"]},
                {"domain": "beta", "symbols": ["conflicting_func"]},
                {"domain": "gamma", "symbols": ["func_c"]},
            ],
        )
    active = log.active_all()
    assert len(active) == 1
    assert active[0].pr == 50
    assert active[0].domain == "beta"


def test_cross_domain_plan_success_then_separate_conflict(tmp_path: Path):
    reg = _reg({
        "domains": {
            "d1": {"paths": ["d1.py"]},
            "d2": {"paths": ["d2.py"]},
            "d3": {"paths": ["d3.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    entries = reserve_plan(
        log, reg,
        pr=10, agent="bot", branch="feature",
        plan=[
            PlanItem(domain="d1", symbols=("a",)),
            PlanItem(domain="d2", symbols=("b",)),
            PlanItem(domain="d3", symbols=("c",)),
        ],
    )
    assert len(entries) == 3
    reserve(log, reg, domain="d1", pr=99, agent="holder", branch="main",
            symbols=["x"])
    with pytest.raises(DomainHeldError):
        reserve_plan(
            log, reg,
            pr=20, agent="bot2", branch="feature2",
            plan=[
                PlanItem(domain="d1", symbols=("a",)),
                PlanItem(domain="d3", symbols=("z",)),
            ],
        )
    active = log.active_all()
    assert len([e for e in active if e.pr == 10]) == 3
    assert len([e for e in active if e.pr == 99]) == 1


def test_cross_domain_plan_rollback_logs_released_entries(tmp_path: Path):
    reg = _reg({
        "domains": {
            "d1": {"paths": ["d1.py"]},
            "d2": {"paths": ["d2.py"]},
            "d3": {"paths": ["d3.py"]},
        }
    })
    log = LockLog(tmp_path / "log.jsonl")
    reserve(log, reg, domain="d2", pr=50, agent="holder", branch="main",
            symbols=["conflict"])
    with pytest.raises(DomainHeldError):
        reserve_plan(
            log, reg,
            pr=10, agent="bot", branch="feature",
            plan=[
                PlanItem(domain="d1", symbols=("a",)),
                PlanItem(domain="d2", symbols=("conflict",)),
                PlanItem(domain="d3", symbols=("c",)),
            ],
        )
    rolled_back = [
        e for e in log.entries()
        if e.status == "released" and e.pr == 10
    ]
    assert len(rolled_back) == 1
    assert rolled_back[0].domain == "d1"
    assert rolled_back[0].note == "rollback:reserve_plan"


# --------------------------------------------------------------------------- #
# 7. Symbol enrichment for PR diff
# --------------------------------------------------------------------------- #


def test_symbols_from_pr_diff_mixed_file_types():
    py_diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -2,1 +2,1 @@\n-    return 1\n+    return 11\n"
    )
    md_diff = (
        "diff --git a/docs/guide.md b/docs/guide.md\n"
        "--- a/docs/guide.md\n"
        "+++ b/docs/guide.md\n"
        "@@ -5,1 +5,1 @@\n-old\n+new\n"
    )
    yaml_diff = (
        "diff --git a/config.yaml b/config.yaml\n"
        "--- a/config.yaml\n"
        "+++ b/config.yaml\n"
        "@@ -1,2 +1,2 @@\n-old: val\n+new: val\n"
    )
    full_diff = py_diff + md_diff + yaml_diff
    py_content = (
        "def alpha():\n"
        "    return 11\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    md_content = (
        "# Guide\n"
        "\n"
        "## installation\n"
        "\n"
        "new\n"
        "\n"
        "## usage\n"
        "\n"
        "info\n"
    )
    with patch("merge_train.symbol_discovery._gh_pr_diff", return_value=full_diff), \
         patch("merge_train.symbol_discovery._gh_pr_head_ref", return_value="feature"), \
         patch("merge_train.symbol_discovery._gh_file_content_at_ref") as mock_content:
        def _side_effect(path, ref, repo):
            if path == "src/app.py":
                return py_content
            if path == "docs/guide.md":
                return md_content
            return ""
        mock_content.side_effect = _side_effect
        result = symbols_from_pr_diff(42, repo="owner/repo")
    assert "src/app.py" in result
    assert "alpha" in result["src/app.py"]
    assert "docs/guide.md" in result
    assert any("installation" in s for s in result["docs/guide.md"])
    assert "config.yaml" not in result


def test_symbols_from_pr_diff_empty_diff():
    with patch("merge_train.symbol_discovery._gh_pr_diff", return_value=""):
        result = symbols_from_pr_diff(1, repo="owner/repo")
    assert result == {}


def test_symbols_from_pr_diff_python_only():
    diff = (
        "diff --git a/svc.py b/svc.py\n"
        "--- a/svc.py\n"
        "+++ b/svc.py\n"
        "@@ -4,1 +4,1 @@\n-    return 2\n+    return 22\n"
    )
    py_content = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 22\n"
    )
    with patch("merge_train.symbol_discovery._gh_pr_diff", return_value=diff), \
         patch("merge_train.symbol_discovery._gh_pr_head_ref", return_value="main"), \
         patch("merge_train.symbol_discovery._gh_file_content_at_ref", return_value=py_content):
        result = symbols_from_pr_diff(5, repo="org/repo")
    assert "svc.py" in result
    assert "beta" in result["svc.py"]


def test_symbols_from_pr_diff_markdown_only():
    md_diff = (
        "diff --git a/plan.md b/plan.md\n"
        "--- a/plan.md\n"
        "+++ b/plan.md\n"
        "@@ -4,1 +4,1 @@\n-status: pending\n+status: done\n"
    )
    md_content = (
        "# Plan\n"
        "\n"
        "## slot-01\n"
        "\n"
        "status: done\n"
        "\n"
        "## slot-02\n"
        "\n"
        "status: pending\n"
    )
    with patch("merge_train.symbol_discovery._gh_pr_diff", return_value=md_diff), \
         patch("merge_train.symbol_discovery._gh_pr_head_ref", return_value="feature"), \
         patch("merge_train.symbol_discovery._gh_file_content_at_ref", return_value=md_content):
        result = symbols_from_pr_diff(7, repo="org/repo")
    assert "plan.md" in result
    assert any("slot_01" in s for s in result["plan.md"])
