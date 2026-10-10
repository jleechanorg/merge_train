"""The spawn hook preserves CLI arguments without evaluating their contents."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "merge_train/hooks/predict-spawn-check.sh"


def _write_script(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _run_hook(
    tmp_path: Path,
    cli: str,
    *,
    registry: Path | None = None,
    repo: str = "",
    output: str = "{}",
    status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the real hook with deterministic Git and prediction-process seams."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.bin"
    _write_script(
        bindir / "git",
        'if [[ "$1" == rev-parse ]]; then printf "%s\\n" "$PWD"; else exit 1; fi\n',
    )
    record = (
        'printf "%s\\0" "$@" > "$ARGV_LOG"\n'
        'printf "%s" "$PREDICTION_OUTPUT"\n'
        'exit "$PREDICTION_STATUS"\n'
    )
    if cli == "installed":
        _write_script(bindir / "predict-conflicts", record)
    _write_script(
        bindir / "python3",
        'if [[ "$1" == -m ]]; then\n'
        + record
        + "fi\nexec " + shlex.quote(sys.executable) + ' "$@"\n',
    )
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "MERGE_TRAIN_FILES": "a.txt",
        "MERGE_TRAIN_PR": "5",
        "MERGE_TRAIN_REPO": repo,
        "ARGV_LOG": str(argv_log),
        "PREDICTION_OUTPUT": output,
        "PREDICTION_STATUS": str(status),
    }
    if registry is not None:
        registry.write_text("domains: {}\n")
        env["MERGE_TRAIN_REGISTRY"] = str(registry)
    result = subprocess.run(
        ["/bin/bash", str(HOOK)], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert argv_log.is_file(), f"prediction CLI was not invoked: {result.stderr}"
    return result, argv_log.read_bytes().decode().rstrip("\0").split("\0")


@pytest.mark.parametrize("cli", ["installed", "fallback"])
@pytest.mark.parametrize("optional", ["none", "registry", "repo", "both"])
def test_spawn_hook_passes_exact_cli_arguments(tmp_path: Path, cli: str, optional: str) -> None:
    """Both entry points accept flags directly, even with empty optional arrays."""
    registry = tmp_path / "my reg.yaml" if optional in ("registry", "both") else None
    repo = "fixture/repo" if optional in ("repo", "both") else ""
    result, argv = _run_hook(tmp_path, cli, registry=registry, repo=repo)
    expected = [] if cli == "installed" else ["-m", "merge_train.predict"]
    if registry is not None:
        expected += ["--registry", str(registry)]
    expected += ["--from-prs", "5"]
    if repo:
        expected += ["--repo", repo]
    expected += ["--json"]
    assert argv == expected, result.stderr


@pytest.mark.parametrize("cli", ["installed", "fallback"])
@pytest.mark.parametrize("filename", [
    "my reg; printf marker > MARKER; #.yaml",
    "my reg$(printf marker > MARKER).yaml",
])
def test_spawn_hook_registry_is_literal(tmp_path: Path, cli: str, filename: str) -> None:
    """A registry filename must neither split nor execute shell metacharacters."""
    registry = tmp_path / filename
    result, argv = _run_hook(tmp_path, cli, registry=registry, repo="fixture/repo")
    assert not (tmp_path / "MARKER").exists(), "registry path was executed as shell text"
    assert argv[argv.index("--registry") + 1] == str(registry), result.stderr


@pytest.mark.parametrize("cli", ["installed", "fallback"])
@pytest.mark.parametrize(("output", "status", "warning"), [
    ("", 2, "conflict prediction returned no data"),
    ("not JSON", 0, "predicting conflicts"),
    (json.dumps({"pairwise_conflicts": [{"is_conflict": True, "pr_a": 5, "pr_b": 6,
      "domain_conflicts": [{"domain": "example", "overlapping_symbols": ["symbol"]}]}]}),
     1, "WARN  PR#5 vs PR#6"),
])
def test_spawn_hook_prediction_outcomes_remain_nonblocking(
    tmp_path: Path, cli: str, output: str, status: int, warning: str,
) -> None:
    """CLI failure, malformed JSON, and reported conflicts retain exit-zero behavior."""
    result, _ = _run_hook(tmp_path, cli, output=output, status=status)
    assert warning in result.stderr
    assert result.stdout == ""
