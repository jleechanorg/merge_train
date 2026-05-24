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
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import yaml


DEFAULT_REGISTRY = "file_domains.yaml"


DEFAULT_LOG = "<auto>"


def _resolve_default_log(cwd: Path | str | None = None) -> str:
    base = Path.home() / ".merge_train" / "locks"
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
            cwd=str(cwd) if cwd is not None else None,
        ).stdout.strip()
    except FileNotFoundError:
        remote = ""
    slug = hashlib.sha256(remote.encode()).hexdigest()[:12] if remote else "default"
    return str(base / slug / "pr_domain_locks.jsonl")


class DomainHeldError(Exception):
    """Raised when a reservation is requested for a domain already held."""


class UnknownPathError(Exception):
    """Raised when a file path is not mapped to any declared domain."""


@dataclass(frozen=True)
class Domain:
    name: str
    paths: tuple[str, ...]
    owners: tuple[str, ...] = ()
    per_pr_unique: bool = False  # files are per-PR unique; never block each other
    advisory: bool = False       # log conflict but don't block spawning
    concurrency_limit: int = 1   # max concurrent PRs allowed to hold this domain (semaphore)


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
            per_pr_unique = bool(body.get("per_pr_unique", False))
            advisory = bool(body.get("advisory", False))
            concurrency_limit = int(body.get("concurrency_limit", 1))
            domains[name] = Domain(
                name=name, paths=paths, owners=owners,
                per_pr_unique=per_pr_unique, advisory=advisory,
                concurrency_limit=concurrency_limit,
            )
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
    override: bool = False  # True when reserved despite a conflict

    def to_json(self) -> str:
        d = dataclasses.asdict(self)
        if not d.get("symbols"):
            d.pop("symbols", None)
        else:
            d["symbols"] = list(d["symbols"])
        if not d.get("override"):
            d.pop("override", None)
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

    @contextmanager
    def lock(self):
        """Acquire an exclusive flock on the log file for atomic read-modify-write.

        On platforms without fcntl (e.g. Windows), this is a no-op.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = self.path.open("a", encoding="utf-8")
        try:
            if _HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            if _HAS_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

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
                if not raw or raw.startswith("#"):
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


_OVERRIDE_PHRASE = "CONFLICT APPROVED"


def _reserve_locked(
    log: LockLog,
    registry: Registry,
    *,
    domain: str,
    pr: int,
    agent: str,
    branch: str,
    symbols: Iterable[str] = (),
    now: Optional[str] = None,
    override: str = "",
) -> LockEntry:
    """Core reserve logic — caller must hold log.lock()."""
    if domain not in registry.domains:
        raise UnknownPathError(f"unknown domain: {domain}")
    syms = tuple(sorted(set(symbols)))

    active_on_domain = [e for e in log.active_all() if e.domain == domain]
    dom = registry.domains.get(domain)
    limit = dom.concurrency_limit if dom is not None else 1

    # Idempotency: if this PR already holds domain with same or covering symbols, no-op.
    own_active = [e for e in active_on_domain if e.pr == pr]
    if own_active:
        existing = own_active[-1]
        if not syms or (not existing.is_whole_domain and set(syms).issubset(set(existing.symbols))):
            return existing  # already held — don't append duplicate

    other_holders = [e for e in active_on_domain if e.pr != pr]
    whole_domain_other = [e for e in other_holders if e.is_whole_domain]
    symbol_others = [e for e in other_holders if not e.is_whole_domain]

    _approved = override == _OVERRIDE_PHRASE

    if not syms:
        # Whole-domain reservation: semaphore applies to concurrent whole-domain holders.
        # Symbol-level holders also block (can't take the whole domain over existing symbols).
        distinct_whole_prs = {e.pr for e in whole_domain_other}
        if len(distinct_whole_prs) >= limit:
            if not _approved:
                conflict = whole_domain_other[0]
                raise DomainHeldError(
                    f"domain '{domain}' is fully held by PR #{conflict.pr} "
                    f"(agent={conflict.agent}, branch={conflict.branch}, "
                    f"opened_at={conflict.opened_at}) — concurrency limit reached ({limit})"
                )
        if symbol_others and not _approved:
            held = symbol_others[0]
            raise DomainHeldError(
                f"domain '{domain}' has symbol locks held by "
                f"PR #{held.pr} (symbols={','.join(held.symbols)}) — "
                "whole-domain reservation refused"
            )
    else:
        # Symbol-level reservation: whole-domain holders always block;
        # symbol-level holders block only on overlap. Semaphore does not apply.
        for holder in whole_domain_other:
            if not _approved:
                raise DomainHeldError(
                    f"domain '{domain}' is fully held by PR #{holder.pr} "
                    f"(agent={holder.agent}, branch={holder.branch}, "
                    f"opened_at={holder.opened_at})"
                )
        for holder in symbol_others:
            overlap = set(holder.symbols).intersection(syms)
            if overlap and not _approved:
                raise DomainHeldError(
                    f"symbol(s) {','.join(sorted(overlap))} in domain "
                    f"'{domain}' held by PR #{holder.pr} (agent={holder.agent}, "
                    f"branch={holder.branch})"
                )

    entry = LockEntry(
        domain=domain,
        pr=pr,
        agent=agent,
        branch=branch,
        opened_at=now or _utcnow(),
        status="active",
        symbols=syms,
        override=_approved,
    )
    log.append(entry)
    return entry


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
    override: str = "",
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

    Pass ``override=_OVERRIDE_PHRASE`` to force-reserve despite conflicts.
    The entry is written with ``override=True`` for audit purposes.

    Raises :class:`DomainHeldError` on conflict, :class:`UnknownPathError`
    on unknown domain.
    """
    with log.lock():
        return _reserve_locked(
            log, registry,
            domain=domain, pr=pr, agent=agent, branch=branch,
            symbols=symbols, now=now, override=override,
        )


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
    with log.lock():
        for item in items:
            try:
                entry = _reserve_locked(
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
    with log.lock():
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
    advisory_held: list[tuple[str, LockEntry]] = dataclasses.field(default_factory=list)  # warn-only conflicts

    @property
    def ok(self) -> bool:
        return not self.held


def check(
    log: LockLog,
    registry: Registry,
    *,
    files: Iterable[str],
    pr: Optional[int] = None,
    touched_symbols_by_path: Optional[dict[str, Optional[set[str]]]] = None,
    override: str = "",
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

       Three states per path in the mapping:
       - **Not in dict** → whole-domain fallback (unknown/unresolvable)
       - **In dict, value ``None``** → whole-domain fallback (parse failure)
       - **In dict, value ``set``** → symbol-level result; even an empty
         set means "resolution succeeded, no symbols touched" (genuinely free)
    """
    grouped = registry.domains_for_paths(files)
    active_all = log.active_all()
    free: list[str] = []
    held: list[tuple[str, LockEntry]] = []
    advisory_held_list: list[tuple[str, LockEntry]] = []
    unmapped = grouped.pop("__unmapped__", [])

    # Aggregate touched symbols per domain. Three states per path:
    #   not in dict          → whole-domain fallback (unknown/unresolvable)
    #   in dict, value None  → whole-domain fallback (parse failure)
    #   in dict, value set   → symbol-level (even empty = genuinely no overlap)
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
            sym_set = touched_symbols_by_path[path]
            if sym_set is None:
                whole_domain = True
                break
            agg.update(sym_set)
        per_domain_symbols[domain] = None if whole_domain else agg

    for domain in grouped:
        domain_holders = [e for e in active_all if e.domain == domain]
        if not domain_holders:
            free.append(domain)
            continue

        dom = registry.domains.get(domain)
        limit = dom.concurrency_limit if dom is not None else 1

        other_holders = [e for e in domain_holders if pr is None or e.pr != pr]
        whole_domain_other = [e for e in other_holders if e.is_whole_domain]
        symbol_others = [e for e in other_holders if not e.is_whole_domain]

        touched = per_domain_symbols[domain]
        conflict: Optional[LockEntry] = None

        if touched is None:
            # Whole-domain check: semaphore applies to concurrent whole-domain holders.
            # Symbol-level holders also collide (whole-domain write would stomp them).
            distinct_whole_prs = {e.pr for e in whole_domain_other}
            if len(distinct_whole_prs) >= limit:
                conflict = whole_domain_other[0]
            elif symbol_others:
                conflict = symbol_others[0]
        else:
            # Symbol-level check: whole-domain holders always collide;
            # symbol-level holders collide only on symbol overlap. No semaphore.
            for holder in whole_domain_other:
                conflict = holder
                break
            if conflict is None:
                for holder in symbol_others:
                    if touched.intersection(holder.symbols):
                        conflict = holder
                        break

        if conflict is None or override == _OVERRIDE_PHRASE:
            free.append(domain)
        elif dom is not None and dom.advisory:
            advisory_held_list.append((domain, conflict))
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
        advisory_held=advisory_held_list,
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


def _add_global_opts_to_subparser(sp: argparse.ArgumentParser) -> None:
    """Re-register the top-level globals on a subparser so the legacy
    ``domain_lock <cmd> ... --registry/--log/--git-cwd`` form still parses.

    ``default=argparse.SUPPRESS`` is critical: it leaves the attribute
    unset in the Namespace when the user does NOT pass the flag at the
    subcommand level, so the value parsed at the top-level parser is
    preserved instead of being clobbered by ``None``.
    """
    sp.add_argument("--registry", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--log", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--git-cwd", default=argparse.SUPPRESS, help=argparse.SUPPRESS)


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
    p.add_argument(
        "--git-cwd", default=None,
        help="git working tree for default log-path resolution (default: cwd)",
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
    pr_re.add_argument(
        "--dry-run", action="store_true",
        help="check for conflicts without acquiring the lock; exits 0 if "
             "the domain is free, 1 if held, and prints WOULD-RESERVE",
    )
    pr_re.add_argument(
        "--override", default="",
        help=f"type '{_OVERRIDE_PHRASE}' to force-reserve despite a held domain "
             "(conflict bypassed; entry logged with override=true)",
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
    pr_rp.add_argument(
        "--dry-run", action="store_true",
        help="check for conflicts without acquiring locks; exits 0 if all "
             "legs are free, 1 if any is held, and prints WOULD-RESERVE per leg",
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
        "--symbols", default="",
        help="comma-separated touched symbol names (overrides --diff-mode; "
             "enables symbol-level check without staged git diff)",
    )
    pr_ck.add_argument(
        "--override", default="",
        help=f"type '{_OVERRIDE_PHRASE}' to treat held domains as free",
    )

    pr_ls = sub.add_parser("list", help="list locks")
    pr_ls.add_argument("--status", default="active",
                       choices=["active", "released", "all"])
    pr_ls.add_argument("--json", action="store_true")

    pr_au = sub.add_parser("audit", help="dump full registry + lock-log audit JSON")

    pr_pc = sub.add_parser(
        "predict-conflicts",
        help="dry-run: predict pairwise conflicts + recommend merge order for a "
             "set of PRs declared in a YAML plan",
    )
    pr_pc.add_argument("--plan",
                       help="path to YAML/JSON file with a 'prs' list")
    pr_pc.add_argument("--from-prs", metavar="N,M,...",
                       help="comma-separated PR numbers; bypasses --plan, "
                            "fetches file lists from GitHub via gh")
    pr_pc.add_argument("--repo", metavar="OWNER/REPO",
                       help="GitHub repo for --from-prs / --enrich-symbols "
                            "(e.g. jleechanorg/worldarchitect.ai)")
    pr_pc.add_argument("--enrich-symbols", action="store_true",
                       help="auto-populate symbols_by_file from gh pr diff "
                            "for PRs that declare no symbols")
    pr_pc.add_argument("--no-textual", action="store_true",
                       help="skip git merge-tree textual conflict check")
    pr_pc.add_argument("--git-base", default="origin/main",
                       help="base ref for git merge-tree (default: origin/main)")
    pr_pc.add_argument("--json", action="store_true", help="JSON output")


    pr_rd = sub.add_parser("recommend-domains", help="analyze recent repo history and suggest file + symbol lock domains")
    pr_rd.add_argument("--repo", default=".", help="path to git repo (default: cwd)")
    pr_rd.add_argument("--since-days", type=int, default=30, help="lookback window in days")
    pr_rd.add_argument("--top-n", type=int, default=8, help="number of hotspot seeds")
    pr_rd.add_argument("--json", action="store_true", help="JSON output")

    for _sp in (pr_re, pr_rp, pr_rl, pr_ck, pr_ls, pr_au, pr_pc, pr_rd):
        _add_global_opts_to_subparser(_sp)
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
    if args.log == DEFAULT_LOG:
        cwd = Path(args.git_cwd) if args.git_cwd else None
        args.log = _resolve_default_log(cwd)

    if args.cmd == "reserve":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        syms = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        if args.dry_run:
            if args.domain not in registry.domains:
                print(f"error: unknown domain: {args.domain}", file=sys.stderr)
                return 2
            active = log.active_all()
            holders = [e for e in active if e.domain == args.domain]
            for h in holders:
                if h.is_whole_domain:
                    print(f"HELD: {args.domain} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                    return 1
                if syms and set(syms).intersection(h.symbols):
                    overlap = sorted(set(syms).intersection(h.symbols))
                    print(f"HELD: {args.domain} symbols {overlap} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                    return 1
                if not syms:
                    print(f"HELD: {args.domain} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                    return 1
            sym_part = f" symbols={syms}" if syms else ""
            print(f"WOULD-RESERVE: {args.domain}\tPR#{args.pr}\t{args.agent}\t{args.branch}{sym_part}")
            return 0
        try:
            entry = reserve(
                log, registry,
                domain=args.domain, pr=args.pr,
                agent=args.agent, branch=args.branch,
                symbols=syms,
                override=getattr(args, "override", ""),
            )
        except DomainHeldError as exc:
            print(f"DENIED: {exc}", file=sys.stderr)
            return 1
        except UnknownPathError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        tag = "OVERRIDE-RESERVED" if entry.override else "RESERVED"
        print(f"{tag}: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "reserve-plan":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
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
        if args.dry_run:
            active = log.active_all()
            held = False
            for raw_item in plan_items:
                dom = raw_item["domain"] if isinstance(raw_item, dict) else raw_item.domain
                syms = (raw_item.get("symbols") or ()) if isinstance(raw_item, dict) else raw_item.symbols
                if dom not in registry.domains:
                    print(f"error: unknown domain: {dom}", file=sys.stderr)
                    return 2
                holders = [e for e in active if e.domain == dom]
                for h in holders:
                    if h.is_whole_domain:
                        print(f"HELD: {dom} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                        held = True
                        break
                    if syms and set(syms).intersection(h.symbols):
                        overlap = sorted(set(syms).intersection(h.symbols))
                        print(f"HELD: {dom} symbols {overlap} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                        held = True
                        break
                    if not syms:
                        print(f"HELD: {dom} by PR#{h.pr} ({h.agent}/{h.branch})", file=sys.stderr)
                        held = True
                        break
                else:
                    sym_part = f" symbols={list(syms)}" if syms else ""
                    print(f"WOULD-RESERVE: {dom}\tPR#{args.pr}\t{args.agent}\t{args.branch}{sym_part}")
            return 1 if held else 0
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
        log = LockLog(args.log)
        released = release(log, pr=args.pr, domain=args.domain, note=args.note)
        if not released:
            print(f"no active reservations for PR #{args.pr}", file=sys.stderr)
            return 1
        for entry in released:
            print(f"RELEASED: {_fmt_entry(entry)}")
        return 0

    if args.cmd == "check":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        touched_map: Optional[dict[str, Optional[set[str]]]] = None
        diff_fallback: list[str] = []
        if getattr(args, "symbols", ""):
            syms = set(s.strip() for s in args.symbols.split(",") if s.strip())
            touched_map = {f: syms for f in args.files}
        elif args.diff_mode:
            from merge_train.symbols import resolve_touched_symbols
            cwd = Path(args.git_cwd) if args.git_cwd else None
            per_file, fallback = resolve_touched_symbols(args.files, cwd=cwd)
            touched_map = per_file
            diff_fallback = fallback
            for fb_path in fallback:
                touched_map[fb_path] = None
        result = check(
            log, registry,
            files=args.files, pr=args.pr,
            touched_symbols_by_path=touched_map,
            override=getattr(args, "override", ""),
        )
        if args.json:
            payload = {
                "ok": result.ok,
                "free_domains": result.free,
                "held": [
                    {"domain": d, "holder": dataclasses.asdict(e)}
                    for d, e in result.held
                ],
                "advisory_held": [
                    {"domain": d, "holder": dataclasses.asdict(e)}
                    for d, e in result.advisory_held
                ],
                "unmapped_files": result.unmapped,
                "touched_symbols": {
                    d: sorted(s) for d, s in result.touched_symbols.items()
                },
            }
            if diff_fallback:
                payload["fallback_files"] = diff_fallback
            print(json.dumps(payload, indent=2))
        else:
            if result.unmapped:
                print(f"WARN: unmapped files (no domain): "
                      f"{', '.join(result.unmapped)}", file=sys.stderr)
            if diff_fallback:
                print(f"WARN: symbol-resolution fallback (whole-domain): "
                      f"{', '.join(diff_fallback)}", file=sys.stderr)
            for d, holder in result.advisory_held:
                sym_note = (
                    f" symbols={','.join(holder.symbols)}"
                    if holder.symbols else ""
                )
                print(f"ADVISORY: {d} by PR#{holder.pr} "
                      f"agent={holder.agent} branch={holder.branch}{sym_note}")
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
        log = LockLog(args.log)
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
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        log = LockLog(args.log)
        print(json.dumps(audit(log, registry), indent=2))
        return 0

    if args.cmd == "recommend-domains":
        from merge_train.domain_recommender import recommend_domains, to_yaml_dict
        repo = Path(args.repo)
        suggestions = recommend_domains(repo=repo, since_days=args.since_days, top_n=args.top_n)
        payload = to_yaml_dict(suggestions)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(yaml.safe_dump(payload, sort_keys=False))
        return 0

    if args.cmd == "predict-conflicts":
        try:
            registry = load_registry(args.registry)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        from merge_train.predict import cli_predict_conflicts
        cwd = Path(args.git_cwd) if args.git_cwd else None
        plan_path = getattr(args, "plan", None)
        from_prs = getattr(args, "from_prs", None)
        if not plan_path and not from_prs:
            print("error: one of --plan or --from-prs is required",
                  file=sys.stderr)
            return 2
        return cli_predict_conflicts(
            plan_path=plan_path,
            registry=registry,
            include_textual=not args.no_textual,
            git_base=args.git_base,
            git_cwd=cwd,
            json_output=args.json,
            from_prs=from_prs,
            repo=getattr(args, "repo", None),
            enrich_symbols=getattr(args, "enrich_symbols", False),
        )

    return 2


if __name__ == "__main__":
    sys.exit(main())
