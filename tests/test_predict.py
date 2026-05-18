"""Tests for the dry-run / predict-conflicts feature.

Covers: PRSpec parsing, lock-entry projection, pairwise domain conflict
detection, conflict-graph MIS scheduling, CLI smoke for the JSON output
shape, and unmapped-files reporting.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from merge_train.domain_lock import Registry
from merge_train.predict import (
    DISCLAIMER,
    DomainConflict,
    PRSpec,
    Plan,
    _git_merge_tree_conflicts,
    _greedy_maximal_independent_set,
    _pair_domain_conflicts,
    _parse_merge_tree_z,
    _spec_as_lock_entries,
    load_plan,
    predict_conflicts,
)

# Backward-compat alias for tests written before the maximal-rename.
_greedy_max_independent_set = _greedy_maximal_independent_set


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def reg() -> Registry:
    return Registry.from_dict({
        "domains": {
            "world": {"paths": ["mvp_site/world_logic.py"]},
            "rewards": {"paths": ["mvp_site/rewards_engine.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
        }
    })


# --------------------------------------------------------------------------- #
# PRSpec
# --------------------------------------------------------------------------- #


def test_prspec_from_dict_full():
    spec = PRSpec.from_dict({
        "pr": 123, "branch": "feat/x",
        "files": ["a.py"],
        "symbols": {"a.py": ["foo", "bar"]},
    })
    assert spec.pr == 123
    assert spec.branch == "feat/x"
    assert spec.files == ("a.py",)
    assert spec.symbols_by_file == {"a.py": frozenset({"foo", "bar"})}


def test_prspec_from_dict_defaults_branch():
    spec = PRSpec.from_dict({"pr": 7, "files": []})
    assert spec.branch == "pr-7"


def test_prspec_missing_symbols_means_whole_file():
    spec = PRSpec.from_dict({"pr": 1, "files": ["a.py"]})
    assert spec.symbols_by_file == {}  # caller treats as whole-file


def test_load_plan_yaml(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({
        "prs": [
            {"pr": 1, "branch": "b1", "files": ["a.py"]},
            {"pr": 2, "branch": "b2", "files": ["b.py"]},
        ]
    }))
    specs = load_plan(p)
    assert [s.pr for s in specs] == [1, 2]


def test_load_plan_accepts_legacy_plan_key(tmp_path: Path):
    """Backward compat with the `reserve-plan` schema's top-level key."""
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({
        "plan": [{"pr": 1, "branch": "b", "files": ["a.py"]}]
    }))
    assert len(load_plan(p)) == 1


def test_load_plan_rejects_non_list(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({"prs": "not a list"}))
    with pytest.raises(ValueError):
        load_plan(p)


# --------------------------------------------------------------------------- #
# _spec_as_lock_entries
# --------------------------------------------------------------------------- #


def test_spec_as_lock_entries_whole_file(reg: Registry):
    spec = PRSpec(
        pr=1, branch="b1",
        files=("mvp_site/world_logic.py",),
        symbols_by_file={},
    )
    entries = _spec_as_lock_entries(spec, reg)
    assert len(entries) == 1
    assert entries[0].domain == "world"
    assert entries[0].symbols == []  # whole-domain


def test_spec_as_lock_entries_symbol_level(reg: Registry):
    spec = PRSpec(
        pr=1, branch="b1",
        files=("mvp_site/world_logic.py",),
        symbols_by_file={"mvp_site/world_logic.py": frozenset({"level_up"})},
    )
    entries = _spec_as_lock_entries(spec, reg)
    assert entries[0].symbols == ["level_up"]


def test_spec_as_lock_entries_partial_symbols_falls_back_to_whole(reg: Registry):
    """If any file in the domain is missing from symbols_by_file, the
    domain locks whole — same fail-closed semantics as --diff-mode."""
    spec = PRSpec(
        pr=1, branch="b1",
        files=("mvp_site/world_logic.py", "mvp_site/rewards_engine.py"),
        # only one of two has symbol info
        symbols_by_file={"mvp_site/world_logic.py": frozenset({"x"})},
    )
    entries = {e.domain: e for e in _spec_as_lock_entries(spec, reg)}
    # rewards has no symbol info -> whole-domain
    assert entries["rewards"].symbols == []
    # world is fully symbol-resolved -> symbol-level
    assert entries["world"].symbols == ["x"]


