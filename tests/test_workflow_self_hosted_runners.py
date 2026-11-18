"""Contract tests verifying all merge_train GitHub workflows use self-hosted runners."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

HOSTED_RUNNER_PATTERN = re.compile(r"\b(?:macos|ubuntu)-(?:\d+(?:\.\d+)*|latest)\b")
RUNNER_WORKFLOWS = ("tests.yml", "evidence-guard.yml")


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


def test_no_github_hosted_runners_in_workflows() -> None:
    workflow_files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )
    assert workflow_files, f"No workflow files found in {WORKFLOWS_DIR}"

    for wf_path in workflow_files:
        raw_text = wf_path.read_text(encoding="utf-8")
        match = HOSTED_RUNNER_PATTERN.search(raw_text)
        assert match is None, (
            f"{wf_path.name} still references GitHub-hosted runner {match.group(0)!r}"
        )

    for wf_name in RUNNER_WORKFLOWS:
        wf_path = WORKFLOWS_DIR / wf_name
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        jobs = data.get("jobs", {})
        for job_id, job_cfg in jobs.items():
            if not isinstance(job_cfg, dict):
                continue
            if "uses" in job_cfg and "runs-on" not in job_cfg:
                continue
            runs_on = job_cfg.get("runs-on")
            assert runs_on is not None, f"{wf_path.name}:{job_id} missing runs-on"
            runs_on_strs = _flatten_strings(runs_on)
            assert any(
                "self-hosted" in s for s in runs_on_strs
            ), f"{wf_path.name}:{job_id} must run on self-hosted, got {runs_on!r}"

            matrix = (job_cfg.get("strategy") or {}).get("matrix") or {}
            for m_str in _flatten_strings(matrix):
                assert not HOSTED_RUNNER_PATTERN.search(m_str), (
                    f"{wf_path.name}:{job_id} matrix contains hosted runner {m_str!r}"
                )


def test_tests_workflow_covers_linux_and_native_macos() -> None:
    tests_wf = yaml.safe_load((WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8"))
    jobs = tests_wf["jobs"]

    assert "pytest" in jobs
    assert "self-hosted" in _flatten_strings(jobs["pytest"]["runs-on"])
    assert "self-hosted-mikey" in _flatten_strings(jobs["pytest"]["runs-on"])

    assert "pytest-macos" in jobs
    assert jobs["pytest-macos"]["name"] == "pytest (macOS native)"
    assert "self-hosted-native-macos" in _flatten_strings(
        jobs["pytest-macos"]["runs-on"]
    )

    assert "installer-smoke" in jobs
    assert "self-hosted" in _flatten_strings(jobs["installer-smoke"]["runs-on"])
    assert "self-hosted-mikey" in _flatten_strings(jobs["installer-smoke"]["runs-on"])
    needs = _flatten_strings(jobs["installer-smoke"].get("needs"))
    assert "pytest" in needs and "pytest-macos" in needs
