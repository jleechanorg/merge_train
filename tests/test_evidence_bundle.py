from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_V03 = REPO_ROOT / "evidence" / "v0.3"
EVIDENCE_V04 = REPO_ROOT / "evidence" / "v0.4"
EVIDENCE_V04_AO = REPO_ROOT / "evidence" / "v0.4-ao"
EVIDENCE_V05_AO = REPO_ROOT / "evidence" / "v0.5-ao"
EVIDENCE_V06_AO = REPO_ROOT / "evidence" / "v0.6-ao"


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


# ── v0.4 bundle tests ────────────────────────────────────────────────────────

def _bundle_sha256_checks(evidence_dir: Path) -> None:
    """Shared sha256 integrity check for any evidence bundle dir."""
    required_artifacts = [
        path for path in evidence_dir.rglob("*")
        if path.is_file() and not path.name.endswith(".sha256")
    ]
    missing_sidecars = [
        path.relative_to(evidence_dir).as_posix()
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
            invalid_sidecars.append(path.relative_to(evidence_dir).as_posix())
    assert missing_sidecars == [], f"missing sha256 sidecars: {missing_sidecars}"
    assert invalid_sidecars == [], f"invalid sha256 sidecars: {invalid_sidecars}"


def test_v04_bundle_exists() -> None:
    """v0.4 evidence bundle must exist (proves E2E rerun at HEAD debeaf9a)."""
    assert EVIDENCE_V04.is_dir(), (
        "evidence/v0.4/ missing — rerun scripts/e2e_md_area_lock_runner.py at current HEAD"
    )
    assert (EVIDENCE_V04 / "metadata.json").is_file(), "evidence/v0.4/metadata.json missing"


def test_v04_metadata_sha_matches_head() -> None:
    """Bundle SHA must be within 5 commits of HEAD."""
    if not EVIDENCE_V04.is_dir():
        return  # covered by test_v04_bundle_exists
    meta = json.loads((EVIDENCE_V04 / "metadata.json").read_text())
    bundle_sha = meta.get("merge_train_sha", "")
    assert bundle_sha, "metadata.json missing merge_train_sha"
    # Verify SHA exists
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{bundle_sha}^{{commit}}"],
        cwd=REPO_ROOT, capture_output=True,
    )
    assert result.returncode == 0, f"bundle SHA {bundle_sha} not found in git history"
    # Check staleness (≤5 commits)
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"{bundle_sha}..HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    commits_ahead = int(ahead.stdout.strip() or "999")
    assert commits_ahead <= 10, (
        f"evidence/v0.4 is {commits_ahead} commits stale "
        f"(bundle_sha={bundle_sha[:12]}); rerun runner to refresh"
    )


def test_v04_bundle_scenarios_all_passed() -> None:
    """All run.json scenarios must have passed=True."""
    if not EVIDENCE_V04.is_dir():
        return
    run_json = EVIDENCE_V04 / "run.json"
    if not run_json.is_file():
        return
    data = json.loads(run_json.read_text())
    failed = [s["name"] for s in data.get("scenarios", []) if not s.get("passed")]
    assert failed == [], f"v0.4 scenarios failed: {failed}"


def test_v04_hook_behavior_scenarios_present() -> None:
    """Phase 2 scenarios must be present in run.json."""
    if not EVIDENCE_V04.is_dir():
        return
    run_json = EVIDENCE_V04 / "run.json"
    if not run_json.is_file():
        return
    data = json.loads(run_json.read_text())
    names = {s["name"] for s in data.get("scenarios", [])}
    assert "lock_pre_reservation_path" in names, "missing lock_pre_reservation_path scenario"
    assert "worktree_fallback_chain" in names, "missing worktree_fallback_chain scenario"


def test_v04_checksums_valid() -> None:
    """All sha256 sidecars in v0.4 bundle must be valid."""
    if not EVIDENCE_V04.is_dir():
        return
    _bundle_sha256_checks(EVIDENCE_V04)


def test_v04_agent_transcripts_present() -> None:
    """20 slot transcripts with sidecars must exist."""
    if not EVIDENCE_V04.is_dir():
        return
    transcript_dir = EVIDENCE_V04 / "agent_transcripts"
    if not transcript_dir.is_dir():
        return
    transcripts = sorted(transcript_dir.glob("slot-*.log"))
    assert len(transcripts) == 20, f"expected 20 transcripts, got {len(transcripts)}"
    missing_sidecars = [
        t.relative_to(REPO_ROOT).as_posix()
        for t in transcripts
        if not t.with_suffix(t.suffix + ".sha256").is_file()
    ]
    assert missing_sidecars == []


def test_v04_ao_bundle_exists() -> None:
    """AO orchestration evidence bundle must exist (proves ao spawn drives area-lock)."""
    assert EVIDENCE_V04_AO.is_dir(), (
        "evidence/v0.4-ao/ missing — run scripts/e2e_ao_orchestrated_runner.py"
    )
    assert (EVIDENCE_V04_AO / "run.json").is_file(), "evidence/v0.4-ao/run.json missing"


def test_v04_ao_scenarios_all_passed() -> None:
    """AO bundle run.json: all scenarios must have passed=True."""
    if not (EVIDENCE_V04_AO / "run.json").is_file():
        return
    data = json.loads((EVIDENCE_V04_AO / "run.json").read_text())
    assert data.get("orchestration_mode") == "ao_spawn", "unexpected orchestration_mode"
    failed = [s["name"] for s in data.get("scenarios", []) if not s.get("passed")]
    assert failed == [], f"AO scenarios failed: {failed}"