# --------------------------------------------------------------------------- #
# _pair_domain_conflicts
# --------------------------------------------------------------------------- #


def test_pair_no_shared_domain(reg: Registry):
    a = _spec_as_lock_entries(
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)), reg
    )
    b = _spec_as_lock_entries(
        PRSpec(pr=2, branch="b2", files=("mvp_site/agents.py",)), reg
    )
    assert _pair_domain_conflicts(a, b) == []


def test_pair_whole_domain_conflict(reg: Registry):
    a = _spec_as_lock_entries(
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)), reg
    )
    b = _spec_as_lock_entries(
        PRSpec(pr=2, branch="b2", files=("mvp_site/world_logic.py",)), reg
    )
    out = _pair_domain_conflicts(a, b)
    assert len(out) == 1
    assert out[0].domain == "world"
    assert out[0].symbols == ()  # whole-domain


def test_pair_disjoint_symbols_no_conflict(reg: Registry):
    a = _spec_as_lock_entries(PRSpec(
        pr=1, branch="b1", files=("mvp_site/world_logic.py",),
        symbols_by_file={"mvp_site/world_logic.py": frozenset({"foo"})},
    ), reg)
    b = _spec_as_lock_entries(PRSpec(
        pr=2, branch="b2", files=("mvp_site/world_logic.py",),
        symbols_by_file={"mvp_site/world_logic.py": frozenset({"bar"})},
    ), reg)
    assert _pair_domain_conflicts(a, b) == []


def test_pair_overlapping_symbols_reports_intersection(reg: Registry):
    a = _spec_as_lock_entries(PRSpec(
        pr=1, branch="b1", files=("mvp_site/world_logic.py",),
        symbols_by_file={"mvp_site/world_logic.py": frozenset({"foo", "shared"})},
    ), reg)
    b = _spec_as_lock_entries(PRSpec(
        pr=2, branch="b2", files=("mvp_site/world_logic.py",),
        symbols_by_file={"mvp_site/world_logic.py": frozenset({"bar", "shared"})},
    ), reg)
    out = _pair_domain_conflicts(a, b)
    assert out == [DomainConflict(domain="world", symbols=("shared",))]


# --------------------------------------------------------------------------- #
# _greedy_max_independent_set
# --------------------------------------------------------------------------- #


def test_mis_no_edges():
    assert _greedy_max_independent_set([1, 2, 3], set()) == [1, 2, 3]


def test_mis_complete_graph_picks_one():
    edges = {frozenset((1, 2)), frozenset((2, 3)), frozenset((1, 3))}
    result = _greedy_max_independent_set([1, 2, 3], edges)
    assert len(result) == 1


def test_mis_chain_picks_endpoints():
    # 1 - 2 - 3; both 1 and 3 are non-adjacent => MIS = {1, 3}
    edges = {frozenset((1, 2)), frozenset((2, 3))}
    assert _greedy_max_independent_set([1, 2, 3], edges) == [1, 3]


def test_mis_deterministic_on_ties():
    edges = {frozenset((1, 2)), frozenset((3, 4))}
    # Two components, each picks one node. Lower id wins tie => {1, 3}
    assert _greedy_max_independent_set([1, 2, 3, 4], edges) == [1, 3]


# --------------------------------------------------------------------------- #
# predict_conflicts (end-to-end, no textual)
# --------------------------------------------------------------------------- #


