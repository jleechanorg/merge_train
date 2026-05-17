"""Core registry + lock-log + CLI for merge_train.

Spawn-time file-domain lock registry. Declarative YAML maps file paths
(or glob patterns) to named domains. An append-only JSONL log records
which PR/agent holds which domain. The CLI lets a spawn hook check
whether a set of changed files would collide with an active reservation
before any agent burns tokens.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml


DEFAULT_REGISTRY = "file_domains.yaml"
DEFAULT_LOG = "pr_domain_locks.jsonl"


class DomainHeldError(Exception):
    """Raised when a reservation is requested for a domain already held."""


class UnknownPathError(Exception):
    """Raised when a file path is not mapped to any declared domain."""


@dataclass(frozen=True)
class Domain:
    name: str
    paths: tuple[str, ...]
    owners: tuple[str, ...] = ()


@dataclass
class Registry:
    """Declarative file -> domain mapping loaded from YAML."""

    domains: dict[str, Domain] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        domains: dict[str, Domain] = {}
        for name, body in (data.get("domains") or {}).items():
            paths = tuple(body.get("paths") or ())
            owners = tuple(body.get("owners") or ())
            domains[name] = Domain(name=name, paths=paths, owners=owners)
        return cls(domains=domains)

    def domain_for_path(self, path: str) -> Optional[str]:
        """Return the domain name that owns *path*, or None.

        Paths use glob patterns (fnmatch). If multiple domains match a
        single path, the first declared (insertion order) wins. Two
        domains claiming the same path is a registry-authoring error,
        not enforced here.
        """
        norm = path.lstrip("./")
        for domain in self.domains.values():
            for pattern in domain.paths:
                if fnmatch.fnmatch(norm, pattern.lstrip("./")):
                    return domain.name
        return None

    def domains_for_paths(self, paths: Iterable[str]) -> dict[str, list[str]]:
        """Group *paths* by their resolved domain. Unmapped paths
        appear under the key ``__unmapped__``."""
        grouped: dict[str, list[str]] = {}
        for path in paths:
            name = self.domain_for_path(path) or "__unmapped__"
            grouped.setdefault(name, []).append(path)
        return grouped


@dataclass(frozen=True)
class LockEntry:
    domain: str
    pr: int
    agent: str
    branch: str
    opened_at: str
    status: str  # "active" | "released"
    closed_at: Optional[str] = None
    note: Optional[str] = None
    symbols: tuple[str, ...] = ()

    def to_json(self) -> str:
        d = dataclasses.asdict(self)
        # Normalize symbols: drop the field if empty so old logs round-trip
        # unchanged and stay diff-clean.
        if not d.get("symbols"):
            d.pop("symbols", None)
        else:
            d["symbols"] = list(d["symbols"])
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "LockEntry":
        data = json.loads(line)
        if "symbols" in data and data["symbols"] is not None:
            data["symbols"] = tuple(data["symbols"])
        else:
            data.pop("symbols", None)
        return cls(**data)

    @property
    def is_whole_domain(self) -> bool:
        """A reservation with no symbols holds the entire domain."""
        return not self.symbols


class LockLog:
    """Append-only JSONL lock log."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def append(self, entry: LockEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")

    def entries(self) -> list[LockEntry]:
        if not self.path.exists():
            return []
        out: list[LockEntry] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                out.append(LockEntry.from_json(raw))
        return out

    def active(self) -> dict[str, LockEntry]:
        """Active reservations keyed by domain — *file-level only*.

        Back-compat shim: returns at most one whole-domain entry per
        domain (most-recent wins). For symbol-aware data, use
        :meth:`active_all` which can return multiple coexisting
        reservations per domain (disjoint symbol sets).
        """
        all_active = self.active_all()
        out: dict[str, LockEntry] = {}
        for entry in all_active:
            if entry.is_whole_domain:
                out[entry.domain] = entry
            else:
                # Surface *something* per domain for callers that still
                # think file-level. Prefer the most recent.
                out.setdefault(entry.domain, entry)
        return out

    def active_all(self) -> list[LockEntry]:
        """Every currently-active reservation, in append order.

        Multiple reservations may coexist on the same domain when each
        carries a disjoint set of symbols. A *whole-domain* reservation
        (``symbols == ()``) coexisting with anything else is a registry
        bug — :func:`reserve` rejects it.

        A release is keyed by ``(domain, pr, symbols)``: it clears the
        single reservation whose key matches the last active entry.
        """
        # Walk entries in order. Treat each (domain, pr, symbols) as
        # an independent lock key. Later entries override earlier ones
        # with the same key.
        latest: dict[tuple[str, int, tuple[str, ...]], LockEntry] = {}
        for entry in self.entries():
            key = (entry.domain, entry.pr, tuple(entry.symbols))
            latest[key] = entry
        return [e for e in latest.values() if e.status == "active"]


