#!/usr/bin/env python3
"""Pairwise merge-tree simulation for E2E evidence.

For every pair of branches, run `git merge-tree` (or equivalent) against
a common base and record whether the merge succeeds cleanly or conflicts.

By default, symbol-level enrichment is performed via ``symbols_from_pr_diff``
so each pair reports not just *whether* files overlap but *which symbols*
are touched by both PRs. Pass ``--no-enrich-symbols`` to skip this and
revert to file-level-only output.

Output is a JSON dict mapping ``"branchA branchB"`` to result objects::

    {
      "branch_a": "...",
      "branch_b": "...",
      "pr_a": 123,
      "pr_b": 456,
      "base": "origin/main",
      "exit_code": 0,
      "conflict": false,
      "clean": true,
      "overlapping_files": ["mvp_site/world_logic.py"],
      "symbol_overlaps": {
        "mvp_site/world_logic.py": ["apply_time_freeze", "validate_npc"]
      }
    }

Usage::

    python scripts/e2e_pairwise_merge_tree.py \
        --base origin/main \
        --branches-file evidence/branches.txt \
        --output evidence/pairwise_merge_tree.json \
        --repo jleechanorg/worldarchitect.ai \
        --prs 7178,7173,6922,6911
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from itertools import combinations
from pathlib import Path
from typing import Optional


def _merge_tree_result(
    base: str,
    branch_a: str,
    branch_b: str,
    git_cwd: Optional[str] = None,
    pr_a: Optional[int] = None,
    pr_b: Optional[int] = None,
    repo: Optional[str] = None,
    enrich_symbols: bool = True,
) -> dict:
    cwd = git_cwd or None

    # ---- Textual conflict check via git merge-tree ----
    # First call: warm up refs (handles the case where git merge-tree exits
    # non-zero on binary files encoded as text=True, ignore errors here).
    try:
        subprocess.run(
            ["git", "merge-tree", base, f"origin/{branch_a}", f"origin/{branch_b}"],
            capture_output=True,
            check=False,
            cwd=cwd,
        )
    except FileNotFoundError:
        pass

    try:
        result = subprocess.run(
            ["git", "merge-tree", base, f"origin/{branch_a}", f"origin/{branch_b}"],
            capture_output=True,
            check=False,
            cwd=cwd,
        )
        raw = result.stdout.decode("utf-8", errors="replace")
        has_conflict_markers = "<<<<<<< " in raw or "<<<<<<<" in raw

        # Extract conflicting files from legacy merge-tree format
        textual_conflict_files: list[str] = []
        if has_conflict_markers:
            current_path: Optional[str] = None
            seen: set[str] = set()
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.startswith(("base ", "our ", "their ", "result ")):
                    tokens = stripped.split(None, 3)
                    if len(tokens) == 4:
                        current_path = tokens[3]
                elif (
                    (line.startswith("+<<<<<<<") or line.startswith("<<<<<<<"))
                    and current_path
                    and current_path not in seen
                ):
                    seen.add(current_path)
                    textual_conflict_files.append(current_path)

    except Exception as exc:
        return {
            "branch_a": branch_a,
            "branch_b": branch_b,
            "pr_a": pr_a,
            "pr_b": pr_b,
            "base": base,
            "error": str(exc),
            "conflict": None,
            "clean": None,
            "overlapping_files": [],
            "symbol_overlaps": {},
        }

    # ---- File overlap ----
    overlapping_files: list[str] = []
    symbol_overlaps: dict[str, list[str]] = {}

    if cwd:

        def _branch_files(branch: str) -> set[str]:
            r = subprocess.run(
                ["git", "diff", "--name-only", base, f"origin/{branch}"],
                capture_output=True,
                text=True,
                check=False,
                cwd=cwd,
            )
            return set(r.stdout.strip().splitlines())

        a_files = _branch_files(branch_a)
        b_files = _branch_files(branch_b)
        overlap = a_files & b_files
        # Filter to product code only (exclude evidence/docs artefacts)
        overlapping_files = sorted(
            f
            for f in overlap
            if not f.startswith(("evidence/", "docs/evidence", ".beads/"))
        )

    # ---- Symbol-level enrichment ----
    if enrich_symbols and repo and pr_a is not None and pr_b is not None:
        try:
            from merge_train.symbol_discovery import symbols_from_pr_diff

            syms_a = symbols_from_pr_diff(pr_a, repo)
            syms_b = symbols_from_pr_diff(pr_b, repo)
            for fpath in overlapping_files:
                sa = syms_a.get(fpath)
                sb = syms_b.get(fpath)
                if sa is not None and sb is not None:
                    shared = sorted(sa & sb)
                    if shared:
                        symbol_overlaps[fpath] = shared
        except Exception as exc:
            symbol_overlaps["__enrichment_error__"] = [str(exc)]

    return {
        "branch_a": branch_a,
        "branch_b": branch_b,
        "pr_a": pr_a,
        "pr_b": pr_b,
        "base": base,
        "exit_code": result.returncode,
        "conflict": has_conflict_markers,
        "clean": not has_conflict_markers,
        "overlapping_files": overlapping_files,
        "symbol_overlaps": symbol_overlaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pairwise merge-tree simulation with symbol enrichment"
    )
    parser.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    parser.add_argument(
        "--branches-file", required=True, help="file with one branch name per line"
    )
    parser.add_argument("--output", required=True, help="output JSON path")
    parser.add_argument("--git-cwd", default=None, help="git working directory")
    parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        default=None,
        help="GitHub repo for symbol enrichment (e.g. jleechanorg/worldarchitect.ai)",
    )
    parser.add_argument(
        "--prs",
        metavar="N,M,...",
        default=None,
        help="comma-separated PR numbers in same order as branches-file (enables symbol enrichment)",
    )
    parser.add_argument(
        "--no-enrich-symbols",
        action="store_true",
        help="disable symbol-level enrichment (file-level only)",
    )
    args = parser.parse_args()

    branches_file = Path(args.branches_file)
    if not branches_file.exists():
        print(f"error: branches file not found: {branches_file}", file=sys.stderr)
        return 2

    branches = [
        line.strip() for line in branches_file.read_text().splitlines() if line.strip()
    ]
    if len(branches) < 2:
        print(f"error: need at least 2 branches, got {len(branches)}", file=sys.stderr)
        return 2

    # Parse PR numbers if provided
    pr_numbers: list[Optional[int]] = [None] * len(branches)
    if args.prs:
        try:
            parsed = [int(x.strip()) for x in args.prs.split(",") if x.strip()]
        except ValueError as exc:
            print(
                f"error: --prs must be comma-separated integers: {exc}", file=sys.stderr
            )
            return 2
        if len(parsed) != len(branches):
            print(
                f"error: --prs has {len(parsed)} entries but branches-file has {len(branches)}",
                file=sys.stderr,
            )
            return 2
        pr_numbers = parsed  # type: ignore[assignment]

    branch_to_pr = dict(zip(branches, pr_numbers))
    enrich = not args.no_enrich_symbols
    if enrich and args.repo:
        print(f"Symbol enrichment: ON (repo={args.repo})")
    else:
        print("Symbol enrichment: OFF (pass --repo and --prs to enable)")

    results: dict[str, dict] = {}
    pairs = list(combinations(branches, 2))
    print(f"Simulating {len(pairs)} pairwise merges (base={args.base})...")
    for a, b in pairs:
        key = f"{a} {b}"
        results[key] = _merge_tree_result(
            args.base,
            a,
            b,
            git_cwd=args.git_cwd,
            pr_a=branch_to_pr[a],
            pr_b=branch_to_pr[b],
            repo=args.repo,
            enrich_symbols=enrich,
        )

    conflicts = sum(1 for v in results.values() if v.get("conflict"))
    clean = sum(1 for v in results.values() if v.get("clean"))
    symbol_conflict_pairs = sum(1 for v in results.values() if v.get("symbol_overlaps"))
    print(
        f"Done: {clean} clean, {conflicts} textual-conflict, {symbol_conflict_pairs} symbol-overlapping, {len(pairs)} total"
    )

    # Human-readable summary
    for key, r in results.items():
        status = "CONFLICT" if r.get("conflict") else "CLEAN"
        sym = r.get("symbol_overlaps", {})
        sym_summary = ""
        if sym and "__enrichment_error__" not in sym:
            total_syms = sum(len(v) for v in sym.values())
            sym_summary = f" [{total_syms} shared symbols across {len(sym)} file(s)]"
        elif r.get("overlapping_files"):
            sym_summary = f" [{len(r['overlapping_files'])} overlapping file(s), symbols not enriched]"
        print(f"  {key}: {status}{sym_summary}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(f"Written: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