def test_predict_all_disjoint_one_batch(reg: Registry):
    specs = [
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=2, branch="b2", files=("mvp_site/agents.py",)),
        PRSpec(pr=3, branch="b3", files=("mvp_site/rewards_engine.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    assert plan.input_prs == [1, 2, 3]
    assert all(not pc.is_conflict for pc in plan.pairwise_conflicts)
    assert plan.parallel_batches == [[1, 2, 3]]
    assert plan.recommended_order == [1, 2, 3]
    assert plan.disclaimer == DISCLAIMER


def test_predict_two_conflict_one_clear(reg: Registry):
    specs = [
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=2, branch="b2", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=3, branch="b3", files=("mvp_site/agents.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    conflicts = [pc for pc in plan.pairwise_conflicts if pc.is_conflict]
    assert len(conflicts) == 1
    assert {conflicts[0].pr_a, conflicts[0].pr_b} == {1, 2}
    # PR 1 and 3 (or 2 and 3) can co-merge; PR 3 is in the same batch as one of them
    assert plan.parallel_batches[0] == sorted(plan.parallel_batches[0])
    # All three PRs appear somewhere in the order
    assert sorted(plan.recommended_order) == [1, 2, 3]
    # The recommended order serializes the 1-2 conflict into separate batches
    assert len(plan.parallel_batches) == 2


def test_predict_symbol_level_co_tenancy(reg: Registry):
    specs = [
        PRSpec(
            pr=1, branch="b1",
            files=("mvp_site/world_logic.py",),
            symbols_by_file={"mvp_site/world_logic.py": frozenset({"foo"})},
        ),
        PRSpec(
            pr=2, branch="b2",
            files=("mvp_site/world_logic.py",),
            symbols_by_file={"mvp_site/world_logic.py": frozenset({"bar"})},
        ),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    assert all(not pc.is_conflict for pc in plan.pairwise_conflicts)
    assert plan.parallel_batches == [[1, 2]]


def test_predict_unmapped_files_recorded(reg: Registry):
    specs = [
        PRSpec(pr=1, branch="b1", files=("some_unmapped_file.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    assert plan.unmapped_files_by_pr == {1: ["some_unmapped_file.py"]}


# --------------------------------------------------------------------------- #
# JSON output shape
# --------------------------------------------------------------------------- #


def test_plan_to_json_dict_shape(reg: Registry):
    specs = [
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=2, branch="b2", files=("mvp_site/world_logic.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    j = plan.to_json_dict()
    assert set(j) == {
        "input_prs", "pairwise_conflicts", "parallel_batches",
        "recommended_order", "unmapped_files_by_pr", "disclaimer",
    }
    assert j["disclaimer"] == DISCLAIMER
    assert j["pairwise_conflicts"][0]["prs"] == [1, 2]
    assert j["pairwise_conflicts"][0]["domain_conflicts"][0]["domain"] == "world"


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #


def _write_plan_and_reg(tmp_path: Path) -> tuple[Path, Path, Path]:
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {
            "world": {"paths": ["mvp_site/world_logic.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
        }
    }))
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "prs": [
            {"pr": 1, "branch": "b1", "files": ["mvp_site/world_logic.py"]},
            {"pr": 2, "branch": "b2", "files": ["mvp_site/world_logic.py"]},
            {"pr": 3, "branch": "b3", "files": ["mvp_site/agents.py"]},
        ]
    }))
    log = tmp_path / "log.jsonl"
    return reg, plan, log


def test_cli_predict_conflicts_human(tmp_path: Path):
    reg, plan, log = _write_plan_and_reg(tmp_path)
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(plan), "--no-textual",
    ], capture_output=True, text=True)
    # PR 1 vs 2 conflict on world domain -> exit 1
    assert r.returncode == 1, r.stderr
    assert "PR#1 <-> PR#2" in r.stdout
    assert "domain=world" in r.stdout
    assert "Risk-reduction signal" in r.stdout


def test_cli_predict_conflicts_json(tmp_path: Path):
    reg, plan, log = _write_plan_and_reg(tmp_path)
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(plan), "--no-textual", "--json",
    ], capture_output=True, text=True)
    assert r.returncode == 1, r.stderr
    payload = json.loads(r.stdout)
    assert payload["input_prs"] == [1, 2, 3]
    assert len(payload["pairwise_conflicts"]) == 1
    assert payload["pairwise_conflicts"][0]["prs"] == [1, 2]
    assert payload["disclaimer"].startswith("Risk-reduction signal")
    assert sorted(payload["recommended_order"]) == [1, 2, 3]


def test_cli_predict_conflicts_all_disjoint_exit0(tmp_path: Path):
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {
            "world": {"paths": ["mvp_site/world_logic.py"]},
            "agents": {"paths": ["mvp_site/agents.py"]},
        }
    }))
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump({
        "prs": [
            {"pr": 1, "branch": "b1", "files": ["mvp_site/world_logic.py"]},
            {"pr": 2, "branch": "b2", "files": ["mvp_site/agents.py"]},
        ]
    }))
    log = tmp_path / "log.jsonl"
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(plan), "--no-textual", "--json",
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["pairwise_conflicts"] == []
    assert payload["parallel_batches"] == [[1, 2]]


