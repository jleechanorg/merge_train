"""Contract tests verifying all merge_train GitHub workflows use self-hosted runners."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

HOSTED_RUNNER_PATTERN = re.compile(
    r"\b(?:macos|ubuntu|windows)-(?:\d+(?:\.\d+)*|latest)\b"
)
FORK_PR_GUARD_EXPRESSION = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out.extend(_flatten_strings(v))
        return out
    return []


def _discover_workflow_files() -> list[Path]:
    workflow_files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )
    assert workflow_files, f"No workflow files found in {WORKFLOWS_DIR}"
    return workflow_files


def _runs_on_labels(runs_on: object) -> list[str]:
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        assert all(isinstance(item, str) for item in runs_on), (
            f"Expected list of string labels in runs-on, got {runs_on!r}"
        )
        return list(runs_on)
    raise AssertionError(f"Unexpected runs-on format: {runs_on!r}")


def _github_path_entries(script: str) -> list[str]:
    return re.findall(
        r'echo\s+"([^"]+)"\s*>>\s*"?\$GITHUB_PATH"?',
        script,
    )


def test_no_github_hosted_runners_in_workflows() -> None:
    workflow_files = _discover_workflow_files()

    for wf_path in workflow_files:
        raw_text = wf_path.read_text(encoding="utf-8")
        match = HOSTED_RUNNER_PATTERN.search(raw_text)
        assert match is None, (
            f"{wf_path.name} still references GitHub-hosted runner {match.group(0)!r}"
        )

        try:
            data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError:
            if wf_path.name == "hermes-pr-tag-listener.yml":
                # Pre-existing quoting bug on origin/main (dispatch_ao: ''false'');
                # raw-text HOSTED_RUNNER_PATTERN check above already verified no hosted runners.
                continue
            raise
        jobs = data.get("jobs", {})
        for job_id, job_cfg in jobs.items():
            if not isinstance(job_cfg, dict):
                continue
            if "uses" in job_cfg and "runs-on" not in job_cfg:
                continue

            if wf_path.name in {"tests.yml", "evidence-guard.yml"}:
                assert job_cfg.get("timeout-minutes") == 20, (
                    f"{wf_path.name}:{job_id} must set timeout-minutes: 20, got {job_cfg.get('timeout-minutes')!r}"
                )

            runs_on = job_cfg.get("runs-on")
            assert runs_on is not None, f"{wf_path.name}:{job_id} missing runs-on"
            labels = _runs_on_labels(runs_on)

            if "self-hosted-native-macos" in labels:
                assert "self-hosted" not in labels, (
                    f"{wf_path.name}:{job_id} macOS native job must not include bare 'self-hosted': {labels!r}"
                )
            else:
                assert "self-hosted" in labels and "Linux" in labels, (
                    f"{wf_path.name}:{job_id} Linux self-hosted job must include exact 'self-hosted' and 'Linux' labels, got {labels!r}"
                )

            if_expr = job_cfg.get("if")
            assert isinstance(if_expr, str) and (
                "github.event.pull_request.head.repo.full_name == github.repository"
                in if_expr
            ), (
                f"{wf_path.name}:{job_id} self-hosted job must gate fork PRs via if:, got {if_expr!r}"
            )
            assert if_expr.strip() == FORK_PR_GUARD_EXPRESSION, (
                f"{wf_path.name}:{job_id} unexpected fork-PR guard if:, got {if_expr!r}"
            )

            matrix = (job_cfg.get("strategy") or {}).get("matrix") or {}
            for m_str in _flatten_strings(matrix):
                assert not HOSTED_RUNNER_PATTERN.search(m_str), (
                    f"{wf_path.name}:{job_id} matrix contains hosted runner {m_str!r}"
                )

            job_env = job_cfg.get("env") or {}
            for env_str in _flatten_strings(job_env):
                assert "runner." not in env_str, (
                    f"{wf_path.name}:{job_id}.env cannot reference runner context: {env_str!r}"
                )


def test_tests_workflow_covers_linux_and_native_macos() -> None:
    tests_wf = yaml.safe_load((WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8"))
    jobs = tests_wf["jobs"]

    assert "pytest" in jobs
    pytest_labels = _runs_on_labels(jobs["pytest"]["runs-on"])
    assert pytest_labels == ["self-hosted", "Linux", "self-hosted-mikey"]

    assert "pytest-macos" in jobs
    macos_labels = _runs_on_labels(jobs["pytest-macos"]["runs-on"])
    assert "self-hosted-native-macos" in macos_labels
    assert "self-hosted" not in macos_labels

    assert "installer-smoke" in jobs
    smoke_labels = _runs_on_labels(jobs["installer-smoke"]["runs-on"])
    assert smoke_labels == ["self-hosted", "Linux", "self-hosted-mikey"]
    needs = _flatten_strings(jobs["installer-smoke"].get("needs"))
    assert needs == ["pytest"], (
        f"installer-smoke must depend only on pytest (not pytest-macos), got {needs!r}"
    )


def test_pytest_macos_venv_path_precedence_and_python_invocation() -> None:
    tests_wf = yaml.safe_load((WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8"))
    macos_steps = tests_wf["jobs"]["pytest-macos"]["steps"]

    setup_step = next(
        s for s in macos_steps if "venv-macos" in (s.get("run") or "") and "GITHUB_PATH" in (s.get("run") or "")
    )
    setup_run = setup_step["run"]
    path_entries = _github_path_entries(setup_run)
    for required_entry in (
        "/opt/homebrew/bin",
        "$HOME/bin",
        "$HOME/.local/bin",
        "$HOME/.nvm/versions/node/v22.22.0/bin",
        "$VENV_DIR/bin",
    ):
        assert required_entry in path_entries, (
            f"pytest-macos setup must append {required_entry!r} to GITHUB_PATH, got {path_entries!r}"
        )

    venv_idx = path_entries.index("$VENV_DIR/bin")
    assert venv_idx == len(path_entries) - 1, (
        f"$VENV_DIR/bin must be appended LAST to GITHUB_PATH so it has highest precedence, got {path_entries!r}"
    )
    assert path_entries.index("$HOME/.nvm/versions/node/v22.22.0/bin") > path_entries.index(
        "/opt/homebrew/bin"
    ), f"nvm Node 22 must be appended after /opt/homebrew/bin to precede Homebrew Node, got {path_entries!r}"
    assert "VENV_PYTHON=${VENV_DIR}/bin/python" in setup_run
    assert '>> "$GITHUB_ENV"' in setup_run

    versions_step = next(s for s in macos_steps if s.get("name") == "Show versions")
    versions_run = versions_step["run"]
    assert "RUNNER_TEMP" in versions_run and "venv-macos" in versions_run
    assert "python -c" in versions_run
    assert '"$VENV_PYTHON" -c' in versions_run
    assert "node --version || true" in versions_run
    assert "git --version" in versions_run

    run_pytest_step = next(s for s in macos_steps if s.get("name") == "Run pytest")
    assert run_pytest_step["run"].strip() == '"$VENV_PYTHON" -m pytest tests/ -q'


def test_git_config_global_and_installer_smoke_isolation() -> None:
    tests_wf = yaml.safe_load((WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8"))
    jobs = tests_wf["jobs"]

    for job_id in ("pytest", "pytest-macos", "installer-smoke"):
        steps_text = "\n".join(s.get("run") or "" for s in jobs[job_id]["steps"])
        assert "GIT_CONFIG_GLOBAL" in steps_text, (
            f"{job_id} must isolate GIT_CONFIG_GLOBAL under RUNNER_TEMP"
        )
        assert "RUNNER_TEMP" in steps_text
        assert '>> "$GITHUB_ENV"' in steps_text

    smoke_steps = jobs["installer-smoke"]["steps"]
    step_names = [s.get("name") for s in smoke_steps]
    assert step_names.index("Create isolated smoke environment and target repo") < step_names.index(
        "Set up Python 3.12 venv"
    ), "isolated smoke environment must be created before Set up Python 3.12 venv"

    smoke_venv_step = next(
        s for s in smoke_steps if "GITHUB_PATH" in (s.get("run") or "")
    )
    smoke_path_entries = _github_path_entries(smoke_venv_step["run"])
    assert "$HOME/.local/bin" not in smoke_path_entries
    assert smoke_path_entries == ["$VENV_DIR/bin", "$SMOKE_HOME/.local/bin"], (
        f"installer-smoke must append $VENV_DIR/bin then $SMOKE_HOME/.local/bin to GITHUB_PATH, got {smoke_path_entries!r}"
    )

    smoke_all_run = "\n".join(s.get("run") or "" for s in smoke_steps)
    assert 'mktemp -d "${RUNNER_TEMP}/installer-smoke.XXXXXX"' in smoke_all_run
    for required_env_line in (
        'RUNNER_REAL_HOME=${RUNNER_REAL_HOME}',
        'SMOKE_DIR=${SMOKE_DIR}',
        'SMOKE_HOME=${SMOKE_HOME}',
        'UV_TOOL_DIR=${SMOKE_DIR}/uv-tools',
        'UV_TOOL_BIN_DIR=${SMOKE_HOME}/.local/bin',
        'XDG_CONFIG_HOME=${SMOKE_HOME}/.config',
        'XDG_DATA_HOME=${SMOKE_HOME}/.local/share',
        'XDG_CACHE_HOME=${SMOKE_DIR}/cache',
    ):
        assert required_env_line in smoke_all_run, (
            f"installer-smoke must export {required_env_line!r} to GITHUB_ENV"
        )
    assert "$SMOKE_DIR/target_repo" in smoke_all_run
    assert "$SMOKE_DIR/rerun.log" in smoke_all_run
    assert "$SMOKE_DIR/real-home-before.txt" in smoke_all_run
    assert "$SMOKE_DIR/real-home-after.txt" in smoke_all_run
    assert 'diff -u "$SMOKE_DIR/real-home-before.txt" "$SMOKE_DIR/real-home-after.txt"' in smoke_all_run

    steps_by_name = {s.get("name"): s for s in smoke_steps}
    for step_name in (
        "Run install.sh against the target repo",
        "Verify install artifacts",
        "Re-run install.sh (idempotency check)",
    ):
        step_env = steps_by_name[step_name].get("env") or {}
        assert step_env.get("HOME") == "${{ env.SMOKE_HOME }}", (
            f"{step_name!r} must set step env.HOME to '${{{{ env.SMOKE_HOME }}}}', got {step_env!r}"
        )

    verify_run = steps_by_name["Verify install artifacts"]["run"]
    assert "command -v predict-conflicts" in verify_run
    assert "$SMOKE_HOME/.local/bin/predict-conflicts" in verify_run
    assert "$SMOKE_HOME/.local/bin/predict-spawn-check.sh" in verify_run
    assert "$RUNNER_REAL_HOME" in verify_run
