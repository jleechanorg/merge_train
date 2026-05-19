from __future__ import annotations

import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_V03 = REPO_ROOT / "evidence" / "v0.3"


def test_v03_agent_transcripts_have_checksum_sidecars() -> None:
    transcript_dir = EVIDENCE_V03 / "agent_transcripts"
    transcripts = sorted(transcript_dir.glob("slot-*.log"))

    assert len(transcripts) == 20
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in transcripts
        if not path.with_suffix(path.suffix + ".sha256").is_file()
    ]

    assert missing == []


def test_v03_required_runbook_artifact_paths_exist() -> None:
    runbook = (REPO_ROOT / "docs" / "opencode_md_area_lock_e2e.md").read_text()

    if "- `hook_config/`" in runbook:
        hook_config_files = sorted((EVIDENCE_V03 / "hook_config").glob("*"))
        assert hook_config_files, (
            "runbook lists hook_config/ as required, but evidence/v0.3/hook_config "
            "is missing"
        )


def test_v03_checksums_cover_all_required_artifact_files() -> None:
    checksum_lines = (EVIDENCE_V03 / "checksums.txt").read_text().splitlines()
    checksum_paths = {line.split(maxsplit=1)[1] for line in checksum_lines if line.strip()}
    required_artifacts = [
        path
        for path in EVIDENCE_V03.rglob("*")
        if path.is_file() and not path.name.endswith(".sha256")
    ]
    master_indexed_artifacts = [
        path for path in required_artifacts if path.name != "checksums.txt"
    ]
    missing_from_master = [
        path.relative_to(EVIDENCE_V03).as_posix()
        for path in master_indexed_artifacts
        if path.relative_to(EVIDENCE_V03).as_posix() not in checksum_paths
    ]
    missing_sidecars = [
        path.relative_to(EVIDENCE_V03).as_posix()
        for path in required_artifacts
        if not Path(str(path) + ".sha256").is_file()
    ]
    invalid_sidecars = []
    for path in required_artifacts:
        sidecar = Path(str(path) + ".sha256")
        if not sidecar.is_file():
            continue
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        actual = sidecar.read_text().split(maxsplit=1)[0]
        if actual != expected:
            invalid_sidecars.append(path.relative_to(EVIDENCE_V03).as_posix())

    assert missing_from_master == []
    assert missing_sidecars == []
    assert invalid_sidecars == []