def test_cli_predict_conflicts_missing_plan_exit2(tmp_path: Path):
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    log = tmp_path / "log.jsonl"
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(tmp_path / "missing.yaml"),
        "--no-textual",
    ], capture_output=True, text=True)
    assert r.returncode == 2


def test_cli_predict_conflicts_accepts_global_opts_after_subcommand(tmp_path: Path):
    """Backward-compat sanity: global flags after the subcommand parse fine."""
    reg, plan, log = _write_plan_and_reg(tmp_path)
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "predict-conflicts", "--plan", str(plan), "--no-textual",
        "--registry", str(reg), "--log", str(log),
    ], capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr  # parser must not reject


# --------------------------------------------------------------------------- #
# Textual conflict mocking (no real git refs needed)
# --------------------------------------------------------------------------- #


def test_textual_conflict_mocked(reg: Registry, monkeypatch):
    """When ``_git_merge_tree_conflicts`` reports textual conflicts on
    PRs that have no domain overlap, predict_conflicts must still surface
    them and treat them as conflict-graph edges."""
    from merge_train import predict as predict_mod
    from merge_train.predict import TextualConflict

    def fake_merge_tree(a, b, *, base, cwd):
        if (a, b) == ("b1", "b2") or (a, b) == ("b2", "b1"):
            return [TextualConflict(file="pyproject.toml")]
        return []

    monkeypatch.setattr(predict_mod, "_git_merge_tree_conflicts", fake_merge_tree)

    specs = [
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=2, branch="b2", files=("mvp_site/agents.py",)),
        PRSpec(pr=3, branch="b3", files=("mvp_site/rewards_engine.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=True)
    conflicts = [pc for pc in plan.pairwise_conflicts if pc.is_conflict]
    assert len(conflicts) == 1
    pair = conflicts[0]
    assert {pair.pr_a, pair.pr_b} == {1, 2}
    assert pair.textual_conflicts[0].file == "pyproject.toml"
    # PRs 1 and 2 must end up in different batches due to textual edge
    batches = plan.parallel_batches
    assert not any({1, 2}.issubset(set(b)) for b in batches)


# --------------------------------------------------------------------------- #
# _parse_merge_tree_z — parser unit tests (no git required)
# --------------------------------------------------------------------------- #


def test_parse_merge_tree_z_no_conflict():
    """Empty stdout -> no paths."""
    assert _parse_merge_tree_z("") == []


def test_parse_merge_tree_z_single_file():
    """Realistic stdout: tree-OID \\0 path \\0 \\0 messages..."""
    stdout = "abc123def456\0pyproject.toml\0\0Auto-merging pyproject.toml\nCONFLICT (content): Merge conflict in pyproject.toml\n"
    out = _parse_merge_tree_z(stdout)
    assert len(out) == 1
    assert out[0].file == "pyproject.toml"


def test_parse_merge_tree_z_multiple_files():
    """OID + multiple conflict paths in the first block."""
    stdout = "abc123\0f.txt\0g.txt\0h.txt\0\0Auto-merging f.txt\nCONFLICT (content): Merge conflict in f.txt"
    out = _parse_merge_tree_z(stdout)
    assert [c.file for c in out] == ["f.txt", "g.txt", "h.txt"]


def test_parse_merge_tree_z_rejects_log_message_lines():
    """Regression: pre-fix parser used .splitlines() + filter on '.', so
    'Auto-merging f.txt' and 'CONFLICT (content): Merge conflict in f.txt'
    leaked into the conflict list. The -z parser uses NUL boundaries
    and must NOT include log-message lines."""
    stdout = "abc\0f.txt\0\0Auto-merging f.txt\nCONFLICT (content): Merge conflict in f.txt"
    out = _parse_merge_tree_z(stdout)
    assert [c.file for c in out] == ["f.txt"]
    assert "Auto-merging f.txt" not in [c.file for c in out]
    assert not any("CONFLICT" in c.file for c in out)


def test_parse_merge_tree_z_no_double_nul_block():
    """If git emits only tree-OID + paths and EOF (no \\0\\0 boundary),
    everything before EOF is the first block."""
    stdout = "abc\0only-one.txt"
    out = _parse_merge_tree_z(stdout)
    assert [c.file for c in out] == ["only-one.txt"]


# --------------------------------------------------------------------------- #
# Real-subprocess test for _git_merge_tree_conflicts (the CRITICAL bugs)
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _make_conflicting_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with two branches that conflict on one file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "conflict.txt").write_text("base\nshared\nbase\n")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "br_a")
    (repo / "conflict.txt").write_text("A-edit\nshared\nbase\n")
    _git(repo, "commit", "-q", "-am", "a")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "br_b")
    (repo / "conflict.txt").write_text("B-edit\nshared\nbase\n")
    _git(repo, "commit", "-q", "-am", "b")
    return repo


