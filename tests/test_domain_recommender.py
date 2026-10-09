from pathlib import Path

from merge_train.domain_recommender import recommend_domains, to_yaml_dict


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def level_up():\n    return 1\n")
    (repo / "b.py").write_text("def compute_dice():\n    return 2\n")
    (repo / "c.py").write_text("def reward():\n    return 3\n")

    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    # Use a real-shaped email (not the RFC 2606 reserved "example.com") so the
    # pre-commit git-identity guard doesn't reject the test's commit. The guard
    # is enforced globally (see ~/.claude/hooks/pre-commit-git-identity-example-com-guard.sh);
    # we satisfy it here by using a non-placeholder domain.
    subprocess.run(
        ["git", "config", "user.email", "tester@merge-train.local"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )

    (repo / "a.py").write_text("def level_up():\n    return 2\n")
    (repo / "b.py").write_text("def compute_dice():\n    return 3\n")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "hot"], cwd=repo, check=True, capture_output=True
    )
    return repo


def test_recommend_domains_and_symbol_groups(tmp_path: Path):
    repo = _init_repo(tmp_path)
    sugg = recommend_domains(repo, since_days=365, top_n=3)
    assert sugg
    payload = to_yaml_dict(sugg)
    assert "domains" in payload
    assert "symbol_groups" in payload
    first = next(iter(payload["symbol_groups"].values()))
    assert "symbols" in first
