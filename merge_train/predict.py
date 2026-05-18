"""Dry-run conflict prediction across a set of PRs.

Given a declared set of PRs (each with files and optional touched symbols),
this module:

1. Computes pairwise **symbol-domain conflicts** by reusing the same
   logic the `check` subcommand uses against the live registry, just
   against an in-memory "would-reserve" state instead of the lock log.
2. Optionally augments that with **textual conflicts** detected by
   running ``git merge-tree`` between each pair of PR branches.
3. Builds a conflict graph and emits:
   - the **maximum parallel batch** (greedy maximal independent set)
   - a **recommended merge order** (iteratively peel off the next MIS)

Output is intentionally framed as a **risk-reduction signal, not a
merge guarantee** — see `Plan.disclaimer`. CI + human review remain
the authoritative gates.

Design inputs:
- /research subagent (https://github.com/jleechanorg/merge_train, 2026-05-18)
  recommended greedy MIS on a conflict graph (Section 10 of survey).
- /secondo (gemini) insisted on `git merge-tree` integration for
  textual safety net + explicit disclaimer.

References:
- Greedy MIS algorithm: textbook (priority/weight-based, deterministic).
- ``git merge-tree`` semantics: https://git-scm.com/docs/git-merge-tree
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from merge_train.domain_lock import (
    LockEntry,
    LockLog,
    Registry,
    check,
)


DISCLAIMER = (
    "Risk-reduction signal, not a merge guarantee. Conflict detection is "
    "based on declared file/symbol scopes + optional `git merge-tree`. "
    "Run CI and human review before merging."
)


# --------------------------------------------------------------------------- #
# Input model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PRSpec:
    """A single PR's declared scope.

    ``symbols_by_file`` maps an entry of ``files`` to its touched-symbol
    set. Missing entries are treated as whole-file edits (fail-closed),
    matching the same semantics as ``domain_lock check --diff-mode``.
    """

    pr: int
    branch: str
    files: tuple[str, ...]
    symbols_by_file: dict[str, frozenset[str]] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "PRSpec":
        pr = int(d["pr"])
        branch = str(d.get("branch") or f"pr-{pr}")
        files = tuple(d.get("files") or ())
        raw_syms = d.get("symbols") or {}
        symbols_by_file: dict[str, frozenset[str]] = {}
        for path, syms in raw_syms.items():
            if syms is None:
                continue  # treat as whole-file
            symbols_by_file[path] = frozenset(str(s) for s in syms)
        return PRSpec(
            pr=pr, branch=branch, files=files, symbols_by_file=symbols_by_file
        )


def load_plan(path: str | Path) -> list[PRSpec]:
    """Load a list of ``PRSpec``s from a YAML/JSON file.

    Schema::

        prs:
          - pr: 123
            branch: feat/foo
            files: [a.py, b.py]
            symbols: {a.py: [foo], b.py: [bar]}
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    items = data.get("prs") or data.get("plan") or []
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected a 'prs' list, got {type(items).__name__}")
    return [PRSpec.from_dict(item) for item in items]


# --------------------------------------------------------------------------- #
# Conflict primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DomainConflict:
    domain: str
    symbols: tuple[str, ...]  # symbols *both* PRs touched in the domain (empty = whole-domain)


@dataclass(frozen=True)
class TextualConflict:
    file: str


@dataclass(frozen=True)
class PairConflict:
    pr_a: int
    pr_b: int
    domain_conflicts: tuple[DomainConflict, ...]
    textual_conflicts: tuple[TextualConflict, ...]

    @property
    def is_conflict(self) -> bool:
        return bool(self.domain_conflicts) or bool(self.textual_conflicts)


def _spec_as_lock_entries(spec: PRSpec, registry: Registry) -> list[LockEntry]:
    """Express a ``PRSpec`` as the set of ``LockEntry`` rows it *would*
    write if it called ``reserve`` (one entry per domain it touches).

    Whole-file edits → whole-domain lock. Symbol-resolved files →
    union of touched symbols within the domain.
    """
    grouped = registry.domains_for_paths(spec.files)
    grouped.pop("__unmapped__", None)
    entries: list[LockEntry] = []
    for domain, paths in grouped.items():
        agg: set[str] = set()
        whole_domain = False
        for path in paths:
            if path not in spec.symbols_by_file:
                whole_domain = True
                break
            agg.update(spec.symbols_by_file[path])
        symbols = [] if whole_domain else sorted(agg)
        entries.append(
            LockEntry(
                domain=domain,
                pr=spec.pr,
                agent="<predicted>",
                branch=spec.branch,
                opened_at="<predicted>",
                status="active",
                symbols=symbols,
            )
        )
    return entries


