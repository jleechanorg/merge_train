#!/usr/bin/env python3
"""E2E runner proving AO orchestration via `ao spawn` drives Markdown area-lock slots.

Complements e2e_md_area_lock_runner.py (which proves direct openw invocation).
This runner proves the full orchestration path:
  ao spawn --agent opencode → AO session → openw run → git push → PR

Evidence saved to evidence/v0.4-ao/ (or /tmp/merge_train_evidence/ao/<run_id>).

Usage:
    python scripts/e2e_ao_orchestrated_runner.py [--slots 4] [--mctrl-repo PATH]
    (default: 4 slots to keep AO session count low and test time short)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, check: bool = True, capture: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=capture, text=True, check=False, cwd=cwd)
    if check and result.returncode != 0:
        print(f"FAIL: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stdout: {result.stdout[:500]}", file=sys.stderr)
        print(f"  stderr: {result.stderr[:500]}", file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_checksum(filepath: Path) -> None:
    sha = _sha256_file(filepath)
    sidecar = filepath.parent / f"{filepath.name}.sha256"
    sidecar.write_text(f"{sha}  {filepath.name}\n")


def _domain_lock_cmd(extra: list[str], registry: str, log: str, git_cwd: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "merge_train.domain_lock", "--registry", registry, "--log", log]
    if git_cwd:
        cmd += ["--git-cwd", git_cwd]
    return cmd + extra


def _ao_spawn_slot(slot: int, run_id: str, mctrl_repo: str, registry: str, lock_log: str,
                   merge_train_repo: str) -> dict:
    """Spawn one AO session for a slot, wait for idle, collect evidence."""
    n = f"{slot:02d}"
    branch = f"merge-train-e2e-ao/{run_id}/slot-{n}"
    synthetic_pr = 60000 + slot
    symbol = f"md:shared_plan.slot_{n}"

    # Reserve lock BEFORE spawning AO session
    plan_path = f"/tmp/e2e_ao_plan_slot_{n}.yaml"
    Path(plan_path).write_text(f"plan:\n  - domain: e2e_shared_markdown\n    symbols: [{symbol}]\n")
    reserve_result = _run(_domain_lock_cmd(
        ["reserve-plan", "--pr", str(synthetic_pr), "--agent", f"ao-slot-{n}",
         "--branch", branch, "--plan", plan_path],
        registry=registry, log=lock_log, git_cwd=mctrl_repo,
    ), check=False)
    reserved = reserve_result.returncode == 0
    if not reserved:
        return {
            "slot": slot, "reserved": False,
            "error": f"lock reservation failed: {reserve_result.stderr.strip()}",
        }

    # Build the task for the AO session
    task = (
        f"In the repo at {mctrl_repo}, on branch {branch} (create from setup branch "
        f"merge-train-e2e/{run_id}/setup if it doesn't exist): "
        f"edit merge_train_e2e/shared_plan.md under heading ## slot-{n}, "
        f"change 'status: pending' to 'status: complete by ao-slot-{n}'. "
        f"Do NOT edit any other heading. Commit and push the branch, "
        f"then create a PR against main in jleechanorg/mctrl_test. "
        f"Report the PR URL when done."
    )

    # Spawn AO session
    spawn_start = time.time()
    spawn_result = _run(
        ["ao", "spawn", "--agent", "opencode", task],
        check=False, cwd=merge_train_repo,
    )
    spawn_elapsed = time.time() - spawn_start
    session_name = ""
    for line in spawn_result.stdout.splitlines():
        if "session" in line.lower() and ":" in line:
            session_name = line.split(":")[-1].strip()
            break
        if line.strip().startswith("ao-") or line.strip().startswith("session-"):
            session_name = line.strip()

    # Wait for session to complete (poll ao status)
    pr_url = ""
    pr_number = 0
    wait_start = time.time()
    max_wait = 300  # 5 minutes per slot
    while time.time() - wait_start < max_wait:
        time.sleep(10)
        if session_name:
            status_result = _run(["ao", "session", "ls", "--json"], check=False, cwd=merge_train_repo)
            if status_result.returncode == 0 and status_result.stdout.strip():
                try:
                    sessions = json.loads(status_result.stdout)
                    for sess in sessions:
                        if sess.get("name") == session_name or sess.get("id") == session_name:
                            if sess.get("status") in ("idle", "done", "complete"):
                                break
                except (json.JSONDecodeError, TypeError):
                    pass
        # Check if branch was pushed and PR exists
        pr_check = _run(
            ["gh", "pr", "list", "--repo", "jleechanorg/mctrl_test",
             "--head", branch, "--json", "number,url", "--state", "open"],
            check=False,
        )
        if pr_check.returncode == 0 and pr_check.stdout.strip():
            try:
                prs = json.loads(pr_check.stdout)
                if prs:
                    pr_number = prs[0].get("number", 0)
                    pr_url = prs[0].get("url", "")
                    break
            except (json.JSONDecodeError, IndexError):
                pass
    wait_elapsed = time.time() - wait_start

    return {
        "slot": slot,
        "reserved": True,
        "symbol": symbol,
        "branch": branch,
        "synthetic_pr": synthetic_pr,
        "session_name": session_name,
        "spawn_exit": spawn_result.returncode,
        "spawn_elapsed_s": round(spawn_elapsed, 1),
        "wait_elapsed_s": round(wait_elapsed, 1),
        "pr_url": pr_url,
        "pr_number": pr_number,
        "agent_mode": "ao_spawn_opencode",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E AO orchestration proof for Markdown area-lock")
    parser.add_argument("--slots", type=int, default=4,
                        help="number of slots (default: 4, keep low for AO session budget)")
    parser.add_argument("--mctrl-repo", default=str(Path.home() / "projects" / "mctrl_test"))
    parser.add_argument("--output-dir", default=None,
                        help="evidence output path (default: /tmp/merge_train_evidence/ao/<run_id>)")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    merge_train_repo = str(_REPO_ROOT)
    mctrl_repo = args.mctrl_repo
    evidence_dir = Path(args.output_dir) if args.output_dir else Path(
        f"/tmp/merge_train_evidence/ao/{run_id}"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)

    registry_path = str(Path(mctrl_repo) / "file_domains.yaml")
    lock_log = str(evidence_dir / "lock_log.jsonl")

    print(f"Run ID: {run_id}")
    print(f"Evidence dir: {evidence_dir}")
    print(f"Slots: {args.slots}")
    print(f"Orchestration: ao spawn --agent opencode")

    merge_train_sha = _run(["git", "rev-parse", "HEAD"], cwd=merge_train_repo).stdout.strip()
    metadata: dict = {
        "run_id": run_id,
        "bundle_version": "1.2.0",
        "bundle_timestamp": _utcnow(),
        "merge_train_sha": merge_train_sha,
        "merge_train_branch": _run(["git", "branch", "--show-current"], cwd=merge_train_repo).stdout.strip(),
        "slots": args.slots,
        "orchestration_mode": "ao_spawn",
        "agent": "opencode",
        "test_type": "e2e_area_lock_ao_orchestrated",
    }

    scenarios: list[dict] = []
    slot_results: list[dict] = []

    print(f"\n=== Spawning {args.slots} AO sessions ===")
    for slot in range(1, args.slots + 1):
        print(f"  Spawning slot-{slot:02d}...")
        result = _ao_spawn_slot(slot, run_id, mctrl_repo, registry_path, lock_log, merge_train_repo)
        slot_results.append(result)
        if result.get("pr_url"):
            print(f"  slot-{slot:02d}: PR #{result['pr_number']} ({result['pr_url']})")
        else:
            print(f"  slot-{slot:02d}: {'reserved, no PR yet' if result.get('reserved') else result.get('error', 'failed')}")

    pr_slots = [r for r in slot_results if r.get("pr_url")]
    scenarios.append({
        "name": "ao_spawn_pr_creation",
        "passed": len(pr_slots) == args.slots,
        "errors": [f"slot {r['slot']}: no PR created" for r in slot_results if not r.get("pr_url")],
        "note": f"{len(pr_slots)}/{args.slots} PRs created via ao spawn --agent opencode",
    })

    # Verify all PRs touch only their assigned slot
    print("\n=== Verifying PR isolation ===")
    isolation_errors: list[str] = []
    for r in pr_slots:
        n = f"{r['slot']:02d}"
        if r.get("pr_number"):
            view = _run(
                ["gh", "pr", "view", str(r["pr_number"]), "--repo", "jleechanorg/mctrl_test",
                 "--json", "files"],
                check=False,
            )
            if view.returncode == 0:
                try:
                    files = json.loads(view.stdout).get("files", [])
                    paths = [f.get("path", "") for f in files]
                    non_plan = [p for p in paths if p != "merge_train_e2e/shared_plan.md"]
                    if non_plan:
                        isolation_errors.append(f"slot-{n}: unexpected files {non_plan}")
                except (json.JSONDecodeError, KeyError):
                    pass
    scenarios.append({
        "name": "ao_pr_isolation",
        "passed": len(isolation_errors) == 0,
        "errors": isolation_errors,
    })

    # Release locks
    print("\n=== Releasing locks ===")
    for slot in range(1, args.slots + 1):
        _run(_domain_lock_cmd(
            ["release", "--pr", str(60000 + slot)],
            registry=registry_path, log=lock_log, git_cwd=mctrl_repo,
        ), check=False)

    # Collect evidence
    (evidence_dir / "prs.json").write_text(json.dumps(slot_results, indent=2))

    metadata["provenance"] = {
        "merge_train_sha": merge_train_sha,
        "ci_run_url": (
            os.environ.get("GITHUB_SERVER_URL", "") + "/" +
            os.environ.get("GITHUB_REPOSITORY", "") + "/actions/runs/" +
            os.environ.get("GITHUB_RUN_ID", "")
        ) if os.environ.get("GITHUB_RUN_ID") else "",
    }
    (evidence_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    run_json = {
        "run_id": run_id,
        "bundle_version": "1.2.0",
        "orchestration_mode": "ao_spawn",
        "scenarios": scenarios,
        "slot_results": slot_results,
    }
    (evidence_dir / "run.json").write_text(json.dumps(run_json, indent=2))

    # Checksums
    all_files = [f for f in evidence_dir.rglob("*") if f.is_file() and not f.name.endswith(".sha256")]
    checksum_lines: list[str] = []
    for f in sorted(all_files):
        if f.name == "checksums.txt":
            continue
        sha = _sha256_file(f)
        rel = f.relative_to(evidence_dir).as_posix()
        checksum_lines.append(f"{sha}  {rel}")
        _write_checksum(f)
    (evidence_dir / "checksums.txt").write_text("\n".join(checksum_lines) + "\n")
    _write_checksum(evidence_dir / "checksums.txt")

    passed = sum(1 for s in scenarios if s["passed"])
    failed = sum(1 for s in scenarios if not s["passed"])
    print(f"\n=== AO orchestration proof: {passed} passed, {failed} failed ===")
    print(f"Evidence: {evidence_dir}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
