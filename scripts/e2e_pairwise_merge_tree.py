#!/usr/bin/env python3
"""Pairwise merge-tree simulation for E2E evidence.

For every pair of branches, run `git merge-tree` (or equivalent) against
a common base and record whether the merge succeeds cleanly or conflicts.
Output is a JSON dict mapping ``"branchA branchB"`` to result objects.

Usage:
    python scripts/e2e_pairwise_merge_tree.py \
        --base origin/main \
        --branches-file evidence/branches.txt \
        --output evidence/pairwise_merge_tree.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from itertools import combinations
from pathlib import Path


def _merge_tree_result(base: str, branch_a: str, branch_b: str, git_cwd: str | None = None) -> dict:
    cwd = git_cwd or None
    try:
        subprocess.run(
            ["git", "merge-tree", base, branch_a, branch_b],
            capture_output=True, text=True, check=False, cwd=cwd,
        )
    except FileNotFoundError:
        pass
    try:
        result = subprocess.run(
            ["git", "merge-tree", base, branch_a, branch_b],
            capture_output=True, text=True, check=False, cwd=cwd,
        )
        has_conflict_markers = "<<<<<<< " in result.stdout or "<<<<<<<" in result.stdout
        return {
            "branch_a": branch_a,
            "branch_b": branch_b,
            "base": base,
            "exit_code": result.returncode,
            "conflict": has_conflict_markers,
            "clean": not has_conflict_markers,
        }
    except Exception as exc:
        return {
            "branch_a": branch_a,
            "branch_b": branch_b,
            "base": base,
            "error": str(exc),
            "conflict": None,
            "clean": None,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pairwise merge-tree simulation")
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
    if len(branches) < 2:
        print(f"error: need at least 2 branches, got {len(branches)}", file=sys.stderr)
        return 2

    results: dict[str, dict] = {}
    pairs = list(combinations(branches, 2))
    print(f"Simulating {len(pairs)} pairwise merges (base={args.base})...")
    for a, b in pairs:
        key = f"{a} {b}"
        results[key] = _merge_tree_result(args.base, a, b, args.git_cwd)

    conflicts = sum(1 for v in results.values() if v.get("conflict"))
    clean = sum(1 for v in results.values() if v.get("clean"))
    print(f"Done: {clean} clean, {conflicts} conflict, {len(pairs)} total")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(f"Written: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