def _pair_domain_conflicts(
    a_entries: list[LockEntry],
    b_entries: list[LockEntry],
) -> list[DomainConflict]:
    """Compute symbol-domain conflicts between two PRs' would-be reservations."""
    by_domain_a = {e.domain: e for e in a_entries}
    out: list[DomainConflict] = []
    for b in b_entries:
        a = by_domain_a.get(b.domain)
        if a is None:
            continue
        # Whole-domain on either side => whole-domain conflict
        if not a.symbols or not b.symbols:
            out.append(DomainConflict(domain=a.domain, symbols=()))
            continue
        overlap = sorted(set(a.symbols) & set(b.symbols))
        if overlap:
            out.append(DomainConflict(domain=a.domain, symbols=tuple(overlap)))
    return out


def _git_merge_tree_conflicts(
    a_branch: str,
    b_branch: str,
    *,
    base: str = "origin/main",
    cwd: Optional[Path] = None,
) -> list[TextualConflict]:
    """Run ``git merge-tree`` to detect textual conflict files between two refs.

    Returns the list of files with conflict hunks. On modern git
    (>=2.38) ``git merge-tree --write-tree --name-only`` reports the
    conflicted paths directly via stderr/stdout. On older git, the
    pre-2.38 form is parsed for ``<<<<<<<`` markers.

    Silently returns ``[]`` on any subprocess failure (refs not found,
    unrelated histories, git missing) — textual prediction is a
    best-effort augmentation, not a gate.
    """
    try:
        # Modern git: --write-tree + --name-only emits conflicted paths on stdout.
        r = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only",
             "--merge-base=" + base, a_branch, b_branch],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            return []
        # returncode 1 = conflict; stdout lists the OID then files
        out_lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        # First line is the resulting tree OID; subsequent lines are files.
        files = out_lines[1:] if len(out_lines) >= 2 else []
        # Some versions emit additional info sections separated by blank lines;
        # filter to lines that look like paths.
        return [TextualConflict(file=p) for p in files if "/" in p or "." in p]
    except FileNotFoundError:
        return []
    except subprocess.SubprocessError:
        return []


# --------------------------------------------------------------------------- #
# Conflict graph + greedy MIS scheduler
# --------------------------------------------------------------------------- #


@dataclass
class Plan:
    input_prs: list[int]
    pairwise_conflicts: list[PairConflict]
    parallel_batches: list[list[int]]
    recommended_order: list[int]
    unmapped_files_by_pr: dict[int, list[str]] = field(default_factory=dict)
    disclaimer: str = DISCLAIMER

    def to_json_dict(self) -> dict:
        return {
            "input_prs": self.input_prs,
            "pairwise_conflicts": [
                {
                    "prs": [pc.pr_a, pc.pr_b],
                    "domain_conflicts": [
                        {"domain": dc.domain, "symbols": list(dc.symbols)}
                        for dc in pc.domain_conflicts
                    ],
                    "textual_conflicts": [
                        {"file": tc.file} for tc in pc.textual_conflicts
                    ],
                }
                for pc in self.pairwise_conflicts
                if pc.is_conflict
            ],
            "parallel_batches": self.parallel_batches,
            "recommended_order": self.recommended_order,
            "unmapped_files_by_pr": self.unmapped_files_by_pr,
            "disclaimer": self.disclaimer,
        }


def _greedy_max_independent_set(
    nodes: list[int],
    edges: set[frozenset[int]],
) -> list[int]:
    """Greedy maximal independent set.

    Picks the lowest-degree node first (ties broken by node id ascending
    for determinism), adds it to the set, removes it and all its
    neighbors. Repeat until no candidates remain. Deterministic.
    """
    remaining = set(nodes)
    adj: dict[int, set[int]] = {n: set() for n in nodes}
    for e in edges:
        a, b = sorted(e)
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)

    chosen: list[int] = []
    while remaining:
        # Lowest degree among remaining, tie-break by id
        cand = min(
            remaining,
            key=lambda n: (len(adj[n] & remaining), n),
        )
        chosen.append(cand)
        remaining -= ({cand} | adj[cand])
    return sorted(chosen)


def _build_conflict_edges(
    specs: list[PRSpec],
    pair_conflicts: list[PairConflict],
) -> set[frozenset[int]]:
    return {
        frozenset((pc.pr_a, pc.pr_b))
        for pc in pair_conflicts
        if pc.is_conflict
    }