def load_registry(path: str | os.PathLike = DEFAULT_REGISTRY) -> Registry:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"registry file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Registry.from_dict(data)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reserve(
    log: LockLog,
    registry: Registry,
    *,
    domain: str,
    pr: int,
    agent: str,
    branch: str,
    symbols: Iterable[str] = (),
    now: Optional[str] = None,
) -> LockEntry:
    """Reserve *domain* (or specific symbols within it) for *pr*/*agent*.

    Symbol-level rules:
      * ``symbols == ()`` is a whole-domain lock — refused if ANY active
        reservation (whole-domain or symbol-level) exists on this domain.
      * ``symbols != ()`` is a symbol-level lock — refused if a
        whole-domain reservation exists OR if any active symbol
        reservation on this domain overlaps the requested set.

    Re-reserving on a domain you already hold is rejected — callers
    should release first if they need to change symbol scope.

    Raises :class:`DomainHeldError` on conflict, :class:`UnknownPathError`
    on unknown domain.
    """
    if domain not in registry.domains:
        raise UnknownPathError(f"unknown domain: {domain}")
    syms = tuple(sorted(set(symbols)))

    active_on_domain = [e for e in log.active_all() if e.domain == domain]
    for held in active_on_domain:
        # Whole-domain held — refuse everything.
        if held.is_whole_domain:
            raise DomainHeldError(
                f"domain '{domain}' is fully held by PR #{held.pr} "
                f"(agent={held.agent}, branch={held.branch}, "
                f"opened_at={held.opened_at})"
            )
        # Requested whole-domain — refuse if any symbol lock exists.
        if not syms:
            raise DomainHeldError(
                f"domain '{domain}' has symbol locks held by "
                f"PR #{held.pr} (symbols={','.join(held.symbols)}) — "
                "whole-domain reservation refused"
            )
        # Symbol-level: refuse on overlap.
        held_set = set(held.symbols)
        overlap = held_set.intersection(syms)
        if overlap:
            raise DomainHeldError(
                f"symbol(s) {','.join(sorted(overlap))} in domain "
                f"'{domain}' held by PR #{held.pr} (agent={held.agent}, "
                f"branch={held.branch})"
            )

    entry = LockEntry(
        domain=domain,
        pr=pr,
        agent=agent,
        branch=branch,
        opened_at=now or _utcnow(),
        status="active",
        symbols=syms,
    )
    log.append(entry)
    return entry


@dataclass(frozen=True)
class PlanItem:
    """One leg of a multi-file reservation plan."""
    domain: str
    symbols: tuple[str, ...] = ()