def test_git_merge_tree_real_subprocess_detects_conflict(tmp_path: Path):
    """End-to-end: real git merge-tree on a real conflicting repo must
    identify the conflicting file. Covers the modern (>=2.40) path on
    machines with new git AND the legacy fallback on machines with old
    git — whichever the test host runs."""
    repo = _make_conflicting_repo(tmp_path)
    conflicts = _git_merge_tree_conflicts(
        "br_a", "br_b", base="main", cwd=repo,
    )
    assert len(conflicts) == 1, f"expected 1 conflict, got {conflicts}"
    assert conflicts[0].file == "conflict.txt"


def test_git_merge_tree_real_subprocess_no_conflict(tmp_path: Path):
    """Disjoint edits on disjoint files must report no textual conflict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a")
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "br_a")
    (repo / "a.txt").write_text("a2")
    _git(repo, "commit", "-q", "-am", "a2")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "br_b")
    (repo / "b.txt").write_text("b2")
    _git(repo, "commit", "-q", "-am", "b2")
    assert _git_merge_tree_conflicts("br_a", "br_b", base="main", cwd=repo) == []


# --------------------------------------------------------------------------- #
# JSON int-key coercion + larger conflict graph
# --------------------------------------------------------------------------- #


def test_to_json_dict_stringifies_unmapped_pr_keys(reg: Registry):
    """Regression: json.dumps would silently coerce int dict keys to
    strings. We stringify in to_json_dict() so the contract is explicit."""
    specs = [PRSpec(pr=42, branch="b1", files=("unmapped.py",))]
    plan = predict_conflicts(specs, reg, include_textual=False)
    j = plan.to_json_dict()
    assert "42" in j["unmapped_files_by_pr"]
    assert 42 not in j["unmapped_files_by_pr"]
    # And the JSON round-trips cleanly (no coercion surprise).
    round_tripped = json.loads(json.dumps(j))
    assert "42" in round_tripped["unmapped_files_by_pr"]


def test_predict_triangle_graph(reg: Registry):
    """3 PRs all pairwise-conflicting (a triangle K3 in the conflict graph).
    Greedy MIS must pick exactly 1 PR per batch (3 batches total)."""
    specs = [
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=2, branch="b2", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=3, branch="b3", files=("mvp_site/world_logic.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    conflicts = [pc for pc in plan.pairwise_conflicts if pc.is_conflict]
    assert len(conflicts) == 3  # 3-choose-2
    # Triangle => MIS of size 1 each batch => 3 batches
    assert len(plan.parallel_batches) == 3
    assert all(len(b) == 1 for b in plan.parallel_batches)
    assert sorted(plan.recommended_order) == [1, 2, 3]


def test_predict_larger_graph_5_prs_2_batches(reg: Registry):
    """5 PRs: {1,2} conflict on world; {3,4} conflict on rewards; 5 alone
    on agents. Optimal MIS is {1 or 2, 3 or 4, 5} => 2 batches max."""
    specs = [
        PRSpec(pr=1, branch="b1", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=2, branch="b2", files=("mvp_site/world_logic.py",)),
        PRSpec(pr=3, branch="b3", files=("mvp_site/rewards_engine.py",)),
        PRSpec(pr=4, branch="b4", files=("mvp_site/rewards_engine.py",)),
        PRSpec(pr=5, branch="b5", files=("mvp_site/agents.py",)),
    ]
    plan = predict_conflicts(specs, reg, include_textual=False)
    assert len(plan.parallel_batches) == 2
    # PR 5 must be in the first batch (no conflicts, max scheduling freedom)
    assert 5 in plan.parallel_batches[0]
    assert sorted(plan.recommended_order) == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# load_plan error-path coverage (load_plan accepted prs:null silently)
# --------------------------------------------------------------------------- #


def test_load_plan_rejects_null_prs(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("prs: null\n")
    with pytest.raises(ValueError, match="missing required key 'prs'"):
        load_plan(p)


def test_load_plan_rejects_missing_prs(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("other_key: value\n")
    with pytest.raises(ValueError, match="missing required key 'prs'"):
        load_plan(p)


def test_load_plan_rejects_missing_pr_field(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({
        "prs": [{"branch": "no-pr-number", "files": ["a.py"]}]
    }))
    with pytest.raises(ValueError, match="missing required 'pr' field"):
        load_plan(p)


def test_load_plan_rejects_null_pr_value(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("prs:\n  - pr: null\n    branch: b\n")
    with pytest.raises(ValueError, match="pr must not be null"):
        load_plan(p)


def test_load_plan_rejects_non_int_pr(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("prs:\n  - pr: not-a-number\n    branch: b\n")
    with pytest.raises(ValueError):
        load_plan(p)


def test_load_plan_rejects_top_level_list(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("- pr: 1\n")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_plan(p)


def test_cli_predict_conflicts_malformed_yaml_exit2(tmp_path: Path):
    """Regression: yaml.YAMLError must produce exit 2 with a useful message."""
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({"domains": {"d1": {"paths": ["a.py"]}}}))
    plan = tmp_path / "plan.yaml"
    plan.write_text("prs: [\nbroken yaml: : :\n")  # bogus
    log = tmp_path / "log.jsonl"
    r = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(plan), "--no-textual",
    ], capture_output=True, text=True)
    assert r.returncode == 2
    assert "malformed plan" in r.stderr


def test_cli_predict_conflicts_exit_codes_pin_contract(tmp_path: Path):
    """Pin the documented exit-code table: 0 no conflict, 1 conflict, 2 plan error."""
    reg = tmp_path / "reg.yaml"
    reg.write_text(yaml.safe_dump({
        "domains": {
            "d1": {"paths": ["a.py"]},
            "d2": {"paths": ["b.py"]},
        }
    }))
    log = tmp_path / "log.jsonl"
    # 0: all disjoint
    plan0 = tmp_path / "p0.yaml"
    plan0.write_text(yaml.safe_dump({
        "prs": [
            {"pr": 1, "branch": "b", "files": ["a.py"]},
            {"pr": 2, "branch": "b", "files": ["b.py"]},
        ]
    }))
    r0 = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(plan0), "--no-textual",
    ], capture_output=True, text=True)
    assert r0.returncode == 0, r0.stderr
    # 1: conflict
    plan1 = tmp_path / "p1.yaml"
    plan1.write_text(yaml.safe_dump({
        "prs": [
            {"pr": 1, "branch": "b", "files": ["a.py"]},
            {"pr": 2, "branch": "b", "files": ["a.py"]},
        ]
    }))
    r1 = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(plan1), "--no-textual",
    ], capture_output=True, text=True)
    assert r1.returncode == 1, r1.stderr
    # 2: missing plan
    r2 = subprocess.run([
        sys.executable, "-m", "merge_train.domain_lock",
        "--registry", str(reg), "--log", str(log),
        "predict-conflicts", "--plan", str(tmp_path / "nope.yaml"),
        "--no-textual",
    ], capture_output=True, text=True)
    assert r2.returncode == 2