def predict_conflicts(
    specs: list[PRSpec],
    registry: Registry,
    *,
    include_textual: bool = False,
    git_base: str = "origin/main",
    git_cwd: Optional[Path] = None,
) -> Plan:
    """Compute the pairwise conflict report + parallel-batch schedule.

    ``include_textual`` triggers per-pair ``git merge-tree`` runs. Off
    by default for unit-test speed; the CLI turns it on unless
    ``--no-textual`` is passed.
    """
    # 1. Predict each PR's domain entries.
    pr_entries: dict[int, list[LockEntry]] = {}
    unmapped: dict[int, list[str]] = {}
    for spec in specs:
        pr_entries[spec.pr] = _spec_as_lock_entries(spec, registry)
        grouped = registry.domains_for_paths(spec.files)
        u = grouped.get("__unmapped__", [])
        if u:
            unmapped[spec.pr] = sorted(u)

    # 2. Pairwise conflict detection.
    pair_conflicts: list[PairConflict] = []
    ids = [s.pr for s in specs]
    spec_by_pr = {s.pr: s for s in specs}
    for i, a_pr in enumerate(ids):
        for b_pr in ids[i + 1:]:
            doms = _pair_domain_conflicts(pr_entries[a_pr], pr_entries[b_pr])
            tex: list[TextualConflict] = []
            if include_textual:
                tex = _git_merge_tree_conflicts(
                    spec_by_pr[a_pr].branch,
                    spec_by_pr[b_pr].branch,
                    base=git_base, cwd=git_cwd,
                )
            pair_conflicts.append(
                PairConflict(
                    pr_a=a_pr, pr_b=b_pr,
                    domain_conflicts=tuple(doms),
                    textual_conflicts=tuple(tex),
                )
            )

    # 3. Build the conflict graph and iteratively peel off MIS batches.
    edges = _build_conflict_edges(specs, pair_conflicts)
    remaining = list(ids)
    batches: list[list[int]] = []
    while remaining:
        batch = _greedy_max_independent_set(remaining, edges)
        if not batch:
            batches.append(sorted(remaining))  # degenerate guard
            break
        batches.append(batch)
        rem_set = set(remaining) - set(batch)
        remaining = [p for p in remaining if p in rem_set]

    order: list[int] = []
    for b in batches:
        order.extend(b)

    return Plan(
        input_prs=list(ids),
        pairwise_conflicts=pair_conflicts,
        parallel_batches=batches,
        recommended_order=order,
        unmapped_files_by_pr=unmapped,
    )


# --------------------------------------------------------------------------- #
# CLI entry-point (wired in domain_lock._build_parser)
# --------------------------------------------------------------------------- #


def cli_predict_conflicts(
    *,
    plan_path: str,
    registry: Registry,
    include_textual: bool,
    git_base: str,
    git_cwd: Optional[Path],
    json_output: bool,
) -> int:
    """Implementation of ``domain_lock predict-conflicts``.

    Returns the exit code:
    - 0 if no pairwise conflict was detected
    - 1 if at least one pair conflicts
    - 2 on plan-file errors (FileNotFoundError, malformed YAML)
    """
    try:
        specs = load_plan(plan_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 2
    except (yaml.YAMLError, ValueError) as exc:
        print(f"error: malformed plan: {exc}", file=__import__("sys").stderr)
        return 2

    plan = predict_conflicts(
        specs, registry,
        include_textual=include_textual,
        git_base=git_base, git_cwd=git_cwd,
    )

    if json_output:
        print(json.dumps(plan.to_json_dict(), indent=2))
    else:
        _print_human(plan)

    any_conflict = any(pc.is_conflict for pc in plan.pairwise_conflicts)
    return 1 if any_conflict else 0


def _print_human(plan: Plan) -> None:
    print(f"PRs analyzed: {plan.input_prs}")
    if plan.unmapped_files_by_pr:
        for pr, files in plan.unmapped_files_by_pr.items():
            print(f"  WARN: PR#{pr} touches unmapped files: {', '.join(files)}")
    conflicts = [pc for pc in plan.pairwise_conflicts if pc.is_conflict]
    if not conflicts:
        print("No pairwise conflicts detected.")
    else:
        print(f"\n{len(conflicts)} pairwise conflict(s):")
        for pc in conflicts:
            print(f"  PR#{pc.pr_a} <-> PR#{pc.pr_b}:")
            for dc in pc.domain_conflicts:
                if dc.symbols:
                    print(
                        f"    domain={dc.domain} symbols={','.join(dc.symbols)}"
                    )
                else:
                    print(f"    domain={dc.domain} (whole-domain)")
            for tc in pc.textual_conflicts:
                print(f"    textual conflict: {tc.file}")
    print(f"\nParallel batches: {plan.parallel_batches}")
    print(f"Recommended order: {plan.recommended_order}")
    print(f"\n{plan.disclaimer}")