def reserve_plan(
    log: LockLog,
    registry: Registry,
    *,
    pr: int,
    agent: str,
    branch: str,
    plan: Iterable[PlanItem | dict],
    now: Optional[str] = None,
) -> list[LockEntry]:
    """Atomically reserve every leg of *plan* for the same PR/agent/branch.

    All-or-nothing: if any leg conflicts, every reservation written so far
    in this call is released (with note=``rollback:reserve_plan``) before
    re-raising :class:`DomainHeldError`. Useful for a worker that edits
    several files at once — either the worker holds every required symbol
    set across every file, or it holds none and can be re-spawned.

    Each plan item is either a :class:`PlanItem` or a dict with keys
    ``domain`` and (optional) ``symbols`` (list of names).
    """
    items: list[PlanItem] = []
    for raw in plan:
        if isinstance(raw, PlanItem):
            items.append(raw)
        else:
            items.append(PlanItem(
                domain=raw["domain"],
                symbols=tuple(raw.get("symbols") or ()),
            ))

    written: list[LockEntry] = []
    rollback_note = "rollback:reserve_plan"
    for item in items:
        try:
            entry = reserve(
                log, registry,
                domain=item.domain, pr=pr,
                agent=agent, branch=branch,
                symbols=item.symbols, now=now,
            )
        except (DomainHeldError, UnknownPathError):
            # Roll back everything we wrote in this call.
            for done in written:
                log.append(LockEntry(
                    domain=done.domain,
                    pr=done.pr,
                    agent=done.agent,
                    branch=done.branch,
                    opened_at=done.opened_at,
                    status="released",
                    closed_at=now or _utcnow(),
                    note=rollback_note,
                    symbols=done.symbols,
                ))
            raise
        written.append(entry)
    return written


def release(
    log: LockLog,
    *,
    pr: int,
    domain: Optional[str] = None,
    note: Optional[str] = None,
    now: Optional[str] = None,
) -> list[LockEntry]:
    """Release active reservations for *pr* (optionally filter to a domain).

    Releases every matching active lock — whole-domain AND symbol-level.
    Returns the list of release entries written, preserving the original
    ``symbols`` tuple so the (domain, pr, symbols) key matches.
    """
    released: list[LockEntry] = []
    for entry in log.active_all():
        if entry.pr != pr:
            continue
        if domain and entry.domain != domain:
            continue
        rel = LockEntry(
            domain=entry.domain,
            pr=entry.pr,
            agent=entry.agent,
            branch=entry.branch,
            opened_at=entry.opened_at,
            status="released",
            closed_at=now or _utcnow(),
            note=note,
            symbols=entry.symbols,
        )
        log.append(rel)
        released.append(rel)
    return released


@dataclass(frozen=True)
class CheckResult:
    free: list[str]
    held: list[tuple[str, LockEntry]]  # (domain, holder)
    unmapped: list[str]
    touched_symbols: dict[str, set[str]] = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.held


def check(
    log: LockLog,
    registry: Registry,
    *,
    files: Iterable[str],
    pr: Optional[int] = None,
    touched_symbols_by_path: Optional[dict[str, set[str]]] = None,
) -> CheckResult:
    """Check whether *files* collide with any active reservation.

    Two modes:

    1. **File-level** (default — ``touched_symbols_by_path`` omitted or
       empty): a domain collides if ANY active reservation on it exists
       (whole-domain OR symbol-level), unless the holder is *pr*.

    2. **Symbol-level**: pass ``touched_symbols_by_path`` mapping each
       file to its set of touched symbol names. A whole-domain
       reservation always collides. A symbol-level reservation collides
       only if the requested touched-symbols intersect the held set.
       Files absent from the mapping are treated as whole-domain edits
       (safe fallback for non-Python or unknown).
    """
    grouped = registry.domains_for_paths(files)
    active_all = log.active_all()
    free: list[str] = []
    held: list[tuple[str, LockEntry]] = []
    unmapped = grouped.pop("__unmapped__", [])

    # Aggregate touched symbols per domain. Missing or empty entry =>
    # whole-domain edit semantics.
    per_domain_symbols: dict[str, Optional[set[str]]] = {}
    for domain, dpaths in grouped.items():
        if touched_symbols_by_path is None:
            per_domain_symbols[domain] = None
            continue
        agg: set[str] = set()
        whole_domain = False
        for path in dpaths:
            if path not in touched_symbols_by_path:
                whole_domain = True
                break
            agg.update(touched_symbols_by_path[path])
        per_domain_symbols[domain] = None if whole_domain else agg

    for domain in grouped:
        domain_holders = [e for e in active_all if e.domain == domain]
        if not domain_holders:
            free.append(domain)
            continue

        touched = per_domain_symbols[domain]
        conflict: Optional[LockEntry] = None
        for holder in domain_holders:
            if pr is not None and holder.pr == pr:
                continue  # own-PR carve-out
            if holder.is_whole_domain:
                conflict = holder
                break
            # Symbol-level holder.
            if touched is None:
                # Caller didn't supply symbol set => whole-domain edit
                # semantics => conflicts with any symbol lock.
                conflict = holder
                break
            if touched.intersection(holder.symbols):
                conflict = holder
                break
        if conflict is None:
            free.append(domain)
        else:
            held.append((domain, conflict))

    return CheckResult(
        free=free,
        held=held,
        unmapped=unmapped,
        touched_symbols={
            d: (s if s is not None else set())
            for d, s in per_domain_symbols.items()
        },
    )


