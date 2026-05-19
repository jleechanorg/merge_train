#!/usr/bin/env python3
"""Sequential merge-tree simulation for E2E evidence.

Merge branches one at a time into the base, recording whether each
sequential merge is clean or conflicts. This simulates a merge-train
where PRs land in a specific order.

Usage:
    python scripts/e2e_sequential_merge_tree.py \
        --base origin/main \
        --branches-file evidence/branches.txt \
        --output evidence/sequential_merge_tree.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _sequential_merge(base: str, branches: list[str], git_cwd: str | None = None) -> list[dict]:
    cwd = git_cwd or None
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="merge_train_seq_") as tmp:
        clone_dir = Path(tmp) / "repo"
        subprocess.run(
            ["git", "clone", "--no-local", "--quiet", "." if not cwd else cwd, str(clone_dir)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "fetch", "--all"], check=True, capture_output=True, text=True, cwd=str(clone_dir),
        )
        for name in branches:
            subprocess.run(
                ["git", "fetch", "origin", name], check=False, capture_output=True, text=True, cwd=str(clone_dir),
            )
        subprocess.run(
            ["git", "checkout", base], check=True, capture_output=True, text=True, cwd=str(clone_dir),
        )
        for branch in branches:
            remote_ref = f"origin/{branch}" if not branch.startswith("origin/") else branch
            try:
                merge_result = subprocess.run(
                    ["git", "merge", remote_ref, "--no-ff", "-m", f"merge: {branch}"],
                    capture_output=True, text=True, check=False, cwd=str(clone_dir),
                )
                has_conflict = merge_result.returncode != 0
                if has_conflict:
                    subprocess.run(
                        ["git", "merge", "--abort"],
                        capture_output=True, text=True, check=False, cwd=str(clone_dir),
                    )
                results.append({
                    "branch": branch,
                    "base_at_merge": base,
                    "clean": not has_conflict,
                    "conflict": has_conflict,
                    "exit_code": merge_result.returncode,
                })
            except Exception as exc:
                results.append({
                    "branch": branch,
                    "base_at_merge": base,
                    "error": str(exc),
                    "clean": None,
                    "conflict": None,
                })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential merge-tree simulation")
    parser.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    parser.add_argument("--branches-file", required=True, help="file with one branch name per line")
    parser.add_argument("--output", required=True, help="output JSON path")
    parser.add_argument("--git-cwd", default=None, help="git working directory")
    args = parser.parse_args()

    branches_file = Path(args.branches_file)
    if not branches_file.exists():
        print(f"error: branches file not found: {branches_file}", file=sys.stderr)
        return 2

    branches = [line.strip() for line in branches_file.read_text().splitlines() if line.strip()]
    if not branches:
        print("error: no branches found in file", file=sys.stderr)
        return 2

    print(f"Simulating sequential merges (base={args.base}, {len(branches)} branches)...")
    results = _sequential_merge(args.base, branches, args.git_cwd)

    conflicts = sum(1 for r in results if r.get("conflict"))
    clean = sum(1 for r in results if r.get("clean"))
    print(f"Done: {clean} clean, {conflicts} conflict, {len(results)} total")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(f"Written: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