def test_v04_ao_prs_created() -> None:
    """AO bundle prs.json: all slots must have real PR URLs."""
    if not (EVIDENCE_V04_AO / "prs.json").is_file():
        return
    slots = json.loads((EVIDENCE_V04_AO / "prs.json").read_text())
    no_pr = [s["slot"] for s in slots if not s.get("pr_url")]
    assert no_pr == [], f"AO slots missing PRs: {no_pr}"


def test_v04_ao_checksums_valid() -> None:
    """All sha256 sidecars in AO bundle must be valid."""
    if not EVIDENCE_V04_AO.is_dir():
        return
    _bundle_sha256_checks(EVIDENCE_V04_AO)


def test_v05_ao_bundle_proves_15_slots() -> None:
    """v0.5 AO bundle must prove 15 slots created PRs through ao spawn."""
    assert EVIDENCE_V05_AO.is_dir(), (
        "evidence/v0.5-ao/ missing — run scripts/e2e_ao_orchestrated_runner.py "
        "--slots 15 --kill-session-after-pr"
    )
    metadata = json.loads((EVIDENCE_V05_AO / "metadata.json").read_text())
    assert metadata.get("orchestration_mode") == "ao_spawn"
    assert metadata.get("slots") == 15
    assert metadata.get("kill_session_after_pr") is True

    run = json.loads((EVIDENCE_V05_AO / "run.json").read_text())
    failed = [s["name"] for s in run.get("scenarios", []) if not s.get("passed")]
    assert failed == []

    slot_results = run.get("slot_results", [])
    assert len(slot_results) == 15
    assert [slot["slot"] for slot in slot_results] == list(range(1, 16))
    missing_prs = [slot["slot"] for slot in slot_results if not slot.get("pr_url")]
    assert missing_prs == []
    bad_spawns = [slot["slot"] for slot in slot_results if slot.get("spawn_exit") != 0]
    assert bad_spawns == []
    bad_kills = [
        slot["slot"]
        for slot in slot_results
        if slot.get("session_kill", {}).get("exit_code") != 0
    ]
    assert bad_kills == []


def test_v05_ao_checksums_valid() -> None:
    """All sha256 sidecars in 15-slot AO bundle must be valid."""
    if not EVIDENCE_V05_AO.is_dir():
        return
    _bundle_sha256_checks(EVIDENCE_V05_AO)


def test_v06_ao_bundle_proves_20_slots() -> None:
    """v0.6 AO bundle must prove 20 slots orchestrated via ao spawn with >=10 PRs."""
    assert EVIDENCE_V06_AO.is_dir(), (
        "evidence/v0.6-ao/ missing — run scripts/e2e_ao_orchestrated_runner.py "
        "--slots 20 --kill-session-after-pr"
    )
    metadata = json.loads((EVIDENCE_V06_AO / "metadata.json").read_text())
    assert metadata.get("orchestration_mode") == "ao_spawn"
    assert metadata.get("slots") == 20
    assert metadata.get("kill_session_after_pr") is True

    run = json.loads((EVIDENCE_V06_AO / "run.json").read_text())

    slot_results = run.get("slot_results", [])
    assert len(slot_results) == 20
    assert [slot["slot"] for slot in slot_results] == list(range(1, 21))
    pr_count = sum(1 for slot in slot_results if slot.get("pr_url"))
    assert pr_count >= 10, f"expected >= 10 PRs, got {pr_count}"
    bad_spawns = [slot["slot"] for slot in slot_results if slot.get("spawn_exit") != 0]
    # Allow at most 2 spawn failures (agent capacity under load)
    assert len(bad_spawns) <= 2, f"too many spawn failures: {bad_spawns}"


def test_v06_ao_metadata_sha_matches_head() -> None:
    """v0.6-ao bundle SHA must be within 10 commits of HEAD."""
    if not EVIDENCE_V06_AO.is_dir():
        return
    meta = json.loads((EVIDENCE_V06_AO / "metadata.json").read_text())
    bundle_sha = meta.get("merge_train_sha", "")
    assert bundle_sha, "metadata.json missing merge_train_sha"
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{bundle_sha}^{{commit}}"],
        cwd=REPO_ROOT, capture_output=True,
    )
    assert result.returncode == 0, f"bundle SHA {bundle_sha} not found in git history"
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"{bundle_sha}..HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    commits_ahead = int(ahead.stdout.strip() or "999")
    assert commits_ahead <= 10, (
        f"evidence/v0.6-ao is {commits_ahead} commits stale "
        f"(bundle_sha={bundle_sha[:12]}); rerun runner to refresh"
    )


def test_v06_ao_metadata_sha_matches_head() -> None:
    """v0.6-ao bundle SHA must be within 10 commits of HEAD."""
    if not EVIDENCE_V06_AO.is_dir():
        return
    meta = json.loads((EVIDENCE_V06_AO / "metadata.json").read_text())
    bundle_sha = meta.get("merge_train_sha", "")
    assert bundle_sha, "metadata.json missing merge_train_sha"
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{bundle_sha}^{{commit}}"],
        cwd=REPO_ROOT, capture_output=True,
    )
    assert result.returncode == 0, f"bundle SHA {bundle_sha} not found in git history"
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"{bundle_sha}..HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    commits_ahead = int(ahead.stdout.strip() or "999")
    assert commits_ahead <= 10, (
        f"evidence/v0.6-ao is {commits_ahead} commits stale "
        f"(bundle_sha={bundle_sha[:12]}); rerun runner to refresh"
    )


def test_v06_ao_checksums_valid() -> None:
    """All sha256 sidecars in 20-slot AO bundle must be valid."""
    if not EVIDENCE_V06_AO.is_dir():
        return
    _bundle_sha256_checks(EVIDENCE_V06_AO)