def list_locks(log: LockLog, *, status: str = "active") -> list[LockEntry]:
    if status == "active":
        return log.active_all()
    if status == "all":
        return log.entries()
    return [e for e in log.entries() if e.status == status]


def audit(log: LockLog, registry: Registry) -> dict:
    """Return a structured audit report of registry + lock-log state.

    ``active_locks`` groups by domain; each value is a list of holder
    dicts to surface symbol-level co-tenancy.
    """
    active: dict[str, list[dict]] = {}
    for entry in log.active_all():
        active.setdefault(entry.domain, []).append(dataclasses.asdict(entry))
    return {
        "registry": {
            "domains": {
                name: {"paths": list(d.paths), "owners": list(d.owners)}
                for name, d in registry.domains.items()
            }
        },
        "active_locks": active,
        "total_entries": len(log.entries()),
        "generated_at": _utcnow(),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="domain_lock",
        description="Spawn-time file-domain lock registry for AI-agent PR pipelines.",
    )
    p.add_argument(
        "--registry",
        default=os.environ.get("MERGE_TRAIN_REGISTRY", DEFAULT_REGISTRY),
        help="path to YAML registry (default: %(default)s)",
    )
    p.add_argument(
        "--log",
        default=os.environ.get("MERGE_TRAIN_LOG", DEFAULT_LOG),
        help="path to JSONL lock log (default: %(default)s)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr_re = sub.add_parser("reserve", help="reserve a domain for a PR/agent")
    pr_re.add_argument("--domain", required=True)
    pr_re.add_argument("--pr", type=int, required=True)
    pr_re.add_argument("--agent", required=True)
    pr_re.add_argument("--branch", required=True)
    pr_re.add_argument(
        "--symbols", default="",
        help="comma-separated symbol names within the domain "
             "(empty = whole-domain lock)",
    )

    pr_rp = sub.add_parser(
        "reserve-plan",
        help="atomically reserve multiple (domain, symbols) legs for one PR",
    )
    pr_rp.add_argument("--pr", type=int, required=True)
    pr_rp.add_argument("--agent", required=True)
    pr_rp.add_argument("--branch", required=True)
    pr_rp.add_argument(
        "--plan", required=True,
        help="path to YAML/JSON file with a 'plan' (or 'reservations') list "
             "of {domain, symbols} entries",
    )

    pr_rl = sub.add_parser("release", help="release a PR's reservations")
    pr_rl.add_argument("--pr", type=int, required=True)
    pr_rl.add_argument("--domain", default=None)
    pr_rl.add_argument("--note", default=None)

    pr_ck = sub.add_parser("check", help="check files against active reservations")
    pr_ck.add_argument("--files", nargs="+", required=True)
    pr_ck.add_argument("--pr", type=int, default=None,
                       help="PR being checked; its own reservations don't self-conflict")
    pr_ck.add_argument("--json", action="store_true", help="JSON output")
    pr_ck.add_argument(
        "--diff-mode", action="store_true",
        help="resolve touched Python symbols from staged git diff "
             "(allows symbol-level co-tenancy on the same file)",
    )
    pr_ck.add_argument(
        "--git-cwd", default=None,
        help="git working tree to run --diff-mode against (default: cwd)",
    )

    pr_ls = sub.add_parser("list", help="list locks")
    pr_ls.add_argument("--status", default="active",
                       choices=["active", "released", "all"])
    pr_ls.add_argument("--json", action="store_true")

    sub.add_parser("audit", help="dump full registry + lock-log audit JSON")
    return p


def _fmt_entry(e: LockEntry) -> str:
    base = f"{e.domain}\tPR#{e.pr}\t{e.agent}\t{e.branch}\t{e.opened_at}\t{e.status}"
    if e.symbols:
        base += f"\tsymbols={','.join(e.symbols)}"
    if e.closed_at:
        base += f"\tclosed={e.closed_at}"
    return base


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        registry = load_registry(args.registry)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log = LockLog(args.log)

    if args.cmd == "reserve":
        syms = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        try:
            entry = reserve(
                log, registry,
                domain=args.domain, pr=args.pr,
                agent=args.agent, branch=args.branch,
                symbols=syms,
            )
        except DomainHeldError as exc:
            print(f"DENIED: {exc}", file=sys.stderr)
            return 1
        except UnknownPathError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"RESERVED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "reserve-plan":
        try:
            with open(args.plan, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError as exc:
            print(f"error: plan file not found: {exc}", file=sys.stderr)
            return 2
        plan_items = raw.get("plan") or raw.get("reservations") or []
        if not plan_items:
            print("error: plan file has no 'plan' or 'reservations' list",
                  file=sys.stderr)
            return 2
        try:
            entries = reserve_plan(
                log, registry,
                pr=args.pr, agent=args.agent, branch=args.branch,
                plan=plan_items,
            )
        except DomainHeldError as exc:
            print(f"DENIED: {exc} (plan rolled back)", file=sys.stderr)
            return 1
        except UnknownPathError as exc:
            print(f"error: {exc} (plan rolled back)", file=sys.stderr)
            return 2
        for entry in entries:
            print(f"RESERVED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "release":
        released = release(log, pr=args.pr, domain=args.domain, note=args.note)
        if not released:
            print(f"no active reservations for PR #{args.pr}", file=sys.stderr)
            return 1
        for entry in released:
            print(f"RELEASED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "check":
        touched_map: Optional[dict[str, set[str]]] = None
        if args.diff_mode:
            from merge_train.symbols import resolve_touched_symbols
            cwd = Path(args.git_cwd) if args.git_cwd else None
            per_file, _fallback = resolve_touched_symbols(args.files, cwd=cwd)
            touched_map = per_file
        result = check(
            log, registry,
            files=args.files, pr=args.pr,
            touched_symbols_by_path=touched_map,
        )
        if args.json:
            payload = {
                "ok": result.ok,
                "free_domains": result.free,
                "held": [
                    {"domain": d, "holder": dataclasses.asdict(e)}
                    for d, e in result.held
                ],
                "unmapped_files": result.unmapped,
                "touched_symbols": {
                    d: sorted(s) for d, s in result.touched_symbols.items()
                },
            }
            print(json.dumps(payload, indent=2))
        else:
            if result.unmapped:
                print(f"WARN: unmapped files (no domain): "
                      f"{', '.join(result.unmapped)}", file=sys.stderr)
            if not result.held:
                print(f"FREE: {len(result.free)} domain(s) clear "
                      f"({', '.join(result.free) or 'none'})")
            else:
                for d, holder in result.held:
                    sym_note = (
                        f" symbols={','.join(holder.symbols)}"
                        if holder.symbols else ""
                    )
                    print(f"HELD: {d} by PR#{holder.pr} "
                          f"agent={holder.agent} branch={holder.branch}{sym_note}")
        return 0 if result.ok else 1

    if args.cmd == "list":
        entries = list_locks(log, status=args.status)
        if args.json:
            print(json.dumps([dataclasses.asdict(e) for e in entries], indent=2))
        else:
            if not entries:
                print(f"no {args.status} locks")
            for entry in entries:
                print(_fmt_entry(entry))
        return 0

    if args.cmd == "audit":
        print(json.dumps(audit(log, registry), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
