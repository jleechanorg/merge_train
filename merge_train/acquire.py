"""``acquire --files`` — declarative collision check (predict-conflicts era).

Given a list of files and a declared in-flight PR set, decide whether
the request can be **allowed** (no conflict) or must be **denied**
(at least one conflict). The whole transaction is atomic — a single
conflict on any file denies the entire request.

This is a **reframed** version of the original ``acquire --files``,
which was scoped against the now-deleted ``domain_lock`` API. The new
implementation is purely in-memory, reuses ``predict_conflicts`` for
conflict detection, and uses ``flock`` for advisory concurrency
safety. There is no persistent lock log.

Design spec: ``docs/acquire_files_spec.md`` (bead ``orch-9le6``).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from merge_train.predict import (
    DomainConflict,
    PRSpec,
    Registry,
    TextualConflict,
    load_plan,
    predict_conflicts,
)
from merge_train.symbols import (
    UnsupportedLanguageError,
    touched_symbols_for_staged_file,
)


# --------------------------------------------------------------------------- #
# Exit codes — match the project-wide contract (0 ok, 1 held, 2 config)
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_DENY = 1
EXIT_CONFIG = 2


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #


def resolve_files_to_symbols(
    files: Iterable[str],
    *,
    cwd: Optional[Path] = None,
) -> tuple[dict[str, set[str]], list[str]]:
    """Resolve each file to its touched-symbol set, with file-level fallback.

    Returns ``(resolved, fallback_files)`` where:

    * ``resolved`` maps path -> set of symbol names. For mapped files
      that resolve successfully, the symbol set comes from
      :func:`merge_train.symbols.touched_symbols_for_staged_file`
      (the file: prefix added by the multi-language layer is stripped
      so consumers see bare symbol names).
    * ``fallback_files`` is the list of paths that could not be
      symbol-resolved (unsupported type, parse error, missing). These
      are recorded in the output as file-level fallback locks using the
      sentinel ``{"file:<path>"}``.

    An empty source (no changes detected) returns an empty symbol set
    but does NOT count as a fallback — the file is mapped but
    currently has no touched symbols, so it requires no lock unit.
    """
    resolved: dict[str, set[str]] = {}
    fallback: list[str] = []
    for path in files:
        try:
            syms = touched_symbols_for_staged_file(path, cwd=cwd)
        except (UnsupportedLanguageError, Exception):
            # Fail-closed: treat as file-level fallback. The file: sentinel
            # is what the predict-conflicts code sees as a whole-file lock
            # unit. Symbol-set names match the rest of the codebase.
            resolved[path] = {f"file:{path}"}
            fallback.append(path)
            continue
        # Strip the file: prefix the multi-language extractor adds so
        # the resolver output uses bare symbol names. This makes
        # domain-conflict intersection work correctly.
        bare = {s.split(":", 1)[-1] if s.startswith("file:") else s for s in syms}
        resolved[path] = bare
    return resolved, fallback


# --------------------------------------------------------------------------- #
# Decision result + main decision function
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AcquireResult:
    """The result of an ``acquire --files`` check."""

    decision: str  # "allow" or "deny"
    files: tuple[str, ...]
    resolved: dict[str, list[str]]  # path -> sorted symbol list
    fallback_files: tuple[str, ...]
    conflicts: tuple[dict, ...]  # list of {domain, symbols, conflicting_pr}
    in_flight_prs: tuple[int, ...]
    candidate_branch: str
    candidate_agent: str
    flock_path: Optional[str] = None

    def to_json_dict(self) -> dict:
        return {
            "decision": self.decision,
            "files": list(self.files),
            "resolved": {k: sorted(v) for k, v in self.resolved.items()},
            "fallback_files": list(self.fallback_files),
            "conflicts": [dict(c) for c in self.conflicts],
            "in_flight_prs": list(self.in_flight_prs),
            "candidate": {
                "branch": self.candidate_branch,
                "agent": self.candidate_agent,
            },
            "flock_path": self.flock_path,
        }


def _conflict_to_dict(
    conflict_dc: Optional[DomainConflict],
    conflict_tc: Optional[TextualConflict],
    other_pr: int,
) -> dict:
    """Build a single conflict record from a domain/textual pair."""
    if conflict_dc is not None:
        return {
            "domain": conflict_dc.domain,
            "symbols": list(conflict_dc.symbols),
            "conflicting_pr": other_pr,
        }
    if conflict_tc is not None:
        return {
            "domain": None,
            "symbols": [],
            "conflicting_pr": other_pr,
            "file": conflict_tc.file,
        }
    return {"domain": None, "symbols": [], "conflicting_pr": other_pr}


def decide(
    *,
    files: list[str],
    in_flight: list[PRSpec],
    registry: Registry,
    branch: str,
    agent: str,
    cwd: Optional[Path] = None,
) -> AcquireResult:
    """Run the atomic decision: allow or deny.

    Builds a single candidate ``PRSpec`` from the resolved files and
    runs ``predict_conflicts([candidate, *in_flight])``. Any blocking
    pairwise conflict between the candidate and an in-flight PR
    results in ``decision="deny"``.

    *cwd* is the working directory passed to symbol resolution; pass the
    git repo root to enable real symbol extraction for staged files.
    """
    # 1. Resolve symbols (one batch call — atomic by construction).
    resolved, fallback = resolve_files_to_symbols(files, cwd=cwd)

    # 2. Build candidate PRSpec. Use a synthetic PR number far above any
    # real PR number to avoid collisions with in-flight PRs.
    candidate = PRSpec(
        pr=_SYNTHETIC_CANDIDATE_PR,
        branch=branch,
        files=tuple(files),
        symbols_by_file={path: frozenset(syms) for path, syms in resolved.items()},
    )

    # 3. Predict conflicts with the in-flight set.
    specs = [candidate] + list(in_flight)
    plan = predict_conflicts(specs, registry, include_textual=False)

    # 4. Walk the plan, collecting conflicts between the candidate and
    # each in-flight PR.
    conflicts: list[dict] = []
    for pc in plan.pairwise_conflicts:
        if not pc.is_conflict:
            continue
        if pc.pr_a != _SYNTHETIC_CANDIDATE_PR and pc.pr_b != _SYNTHETIC_CANDIDATE_PR:
            continue
        other_pr = pc.pr_b if pc.pr_a == _SYNTHETIC_CANDIDATE_PR else pc.pr_a
        # Surface up to one domain conflict and one textual per pair.
        first_dc = next(
            (dc for dc in pc.domain_conflicts if not dc.advisory), None
        )
        first_tc = pc.textual_conflicts[0] if pc.textual_conflicts else None
        if first_dc is not None:
            conflicts.append(_conflict_to_dict(first_dc, None, other_pr))
        elif first_tc is not None:
            conflicts.append(_conflict_to_dict(None, first_tc, other_pr))

    decision = "deny" if conflicts else "allow"

    return AcquireResult(
        decision=decision,
        files=tuple(files),
        resolved={k: sorted(v) for k, v in resolved.items()},
        fallback_files=tuple(fallback),
        conflicts=tuple(conflicts),
        in_flight_prs=tuple(s.pr for s in in_flight),
        candidate_branch=branch,
        candidate_agent=agent,
    )


# Synthetic PR number used to identify the candidate in pairwise-conflict
# reports. Must be larger than any plausible real PR number; use a value
# safely above the 32-bit int range so it never collides with GitHub's
# real PR id space.
_SYNTHETIC_CANDIDATE_PR = 2**31


# --------------------------------------------------------------------------- #
# flock context manager
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def acquire_flock(
    lock_path: Path,
    *,
    timeout_seconds: float = 30.0,
    enabled: bool = True,
):
    """Take an exclusive ``flock`` on *lock_path* for the duration of the
    ``with`` block. Yields the FD on success. Raises ``LockAcquireError``
    on timeout.

    The lock file is created on demand. The lock is **advisory** —
    only callers that take it serialize.
    """
    if not enabled:
        yield None
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout_seconds
        # Try non-blocking first; if held, fall back to blocking with
        # the remaining budget.
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise LockAcquireError(
                        f"could not acquire flock on {lock_path} within {timeout_seconds}s"
                    )
                time.sleep(0.05)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


class LockAcquireError(RuntimeError):
    """Raised when ``acquire_flock`` cannot obtain the lock in time."""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


DEFAULT_LOCK_PATH = Path.home() / ".merge_train" / "acquire.lock"


def _print_human(result: AcquireResult) -> None:
    print(
        f"acquire: branch={result.candidate_branch} agent={result.candidate_agent}"
    )
    print(f"  Resolved {len(result.files)}/{len(result.files)} files.")
    for path in result.files:
        syms = result.resolved.get(path, [])
        is_fallback = path in result.fallback_files
        if is_fallback:
            print(f"  {path}\t-> (file-level fallback)")
        elif syms:
            print(f"  {path}\t-> {','.join(syms)}")
        else:
            print(f"  {path}\t-> (no touched symbols)")
    if result.in_flight_prs:
        print(
            f"  In-flight PRs: {','.join(str(p) for p in result.in_flight_prs)}"
        )
    if result.decision == "allow":
        print("  Decision: allow (exit 0)")
    else:
        for c in result.conflicts:
            if c.get("domain"):
                print(
                    f"  Conflict with PR#{c['conflicting_pr']} on "
                    f"domain={c['domain']} symbols={','.join(c['symbols'])}"
                )
            else:
                print(
                    f"  Conflict with PR#{c['conflicting_pr']} on "
                    f"file={c.get('file', '<unknown>')}"
                )
        print("  Decision: deny (exit 1)")


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the ``acquire`` CLI subcommand."""
    parser = argparse.ArgumentParser(
        prog="acquire",
        description=(
            "Atomic file-list acquisition: check whether a list of files "
            "can be acquired without conflicting with the in-flight PR set."
        ),
    )
    parser.add_argument(
        "files", nargs="*", help="Files to acquire (one or more)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--plan", metavar="FILE",
        help="YAML/JSON file declaring the in-flight PR set.",
    )
    source.add_argument(
        "--from-prs", metavar="N,N,...",
        help="Comma-separated PR numbers to fetch from GitHub.",
    )
    parser.add_argument(
        "--registry", metavar="FILE", default=None,
        help="Optional YAML domain registry.",
    )
    parser.add_argument(
        "--repo", metavar="OWNER/REPO", default=None,
        help="GitHub repo for --from-prs and symbol enrichment.",
    )
    parser.add_argument(
        "--branch", default="acquire",
        help="Branch name requesting acquisition (default: acquire).",
    )
    parser.add_argument(
        "--agent", default="acquire",
        help="Agent name requesting acquisition (default: acquire).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit JSON output instead of human-readable text.",
    )
    parser.add_argument(
        "--lock-path", metavar="FILE", default=str(DEFAULT_LOCK_PATH),
        help=f"Path for the advisory flock (default: {DEFAULT_LOCK_PATH}).",
    )
    parser.add_argument(
        "--lock-timeout", metavar="SECONDS", type=float, default=30.0,
        help="How long to wait for the flock (default: 30).",
    )
    parser.add_argument(
        "--no-flock", action="store_true",
        help="Skip the advisory flock (for tests/CI).",
    )
    parser.add_argument(
        "--git-cwd", metavar="DIR", default=None,
        help="Working directory for git commands (symbol resolution).",
    )

    args = parser.parse_args(argv)

    # CLI-level guards → exit 2 (config error).
    if not args.files:
        print("error: at least one FILE is required", file=sys.stderr)
        return EXIT_CONFIG

    # Load registry (empty if omitted — file-level only).
    if args.registry:
        try:
            registry = Registry.from_yaml(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CONFIG
    else:
        registry = Registry.empty()

    # Load in-flight PRs.
    try:
        if args.from_prs:
            in_flight = _load_from_prs(args.from_prs, args.repo)
        else:
            in_flight = load_plan(args.plan)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (yaml.YAMLError, ValueError, KeyError, TypeError) as exc:
        print(f"error: malformed plan: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except OSError as exc:
        print(f"error: cannot read plan: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    # Run the decision under flock.
    lock_path = Path(args.lock_path)
    git_cwd = Path(args.git_cwd) if args.git_cwd else None
    try:
        with acquire_flock(
            lock_path,
            timeout_seconds=args.lock_timeout,
            enabled=not args.no_flock,
        ):
            result = decide(
                files=list(args.files),
                in_flight=in_flight,
                registry=registry,
                branch=args.branch,
                agent=args.agent,
                cwd=git_cwd,
            )
    except LockAcquireError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DENY

    # Set flock_path on the result for output (only if we actually took it).
    if not args.no_flock:
        result = dataclasses.replace(result, flock_path=str(lock_path))

    if args.json_output:
        print(json.dumps(result.to_json_dict(), indent=2))
    else:
        _print_human(result)

    return EXIT_OK if result.decision == "allow" else EXIT_DENY


def _load_from_prs(
    from_prs: str,
    repo: Optional[str],
) -> list[PRSpec]:
    """Fetch in-flight PR specs from GitHub via ``gh``."""
    from merge_train.predict import _load_specs_from_github
    try:
        pr_numbers = [int(x.strip()) for x in from_prs.split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError(f"--from-prs must be comma-separated integers: {exc}")
    specs, failed = _load_specs_from_github(pr_numbers, repo)
    if failed:
        raise ValueError(f"could not load requested PRs: {','.join(str(p) for p in failed)}")
    if not specs:
        raise ValueError("no PRs could be loaded from GitHub")
    return specs


# ``time`` is imported lazily inside acquire_flock to keep import
# surface tidy. Declare it module-level for testability.
if __name__ == "__main__":
    sys.exit(main())
